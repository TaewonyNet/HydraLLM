import asyncio
import hashlib
import json
import logging
import sqlite3
import subprocess
from datetime import datetime
from typing import Any

from src.core.config import settings
from src.domain.interfaces import ISessionManager
from src.domain.models import ChatMessage, MessagePart, SessionMessage
from src.utils.ulid import generate_message_id, generate_part_id, generate_session_id

logger = logging.getLogger(__name__)


def _get_project_id() -> str:
    """git root 경로를 해싱하여 project_id를 생성."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return hashlib.sha256(result.stdout.strip().encode()).hexdigest()[:12]
    except Exception:
        pass
    return "default"


class SessionManager(ISessionManager):
    def __init__(self, db_path: str | None = None):
        import os

        base_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        raw_path = db_path or settings.database_path
        if not os.path.isabs(raw_path):
            self.db_path = os.path.join(base_dir, raw_path)
        else:
            self.db_path = raw_path

        self.project_id = _get_project_id()
        self._init_db()

    # ─── 커넥션 관리 ───

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def close(self) -> None:
        """앱 종료 시 호출."""
        pass

    # ─── DB 초기화 ───

    def _init_db(self) -> None:
        try:
            with self._get_conn() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        parent_session_id TEXT,
                        fork_point_message_id TEXT,
                        title TEXT DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
                        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
                        message_count INTEGER DEFAULT 0,
                        last_model TEXT DEFAULT '',
                        last_provider TEXT DEFAULT '',
                        summary_token_count INTEGER DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS messages (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                        role TEXT NOT NULL CHECK(role IN ('system','user','assistant')),
                        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
                    );

                    CREATE TABLE IF NOT EXISTS parts (
                        id TEXT PRIMARY KEY,
                        message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                        type TEXT NOT NULL CHECK(type IN ('text','web_fetch','web_search','compaction','step_cost','retry')),
                        data TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
                    );

                    CREATE TABLE IF NOT EXISTS system_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
                    );

                    CREATE TABLE IF NOT EXISTS system_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        level TEXT NOT NULL,
                        category TEXT NOT NULL,
                        message TEXT NOT NULL,
                        metadata TEXT DEFAULT '{}',
                        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
                    );

                    CREATE TABLE IF NOT EXISTS usage_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        endpoint TEXT DEFAULT 'chat',
                        prompt_tokens INTEGER DEFAULT 0,
                        completion_tokens INTEGER DEFAULT 0,
                        total_tokens INTEGER DEFAULT 0,
                        latency_ms INTEGER DEFAULT 0,
                        status TEXT DEFAULT 'success',
                        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
                    );

                    CREATE TABLE IF NOT EXISTS provider_health (
                        provider TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        last_error TEXT,
                        active_keys INTEGER DEFAULT 0,
                        failed_keys INTEGER DEFAULT 0,
                        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
                    );

                    CREATE TABLE IF NOT EXISTS daily_usage (
                        day TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        prompt_tokens INTEGER DEFAULT 0,
                        completion_tokens INTEGER DEFAULT 0,
                        total_tokens INTEGER DEFAULT 0,
                        request_count INTEGER DEFAULT 0,
                        error_count INTEGER DEFAULT 0,
                        PRIMARY KEY (day, provider, model)
                    );

                    CREATE TABLE IF NOT EXISTS web_content_cache (
                        url TEXT PRIMARY KEY,
                        content TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        cached_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
                    );

                    CREATE TABLE IF NOT EXISTS scraping_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL,
                        query TEXT,
                        status TEXT NOT NULL,
                        chars_count INTEGER DEFAULT 0,
                        response_summary TEXT,
                        latency_ms INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
                    );

                    CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_parts_message ON parts(message_id);
                    CREATE INDEX IF NOT EXISTS idx_parts_type ON parts(type);
                    CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
                """
                )
                conn.commit()

            with self._get_conn() as conn:
                cols_usage = [
                    row["name"]
                    for row in conn.execute(
                        "PRAGMA table_info(usage_metrics)"
                    ).fetchall()
                ]
                if "endpoint" not in cols_usage:
                    conn.execute(
                        "ALTER TABLE usage_metrics ADD COLUMN endpoint TEXT DEFAULT 'chat'"
                    )
                    conn.commit()

            try:
                with self._get_conn() as conn:
                    conn.execute("SAVEPOINT test_part")
                    conn.execute(
                        "INSERT INTO parts (id, message_id, type) VALUES ('test_mig', 'none', 'web_search')"
                    )
                    conn.execute("ROLLBACK TO SAVEPOINT test_part")
            except (sqlite3.OperationalError, sqlite3.IntegrityError):
                with self._get_conn() as conn:
                    conn.execute("PRAGMA foreign_keys=OFF")
                    conn.execute("ALTER TABLE parts RENAME TO parts_old")
                    conn.execute(
                        """
                        CREATE TABLE parts (
                            id TEXT PRIMARY KEY,
                            message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                            type TEXT NOT NULL CHECK(type IN ('text','web_fetch','web_search','compaction','step_cost','retry')),
                            data TEXT NOT NULL DEFAULT '{}',
                            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
                        )
                    """
                    )
                    conn.execute(
                        "INSERT INTO parts SELECT * FROM parts_old WHERE type IN ('text','web_fetch','compaction','step_cost','retry')"
                    )
                    conn.execute("DROP TABLE parts_old")
                    conn.execute("PRAGMA foreign_keys=ON")
                    conn.commit()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Failed to initialize SQLite: {e}")

    # ─── 세션 생성/조회 ───

    def _create_session_sync(
        self, session_id: str | None = None, title: str | None = None
    ) -> str:
        sid = session_id or generate_session_id()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO sessions (id, project_id, title) VALUES (?, ?, ?)",
                    (sid, self.project_id, title or "New Session"),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error creating session: {e}")
        return sid

    async def create_session(
        self, session_id: str | None = None, title: str | None = None
    ) -> str:
        return await asyncio.to_thread(self._create_session_sync, session_id, title)

    def _ensure_session_sync(self, session_id: str) -> None:
        """세션이 없으면 자동 생성."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    conn.execute(
                        "INSERT INTO sessions (id, project_id, title) VALUES (?, ?, ?)",
                        (session_id, self.project_id, "Auto Session"),
                    )
                    conn.commit()
        except Exception as e:
            logger.error(f"Error ensuring session {session_id}: {e}")

    # ─── 메시지 저장 ───

    def _save_message_sync(
        self,
        session_id: str,
        role: str,
        content: Any,
        parts: list[dict[str, Any]] | None = None,
    ) -> str:
        """메시지 + 파트 저장. message_id 반환."""
        self._ensure_session_sync(session_id)
        msg_id = generate_message_id()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO messages (id, session_id, role) VALUES (?, ?, ?)",
                    (msg_id, session_id, role),
                )

                # 기본 text 파트 생성
                if isinstance(content, dict | list):
                    content = json.dumps(content, ensure_ascii=False)
                content_str = str(content)

                prt_id = generate_part_id()
                conn.execute(
                    "INSERT INTO parts (id, message_id, type, data) VALUES (?, ?, 'text', ?)",
                    (prt_id, msg_id, json.dumps({"text": content_str})),
                )

                # 추가 파트 (web_fetch, step_cost, retry 등)
                if parts:
                    for part in parts:
                        p_id = generate_part_id()
                        conn.execute(
                            "INSERT INTO parts (id, message_id, type, data) VALUES (?, ?, ?, ?)",
                            (p_id, msg_id, part["type"], json.dumps(part["data"])),
                        )

                conn.execute(
                    "UPDATE sessions SET updated_at = strftime('%Y-%m-%dT%H:%M:%f','now'), "
                    "message_count = message_count + 1 WHERE id = ?",
                    (session_id,),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error saving message for {session_id}: {e}")
        return msg_id

    async def save_message(
        self,
        session_id: str,
        role: str,
        content: Any,
        parts: list[dict[str, Any]] | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._save_message_sync, session_id, role, content, parts
        )

    # ─── 파트 추가 ───

    def _add_part_sync(
        self, message_id: str, part_type: str, data: dict[str, Any]
    ) -> str:
        """기존 메시지에 파트 추가."""
        prt_id = generate_part_id()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO parts (id, message_id, type, data) VALUES (?, ?, ?, ?)",
                    (prt_id, message_id, part_type, json.dumps(data)),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error adding part to {message_id}: {e}")
        return prt_id

    async def add_part(
        self, message_id: str, part_type: str, data: dict[str, Any]
    ) -> str:
        return await asyncio.to_thread(self._add_part_sync, message_id, part_type, data)

    # ─── 컨텍스트 로드 (compaction 경계 이후 메시지만) ───

    def _load_context_sync(self, session_id: str) -> list[ChatMessage]:
        """마지막 compaction 파트 이후 메시지만 로드하여 ChatMessage 리스트로 반환."""
        try:
            with self._get_conn() as conn:
                # 마지막 compaction 경계 찾기
                boundary_row = conn.execute(
                    "SELECT m.rowid AS mrowid FROM messages m "
                    "JOIN parts p ON p.message_id = m.id "
                    "WHERE m.session_id = ? AND p.type = 'compaction' "
                    "ORDER BY m.rowid DESC LIMIT 1",
                    (session_id,),
                ).fetchone()

                # 경계 이후 메시지 + text/compaction 파트 로드
                if boundary_row:
                    rows = conn.execute(
                        "SELECT m.id, m.role, p.type, p.data "
                        "FROM messages m "
                        "JOIN parts p ON p.message_id = m.id "
                        "WHERE m.session_id = ? AND m.rowid >= ? "
                        "AND p.type IN ('text', 'compaction') "
                        "ORDER BY m.rowid ASC, p.rowid ASC",
                        (session_id, boundary_row["mrowid"]),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT m.id, m.role, p.type, p.data "
                        "FROM messages m "
                        "JOIN parts p ON p.message_id = m.id "
                        "WHERE m.session_id = ? AND p.type IN ('text', 'compaction') "
                        "ORDER BY m.rowid ASC, p.rowid ASC",
                        (session_id,),
                    ).fetchall()

                # 메시지별로 파트를 합성하여 ChatMessage 생성
                messages: list[ChatMessage] = []
                current_msg_id: str | None = None
                current_content_parts: list[str] = []
                current_role = ""

                for row in rows:
                    if row["id"] != current_msg_id:
                        if current_msg_id and current_content_parts:
                            messages.append(
                                ChatMessage(
                                    role=current_role,
                                    content="\n".join(current_content_parts),
                                    name=None,
                                )
                            )
                        current_msg_id = row["id"]
                        current_role = row["role"]
                        current_content_parts = []

                    data = json.loads(row["data"])
                    if row["type"] == "text":
                        current_content_parts.append(data.get("text", ""))
                    elif row["type"] == "compaction":
                        current_content_parts.append(
                            f"[Session Summary]\n{data.get('summary', '')}"
                        )

                # 마지막 메시지 처리
                if current_msg_id and current_content_parts:
                    messages.append(
                        ChatMessage(
                            role=current_role,
                            content="\n".join(current_content_parts),
                            name=None,
                        )
                    )

                return messages
        except Exception as e:
            logger.error(f"Error loading context for {session_id}: {e}")
            return []

    async def load_context(self, session_id: str) -> list[ChatMessage]:
        return await asyncio.to_thread(self._load_context_sync, session_id)

    # ─── 메시지 + 파트 상세 로드 (admin/디버그) ───

    def _load_messages_with_parts_sync(self, session_id: str) -> list[SessionMessage]:
        """파트를 포함한 전체 메시지 목록 반환."""
        try:
            with self._get_conn() as conn:
                msg_rows = conn.execute(
                    "SELECT id, session_id, role, created_at FROM messages "
                    "WHERE session_id = ? ORDER BY rowid ASC",
                    (session_id,),
                ).fetchall()

                result: list[SessionMessage] = []
                for msg in msg_rows:
                    part_rows = conn.execute(
                        "SELECT id, message_id, type, data, created_at FROM parts "
                        "WHERE message_id = ? ORDER BY rowid ASC",
                        (msg["id"],),
                    ).fetchall()

                    parts = [
                        MessagePart(
                            id=p["id"],
                            message_id=p["message_id"],
                            type=p["type"],
                            data=json.loads(p["data"]),
                            created_at=p["created_at"],
                        )
                        for p in part_rows
                    ]

                    result.append(
                        SessionMessage(
                            id=msg["id"],
                            session_id=msg["session_id"],
                            role=msg["role"],
                            parts=parts,
                            created_at=msg["created_at"],
                        )
                    )
                return result
        except Exception as e:
            logger.error(f"Error loading messages with parts for {session_id}: {e}")
            return []

    async def load_messages_with_parts(self, session_id: str) -> list[SessionMessage]:
        return await asyncio.to_thread(self._load_messages_with_parts_sync, session_id)

    # ─── 토큰 overflow 감지 ───

    def _estimate_session_tokens_sync(self, session_id: str) -> int:
        """컴팩션 경계 이후 text 파트의 토큰 추정치 합산."""
        try:
            with self._get_conn() as conn:
                boundary_row = conn.execute(
                    "SELECT m.rowid AS mrowid FROM messages m "
                    "JOIN parts p ON p.message_id = m.id "
                    "WHERE m.session_id = ? AND p.type = 'compaction' "
                    "ORDER BY m.rowid DESC LIMIT 1",
                    (session_id,),
                ).fetchone()

                if boundary_row:
                    rows = conn.execute(
                        "SELECT p.data FROM parts p "
                        "JOIN messages m ON p.message_id = m.id "
                        "WHERE m.session_id = ? AND m.rowid >= ? "
                        "AND p.type = 'text'",
                        (session_id, boundary_row["mrowid"]),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT p.data FROM parts p "
                        "JOIN messages m ON p.message_id = m.id "
                        "WHERE m.session_id = ? AND p.type = 'text'",
                        (session_id,),
                    ).fetchall()

                total = 0
                for row in rows:
                    data = json.loads(row["data"])
                    total += max(1, len(data.get("text", "")) // 4)
                return total
        except Exception as e:
            logger.error(f"Error estimating tokens for {session_id}: {e}")
            return 0

    def is_overflow(self, session_id: str) -> bool:
        """compaction 임계값 초과 여부."""
        tokens = self._estimate_session_tokens_sync(session_id)
        return tokens > settings.session_compact_threshold

    # ─── Compaction V2: 선택적 pruning → 구조화된 요약 ───

    def _compact_sync(self, session_id: str, compressor: Any) -> None:
        """V2 컴팩션: 선택적 pruning → 구조화된 요약 → CompactionPart 경계 마커."""
        try:
            current_tokens = self._estimate_session_tokens_sync(session_id)
            if current_tokens <= settings.session_compact_threshold:
                return

            recent_window = settings.session_recent_window

            with self._get_conn() as conn:
                # Phase A: 선택적 Pruning
                protected_ids_rows = conn.execute(
                    "SELECT id FROM messages WHERE session_id = ? "
                    "ORDER BY rowid DESC LIMIT ?",
                    (session_id, recent_window),
                ).fetchall()
                protected_ids = {row["id"] for row in protected_ids_rows}

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
                        f"DELETE FROM parts "
                        f"WHERE type = 'retry' "
                        f"AND message_id NOT IN ({placeholders}) "
                        f"AND message_id IN (SELECT id FROM messages WHERE session_id = ?)",
                        (*protected_ids, session_id),
                    )
                conn.commit()

            # 재측정
            current_tokens = self._estimate_session_tokens_sync(session_id)
            if current_tokens <= settings.session_compact_threshold:
                return

            # Phase B: 구조화된 요약
            with self._get_conn() as conn:
                boundary_row2 = conn.execute(
                    "SELECT m.rowid AS mrowid FROM messages m "
                    "JOIN parts p ON p.message_id = m.id "
                    "WHERE m.session_id = ? AND p.type = 'compaction' "
                    "ORDER BY m.rowid DESC LIMIT 1",
                    (session_id,),
                ).fetchone()

                if boundary_row2:
                    old_msgs = conn.execute(
                        "SELECT m.id, m.role, p.data FROM messages m "
                        "JOIN parts p ON p.message_id = m.id "
                        "WHERE m.session_id = ? AND m.rowid >= ? "
                        "AND p.type = 'text' ORDER BY m.rowid ASC",
                        (session_id, boundary_row2["mrowid"]),
                    ).fetchall()
                else:
                    old_msgs = conn.execute(
                        "SELECT m.id, m.role, p.data FROM messages m "
                        "JOIN parts p ON p.message_id = m.id "
                        "WHERE m.session_id = ? AND p.type = 'text' ORDER BY m.rowid ASC",
                        (session_id,),
                    ).fetchall()

                to_summarize = []
                old_msg_ids = set()
                for row in old_msgs:
                    if row["id"] not in protected_ids:
                        data = json.loads(row["data"])
                        text = data.get("text", "")[:500]
                        to_summarize.append(f"{row['role']}: {text}")
                        old_msg_ids.add(row["id"])

                if not to_summarize:
                    return

                old_text = "\n".join(to_summarize)
                summary = compressor.compress(
                    old_text,
                    instruction="Summarize goal/decisions/discoveries concisely.",
                    target_token=800,
                )

                old_msg_list = list(old_msg_ids)
                if old_msg_list:
                    placeholders = ",".join(["?"] * len(old_msg_list))
                    conn.execute(
                        f"DELETE FROM messages WHERE id IN ({placeholders})",
                        old_msg_list,
                    )

                compact_msg_id = generate_message_id()
                conn.execute(
                    "INSERT INTO messages (id, session_id, role) VALUES (?, ?, 'system')",
                    (compact_msg_id, session_id),
                )

                new_tokens = self._estimate_session_tokens_sync(session_id)
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
                                "token_saving": current_tokens - new_tokens,
                            }
                        ),
                    ),
                )
                conn.commit()

        except Exception as e:
            logger.error(f"Error compacting session {session_id}: {e}")

    async def compact(self, session_id: str, compressor: Any) -> None:
        await asyncio.to_thread(self._compact_sync, session_id, compressor)

    # ─── 세션 포크 ───

    def _fork_session_sync(
        self, source_session_id: str, fork_point_message_id: str | None = None
    ) -> str:
        try:
            with self._get_conn() as conn:
                source = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (source_session_id,)
                ).fetchone()
                if not source:
                    err_msg = f"Source session {source_session_id} not found"
                    raise ValueError(err_msg)

                new_session_id = generate_session_id()
                fork_count = conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE parent_session_id = ?",
                    (source_session_id,),
                ).fetchone()[0]
                new_title = f"{source['title']} (fork #{fork_count + 1})"

                conn.execute(
                    "INSERT INTO sessions (id, project_id, parent_session_id, fork_point_message_id, title) VALUES (?, ?, ?, ?, ?)",
                    (
                        new_session_id,
                        source["project_id"],
                        source_session_id,
                        fork_point_message_id,
                        new_title,
                    ),
                )

                if fork_point_message_id:
                    fork_msg = conn.execute(
                        "SELECT rowid FROM messages WHERE id = ? AND session_id = ?",
                        (fork_point_message_id, source_session_id),
                    ).fetchone()
                    source_msgs = conn.execute(
                        "SELECT * FROM messages WHERE session_id = ? AND rowid <= ? ORDER BY rowid ASC",
                        (source_session_id, fork_msg["rowid"]),
                    ).fetchall()
                else:
                    source_msgs = conn.execute(
                        "SELECT * FROM messages WHERE session_id = ? ORDER BY rowid ASC",
                        (source_session_id,),
                    ).fetchall()

                msg_count = 0
                for msg_row in source_msgs:
                    new_msg_id = generate_message_id()
                    conn.execute(
                        "INSERT INTO messages (id, session_id, role, created_at) VALUES (?, ?, ?, ?)",
                        (
                            new_msg_id,
                            new_session_id,
                            msg_row["role"],
                            msg_row["created_at"],
                        ),
                    )
                    source_parts = conn.execute(
                        "SELECT * FROM parts WHERE message_id = ?", (msg_row["id"],)
                    ).fetchall()
                    for part in source_parts:
                        conn.execute(
                            "INSERT INTO parts (id, message_id, type, data, created_at) VALUES (?, ?, ?, ?, ?)",
                            (
                                generate_part_id(),
                                new_msg_id,
                                part["type"],
                                part["data"],
                                part["created_at"],
                            ),
                        )
                    msg_count += 1

                conn.execute(
                    "UPDATE sessions SET message_count = ? WHERE id = ?",
                    (msg_count, new_session_id),
                )
                conn.commit()
                return new_session_id
        except Exception as e:
            logger.error(f"Error forking session {source_session_id}: {e}")
            raise

    async def fork_session(
        self, source_session_id: str, fork_point_message_id: str | None = None
    ) -> str:
        return await asyncio.to_thread(
            self._fork_session_sync, source_session_id, fork_point_message_id
        )

    # ─── 유틸리티 ───

    def _get_session_info_sync(self, session_id: str) -> dict[str, Any] | None:
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    return None
                token_est = self._estimate_session_tokens_sync(session_id)
                return {
                    **dict(row),
                    "session_id": row["id"],
                    "estimated_tokens": token_est,
                }
        except Exception:
            return None

    async def get_session_info(self, session_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_session_info_sync, session_id)

    def _get_all_sessions_sync(self) -> list[dict[str, Any]]:
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY updated_at DESC"
                ).fetchall()
                return [{"session_id": r["id"], **dict(r)} for r in rows]
        except Exception:
            return []

    async def get_all_sessions(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_all_sessions_sync)

    def _clear_session_sync(self, session_id: str) -> None:
        try:
            with self._get_conn() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                conn.commit()
        except Exception:
            pass

    async def clear_session(self, session_id: str) -> None:
        await asyncio.to_thread(self._clear_session_sync, session_id)

    async def delete_session(self, session_id: str) -> None:
        await asyncio.to_thread(self._clear_session_sync, session_id)

    def _log_system_event_sync(
        self,
        level: str,
        category: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO system_logs (level, category, message, metadata) VALUES (?, ?, ?, ?)",
                    (level, category, message, json.dumps(metadata or {})),
                )
                conn.commit()
        except Exception:
            pass

    async def log_system_event(
        self,
        level: str,
        category: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._log_system_event_sync, level, category, message, metadata
        )

    def _record_usage_sync(
        self,
        request_id: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int = 0,
        status: str = "success",
        endpoint: str = "chat",
    ) -> None:
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO usage_metrics (request_id, provider, model, endpoint, prompt_tokens, completion_tokens, total_tokens, latency_ms, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        provider,
                        model,
                        endpoint,
                        prompt_tokens,
                        completion_tokens,
                        prompt_tokens + completion_tokens,
                        latency_ms,
                        status,
                    ),
                )
                conn.commit()
        except Exception:
            pass

    async def record_usage(
        self,
        request_id: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int = 0,
        status: str = "success",
        endpoint: str = "chat",
    ) -> None:
        await asyncio.to_thread(
            self._record_usage_sync,
            request_id,
            provider,
            model,
            prompt_tokens,
            completion_tokens,
            latency_ms,
            status,
            endpoint,
        )

    async def get_usage_summary(self) -> list[dict[str, Any]]:
        def _get() -> list[dict[str, Any]]:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT provider, model, SUM(prompt_tokens) as prompt, SUM(completion_tokens) as completion, SUM(total_tokens) as total, COUNT(*) as count FROM usage_metrics GROUP BY provider, model"
                ).fetchall()
                return [dict(r) for r in rows]

        return await asyncio.to_thread(_get)

    def _update_provider_health_sync(
        self,
        provider: str,
        status: str,
        active: int,
        failed: int,
        last_error: str | None = None,
    ) -> None:
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO provider_health (provider, status, active_keys, failed_keys, last_error, updated_at) VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%f','now'))",
                    (provider, status, active, failed, last_error),
                )
                conn.commit()
        except Exception:
            pass

    async def update_provider_health(
        self,
        provider: str,
        status: str,
        active: int,
        failed: int,
        last_error: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._update_provider_health_sync,
            provider,
            status,
            active,
            failed,
            last_error,
        )

    def _update_daily_usage_sync(
        self, provider: str, model: str, tokens: int, is_error: bool = False
    ) -> None:
        try:
            with self._get_conn() as conn:
                day = datetime.now().strftime("%Y-%m-%d")
                conn.execute(
                    "INSERT INTO daily_usage (day, provider, model, total_tokens, request_count, error_count) VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT(day, provider, model) DO UPDATE SET total_tokens = total_tokens + excluded.total_tokens, request_count = request_count + 1, error_count = error_count + excluded.error_count",
                    (day, provider, model, tokens, 1 if is_error else 0),
                )
                conn.commit()
        except Exception:
            pass

    async def update_daily_usage(
        self, provider: str, model: str, tokens: int, is_error: bool = False
    ) -> None:
        await asyncio.to_thread(
            self._update_daily_usage_sync, provider, model, tokens, is_error
        )

    def _get_web_cache_sync(self, url: str, ttl_hours: int) -> str | None:
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT content FROM web_content_cache WHERE url = ? AND (julianday('now') - julianday(cached_at)) * 24 < ?",
                    (url, ttl_hours),
                ).fetchone()
                return row[0] if row else None
        except Exception:
            return None

    async def get_web_cache(self, url: str, ttl_hours: int = 24) -> str | None:
        return await asyncio.to_thread(self._get_web_cache_sync, url, ttl_hours)

    def _set_web_cache_sync(self, url: str, content: str, mode: str) -> None:
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO web_content_cache (url, content, mode, cached_at) VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%f','now'))",
                    (url, content, mode),
                )
                conn.commit()
        except Exception:
            pass

    async def set_web_cache(self, url: str, content: str, mode: str) -> None:
        await asyncio.to_thread(self._set_web_cache_sync, url, content, mode)

    def _record_scraping_sync(
        self,
        url: str,
        status: str,
        chars: int,
        latency: int,
        query: str | None = None,
        summary: str | None = None,
    ) -> None:
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO scraping_metrics (url, query, status, chars_count, response_summary, latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        url,
                        query,
                        status,
                        chars,
                        summary,
                        latency,
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
        except Exception:
            pass

    async def record_scraping(
        self,
        url: str,
        status: str,
        chars: int,
        latency: int,
        query: str | None = None,
        summary: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._record_scraping_sync, url, status, chars, latency, query, summary
        )

    async def get_scraping_summary(self) -> dict[str, Any]:
        def _get() -> dict[str, Any]:
            with self._get_conn() as conn:
                total = conn.execute(
                    "SELECT count(*) FROM scraping_metrics"
                ).fetchone()[0]
                hits = conn.execute(
                    "SELECT count(*) FROM scraping_metrics WHERE status = 'cache_hit'"
                ).fetchone()[0]
                fails = conn.execute(
                    "SELECT count(*) FROM scraping_metrics WHERE status = 'failed'"
                ).fetchone()[0]
                return {"total": total, "hits": hits, "fails": fails}

        return await asyncio.to_thread(_get)

    async def get_recent_scraping(self, limit: int = 20) -> list[dict[str, Any]]:
        """최근 웹 스크래핑 내역 조회."""

        def _get() -> list[dict[str, Any]]:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM scraping_metrics ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]

        return await asyncio.to_thread(_get)

    async def get_history(self, session_id: str) -> list[ChatMessage]:
        return await self.load_context(session_id)

    # ─── New methods for IKeyManager/IAdmin alignment ───

    def _get_setting_sync(self, key: str, default: Any = None) -> Any:
        try:
            with self._get_conn() as conn:
                res = conn.execute(
                    "SELECT value FROM system_settings WHERE key = ?", (key,)
                ).fetchone()
                return json.loads(res["value"]) if res else default
        except Exception:
            return default

    async def get_setting(self, key: str, default: Any = None) -> Any:
        return await asyncio.to_thread(self._get_setting_sync, key, default)

    def _set_setting_sync(self, key: str, value: Any) -> None:
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO system_settings (key, value, updated_at) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%f','now'))",
                    (key, json.dumps(value)),
                )
                conn.commit()
        except Exception:
            pass

    async def set_setting(self, key: str, value: Any) -> None:
        await asyncio.to_thread(self._set_setting_sync, key, value)

    async def get_recent_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        def _get() -> list[dict[str, Any]]:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM system_logs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]

        return await asyncio.to_thread(_get)

    async def get_all_provider_health(self) -> list[dict[str, Any]]:
        def _get() -> list[dict[str, Any]]:
            with self._get_conn() as conn:
                rows = conn.execute("SELECT * FROM provider_health").fetchall()
                return [dict(r) for r in rows]

        return await asyncio.to_thread(_get)

    async def clear_system_logs(self) -> None:
        """시스템 로그 전체 삭제."""

        def _clear() -> None:
            with self._get_conn() as conn:
                conn.execute("DELETE FROM system_logs")
                conn.commit()

        await asyncio.to_thread(_clear)

    async def cleanup_old_sessions(self, days: int = 30) -> int:
        """지정된 기간보다 오래된 세션 삭제."""

        def _cleanup() -> int:
            with self._get_conn() as conn:
                # updated_at 기준
                cursor = conn.execute(
                    "DELETE FROM sessions WHERE julianday('now') - julianday(updated_at) > ?",
                    (days,),
                )
                count: int = cursor.rowcount
                conn.commit()
                return count

        return await asyncio.to_thread(_cleanup)
