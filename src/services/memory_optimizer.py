import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from src.core.config import settings
from src.utils.ulid import generate_message_id, generate_part_id

logger = logging.getLogger(__name__)


class MemoryOptimizer:
    """세션 메모리(컨텍스트 토큰) 최적화 전략 — SessionManager 에서 분리한 Compaction V2.

    Phase A 선택적 pruning(오래된 web_fetch 본문 → [PRUNED], retry 파트 삭제, 최근
    ``session_recent_window`` 메시지 보호) → Phase B 구조화 요약(compressor) → compaction
    경계 마커. 임계값(``session_compact_threshold``)을 넘는 동안에만 동작한다.

    강화: 요약 입력에서 동일 내용을 dedup 해 압축 비용/노이즈를 줄인다(중복 메시지의
    DB 삭제는 그대로 수행하되, 요약기에는 고유 내용만 전달).

    DB 는 SessionManager 가 소유하므로 연결 팩토리(``get_conn``)와 토큰 추정기
    (``estimate_tokens``)를 주입받는다.
    """

    def __init__(
        self,
        get_conn: Callable[[], sqlite3.Connection],
        estimate_tokens: Callable[[str], int],
    ) -> None:
        self._get_conn = get_conn
        self._estimate_tokens = estimate_tokens

    def optimize_sync(self, session_id: str, compressor: Any) -> None:
        try:
            current_tokens = self._estimate_tokens(session_id)
            if current_tokens <= settings.session_compact_threshold:
                return

            recent_window = settings.session_recent_window

            # 1) 읽기: 보호 대상(최근 window) + 요약 대상(마지막 compaction 경계 이후 text).
            with self._get_conn() as conn:
                protected_ids = {
                    row["id"]
                    for row in conn.execute(
                        "SELECT id FROM messages WHERE session_id = ? "
                        "ORDER BY rowid DESC LIMIT ?",
                        (session_id, recent_window),
                    ).fetchall()
                }
                boundary = conn.execute(
                    "SELECT m.rowid AS mrowid FROM messages m "
                    "JOIN parts p ON p.message_id = m.id "
                    "WHERE m.session_id = ? AND p.type = 'compaction' "
                    "ORDER BY m.rowid DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                if boundary:
                    old_msgs = conn.execute(
                        "SELECT m.id, m.role, p.data FROM messages m "
                        "JOIN parts p ON p.message_id = m.id "
                        "WHERE m.session_id = ? AND m.rowid >= ? "
                        "AND p.type = 'text' ORDER BY m.rowid ASC",
                        (session_id, boundary["mrowid"]),
                    ).fetchall()
                else:
                    old_msgs = conn.execute(
                        "SELECT m.id, m.role, p.data FROM messages m "
                        "JOIN parts p ON p.message_id = m.id "
                        "WHERE m.session_id = ? AND p.type = 'text' ORDER BY m.rowid ASC",
                        (session_id,),
                    ).fetchall()

            # 2) 요약 입력 dedup + 압축(트랜잭션 밖에서 수행 — 느린 압축 중 쓰기락 미보유).
            to_summarize: list[str] = []
            seen: set[str] = set()
            old_msg_ids: set[str] = set()
            for row in old_msgs:
                if row["id"] in protected_ids:
                    continue
                text = json.loads(row["data"]).get("text", "")[:500]
                old_msg_ids.add(row["id"])  # 중복이어도 메시지 자체는 삭제 대상
                entry = f"{row['role']}: {text}"
                if entry not in seen:
                    seen.add(entry)
                    to_summarize.append(entry)

            summary = None
            if to_summarize:
                summary = compressor.compress(
                    "\n".join(to_summarize),
                    instruction="Summarize goal/decisions/discoveries concisely.",
                    target_token=800,
                )

            # 3) 단일 쓰기 트랜잭션: Phase A pruning + (요약 있으면) 삭제 + compaction 마커.
            #    하나의 commit 으로 묶어 pruned-but-unsummarized 중간 상태를 막는다(N5).
            with self._get_conn() as conn:
                if protected_ids:
                    placeholders = ",".join(["?"] * len(protected_ids))
                    conn.execute(
                        f"UPDATE parts SET data = json_replace(data, '$.content', '[PRUNED]') "
                        f"WHERE type = 'web_fetch' "
                        f"AND message_id NOT IN ({placeholders}) "
                        f"AND message_id IN (SELECT id FROM messages WHERE session_id = ?)",
                        (*protected_ids, session_id),
                    )
                    conn.execute(
                        f"DELETE FROM parts WHERE type = 'retry' "
                        f"AND message_id NOT IN ({placeholders}) "
                        f"AND message_id IN (SELECT id FROM messages WHERE session_id = ?)",
                        (*protected_ids, session_id),
                    )

                if to_summarize:
                    old_msg_list = list(old_msg_ids)
                    if old_msg_list:
                        ph = ",".join(["?"] * len(old_msg_list))
                        conn.execute(
                            f"DELETE FROM messages WHERE id IN ({ph})", old_msg_list
                        )
                    compact_msg_id = generate_message_id()
                    conn.execute(
                        "INSERT INTO messages (id, session_id, role) VALUES (?, ?, 'system')",
                        (compact_msg_id, session_id),
                    )
                    token_saving = max(0, current_tokens - max(1, len(summary or "") // 4))
                    conn.execute(
                        "INSERT INTO parts (id, message_id, type, data) VALUES (?, ?, 'compaction', ?)",
                        (
                            generate_part_id(),
                            compact_msg_id,
                            json.dumps(
                                {
                                    "auto": True,
                                    "overflow": True,
                                    "summary": summary,
                                    "compressed_count": len(old_msg_list),
                                    "token_saving": token_saving,
                                }
                            ),
                        ),
                    )
                conn.commit()

        except Exception as e:
            logger.error(f"Error optimizing session memory {session_id}: {e}")
