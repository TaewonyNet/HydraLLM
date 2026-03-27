import asyncio
import hashlib
import json
import logging
import subprocess
import uuid
from typing import Any

import duckdb

from src.core.config import settings
from src.domain.models import ChatMessage

logger = logging.getLogger(__name__)

DB_PATH = "gateway_sessions.duckdb"


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


def generate_session_id(project_id: str | None = None) -> str:
    """project_id 기반 세션 ID 생성."""
    pid = project_id or _get_project_id()
    short_uuid = uuid.uuid4().hex[:8]
    return f"{pid}-{short_uuid}"


class SessionManager:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.project_id = _get_project_id()
        self._init_db()

    # ─── DB 초기화 ───

    def _init_db(self) -> None:
        try:
            with duckdb.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id VARCHAR PRIMARY KEY,
                        project_id VARCHAR,
                        title VARCHAR,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY,
                        session_id VARCHAR NOT NULL,
                        role VARCHAR NOT NULL,
                        content VARCHAR NOT NULL,
                        is_summary BOOLEAN DEFAULT FALSE,
                        token_estimate INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """
                )
                conn.execute(
                    """
                    CREATE SEQUENCE IF NOT EXISTS msg_id_seq START 1
                """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS system_settings (
                        key VARCHAR PRIMARY KEY,
                        value JSON
                    )
                """
                )
                # 기존 sessions 테이블에 messages JSON 컬럼이 있다면 마이그레이션
                self._migrate_if_needed(conn)

                cols = [
                    row[0]
                    for row in conn.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = 'sessions'"
                    ).fetchall()
                ]
                if "project_id" not in cols:
                    conn.execute("ALTER TABLE sessions ADD COLUMN project_id VARCHAR")
                    logger.info("Added project_id column to sessions table")
                if "title" not in cols:
                    conn.execute(
                        "ALTER TABLE sessions ADD COLUMN title VARCHAR DEFAULT 'New Session'"
                    )
                    logger.info("Added title column to sessions table")
                if "created_at" not in cols:
                    conn.execute(
                        "ALTER TABLE sessions ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                    )
                    logger.info("Added created_at column to sessions table")

        except Exception as e:
            logger.error(f"Failed to initialize DuckDB: {e}")

    def _migrate_if_needed(self, conn: duckdb.DuckDBPyConnection) -> None:
        """기존 JSON blob 방식 세션 데이터를 개별 메시지 레코드로 마이그레이션."""
        try:
            cols = [
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'sessions'"
                ).fetchall()
            ]
            if "messages" in cols:
                rows = conn.execute(
                    "SELECT session_id, messages FROM sessions WHERE messages IS NOT NULL"
                ).fetchall()
                for session_id, msgs_json in rows:
                    if not msgs_json:
                        continue
                    msgs = json.loads(msgs_json)
                    for msg in msgs:
                        content = msg.get("content", "")
                        if isinstance(content, (dict, list)):
                            content = json.dumps(content, ensure_ascii=False)
                        token_est = max(1, len(str(content)) // 4)
                        conn.execute(
                            "INSERT INTO messages (id, session_id, role, content, token_estimate) "
                            "VALUES (nextval('msg_id_seq'), ?, ?, ?, ?)",
                            [
                                session_id,
                                msg.get("role", "user"),
                                str(content),
                                token_est,
                            ],
                        )
                    # project_id 채우기
                    conn.execute(
                        "UPDATE sessions SET project_id = ? WHERE session_id = ? AND project_id IS NULL",
                        [self.project_id, session_id],
                    )
                # messages 컬럼 제거
                conn.execute("ALTER TABLE sessions DROP COLUMN messages")
                if "summary" in cols:
                    conn.execute("ALTER TABLE sessions DROP COLUMN summary")
                logger.info(f"Migrated {len(rows)} sessions to message-level storage")
        except Exception as e:
            logger.debug(f"Migration check: {e}")

    # ─── 세션 생성/조회 ───

    def _create_session_sync(
        self, session_id: str | None = None, title: str | None = None
    ) -> str:
        sid = session_id or generate_session_id(self.project_id)
        try:
            with duckdb.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO sessions (session_id, project_id, title) VALUES (?, ?, ?)",
                    [sid, self.project_id, title or "New Session"],
                )
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
            with duckdb.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?", [session_id]
                ).fetchone()
                if not row:
                    conn.execute(
                        "INSERT INTO sessions (session_id, project_id, title) VALUES (?, ?, ?)",
                        [session_id, self.project_id, "Auto Session"],
                    )
        except Exception as e:
            logger.error(f"Error ensuring session {session_id}: {e}")

    # ─── 메시지 저장 ───

    def _save_message_sync(
        self,
        session_id: str,
        role: str,
        content: Any,
        is_summary: bool = False,
    ) -> None:
        try:
            self._ensure_session_sync(session_id)
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            content_str = str(content)
            token_est = max(1, len(content_str) // 4)

            with duckdb.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO messages (id, session_id, role, content, is_summary, token_estimate) "
                    "VALUES (nextval('msg_id_seq'), ?, ?, ?, ?, ?)",
                    [session_id, role, content_str, is_summary, token_est],
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                    [session_id],
                )
        except Exception as e:
            logger.error(f"Error saving message for {session_id}: {e}")

    async def save_message(
        self,
        session_id: str,
        role: str,
        content: Any,
        is_summary: bool = False,
    ) -> None:
        await asyncio.to_thread(
            self._save_message_sync, session_id, role, content, is_summary
        )

    # ─── 컨텍스트 로드 (compaction 경계 이후 메시지만) ───

    def _load_context_sync(self, session_id: str) -> list[ChatMessage]:
        """세션 히스토리를 로드. 마지막 summary 이후의 메시지만 반환."""
        try:
            with duckdb.connect(self.db_path) as conn:
                # 마지막 compaction summary의 id 찾기
                boundary = conn.execute(
                    "SELECT MAX(id) FROM messages "
                    "WHERE session_id = ? AND is_summary = TRUE",
                    [session_id],
                ).fetchone()
                boundary_id = boundary[0] if boundary and boundary[0] else 0

                # boundary 이후 메시지 로드 (summary 포함)
                rows = conn.execute(
                    "SELECT role, content, is_summary FROM messages "
                    "WHERE session_id = ? AND id >= ? ORDER BY id ASC",
                    [session_id, boundary_id] if boundary_id > 0 else [session_id, 0],
                ).fetchall()

                messages: list[ChatMessage] = []
                for role, content, is_summary in rows:
                    messages.append(ChatMessage(role=role, content=content, name=None))
                return messages
        except Exception as e:
            logger.error(f"Error loading context for {session_id}: {e}")
            return []

    async def load_context(self, session_id: str) -> list[ChatMessage]:
        return await asyncio.to_thread(self._load_context_sync, session_id)

    # ─── 토큰 overflow 감지 ───

    def _estimate_session_tokens_sync(self, session_id: str) -> int:
        """현재 세션의 활성 토큰 추정치 반환."""
        try:
            with duckdb.connect(self.db_path) as conn:
                boundary = conn.execute(
                    "SELECT MAX(id) FROM messages "
                    "WHERE session_id = ? AND is_summary = TRUE",
                    [session_id],
                ).fetchone()
                boundary_id = boundary[0] if boundary and boundary[0] else 0

                result = conn.execute(
                    "SELECT COALESCE(SUM(token_estimate), 0) FROM messages "
                    "WHERE session_id = ? AND id >= ?",
                    [session_id, boundary_id] if boundary_id > 0 else [session_id, 0],
                ).fetchone()
                return result[0] if result else 0
        except Exception as e:
            logger.error(f"Error estimating tokens for {session_id}: {e}")
            return 0

    def is_overflow(self, session_id: str) -> bool:
        """compaction 임계값 초과 여부."""
        tokens = self._estimate_session_tokens_sync(session_id)
        return tokens > settings.session_compact_threshold

    # ─── Compaction: prune + 요약 + 경계 마커 ───

    def _compact_sync(self, session_id: str, compressor: Any) -> None:
        """오래된 메시지를 요약으로 치환하고 경계 마커 삽입."""
        try:
            with duckdb.connect(self.db_path) as conn:
                # 현재 활성 메시지 로드
                boundary = conn.execute(
                    "SELECT MAX(id) FROM messages "
                    "WHERE session_id = ? AND is_summary = TRUE",
                    [session_id],
                ).fetchone()
                boundary_id = boundary[0] if boundary and boundary[0] else 0

                rows = conn.execute(
                    "SELECT id, role, content FROM messages "
                    "WHERE session_id = ? AND id >= ? ORDER BY id ASC",
                    [session_id, boundary_id] if boundary_id > 0 else [session_id, 0],
                ).fetchall()

                if len(rows) <= settings.session_recent_window:
                    return

                recent_window = settings.session_recent_window
                old_msgs = rows[:-recent_window]
                old_ids = [r[0] for r in old_msgs]

                # 요약 대상 텍스트 구성
                old_text = "\n".join([f"{r[1]}: {r[2][:500]}" for r in old_msgs])

                # 요약 생성
                summary = compressor.compress(
                    old_text,
                    instruction="Summarize this conversation history concisely. Keep key facts, decisions, and context.",
                    target_token=800,
                )

                # 오래된 메시지 삭제
                if old_ids:
                    placeholders = ",".join(["?"] * len(old_ids))
                    conn.execute(
                        f"DELETE FROM messages WHERE id IN ({placeholders})",
                        old_ids,
                    )

                # 요약 메시지를 경계 마커로 삽입
                token_est = max(1, len(summary) // 4)
                conn.execute(
                    "INSERT INTO messages (id, session_id, role, content, is_summary, token_estimate) "
                    "VALUES (nextval('msg_id_seq'), ?, 'system', ?, TRUE, ?)",
                    [session_id, f"[Session Summary]\n{summary}", token_est],
                )

                logger.info(
                    f"Compacted session {session_id}: {len(old_ids)} messages → 1 summary"
                )
        except Exception as e:
            logger.error(f"Error compacting session {session_id}: {e}")

    async def compact(self, session_id: str, compressor: Any) -> None:
        await asyncio.to_thread(self._compact_sync, session_id, compressor)

    # ─── 세션 관리 유틸리티 ───

    def _get_session_info_sync(self, session_id: str) -> dict[str, Any] | None:
        try:
            with duckdb.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT session_id, project_id, title, created_at, updated_at "
                    "FROM sessions WHERE session_id = ?",
                    [session_id],
                ).fetchone()
                if not row:
                    return None

                msg_count = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                    [session_id],
                ).fetchone()
                token_est = conn.execute(
                    "SELECT COALESCE(SUM(token_estimate), 0) FROM messages WHERE session_id = ?",
                    [session_id],
                ).fetchone()

                return {
                    "session_id": row[0],
                    "project_id": row[1],
                    "title": row[2],
                    "created_at": str(row[3]),
                    "updated_at": str(row[4]),
                    "message_count": msg_count[0] if msg_count else 0,
                    "estimated_tokens": token_est[0] if token_est else 0,
                }
        except Exception as e:
            logger.error(f"Error getting session info {session_id}: {e}")
            return None

    async def get_session_info(self, session_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_session_info_sync, session_id)

    def _get_all_sessions_sync(self) -> list[dict[str, Any]]:
        try:
            with duckdb.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT s.session_id, s.project_id, s.title, s.updated_at, "
                    "COUNT(m.id) as msg_count "
                    "FROM sessions s LEFT JOIN messages m ON s.session_id = m.session_id "
                    "GROUP BY s.session_id, s.project_id, s.title, s.updated_at "
                    "ORDER BY s.updated_at DESC"
                ).fetchall()
                return [
                    {
                        "session_id": r[0],
                        "project_id": r[1],
                        "title": r[2],
                        "updated_at": str(r[3]),
                        "message_count": r[4],
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"Error listing sessions: {e}")
            return []

    async def get_all_sessions(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_all_sessions_sync)

    def _clear_session_sync(self, session_id: str) -> None:
        try:
            with duckdb.connect(self.db_path) as conn:
                conn.execute("DELETE FROM messages WHERE session_id = ?", [session_id])
                conn.execute("DELETE FROM sessions WHERE session_id = ?", [session_id])
        except Exception as e:
            logger.error(f"Error clearing session {session_id}: {e}")

    async def clear_session(self, session_id: str) -> None:
        await asyncio.to_thread(self._clear_session_sync, session_id)

    def _enforce_limit(self) -> None:
        try:
            with duckdb.connect(self.db_path) as conn:
                res = conn.execute("SELECT count(*) FROM sessions").fetchone()
                if res and res[0] >= settings.max_sessions:
                    excess = res[0] - settings.max_sessions + 1
                    old_ids = conn.execute(
                        "SELECT session_id FROM sessions ORDER BY updated_at ASC LIMIT ?",
                        [excess],
                    ).fetchall()
                    for (sid,) in old_ids:
                        conn.execute("DELETE FROM messages WHERE session_id = ?", [sid])
                        conn.execute("DELETE FROM sessions WHERE session_id = ?", [sid])
                    logger.info(f"Evicted {len(old_ids)} old sessions")
        except Exception as e:
            logger.error(f"Error enforcing session limit: {e}")

    # ─── system_settings (기존 호환) ───

    def _get_setting_sync(self, key: str, default: Any = None) -> Any:
        try:
            with duckdb.connect(self.db_path) as conn:
                res = conn.execute(
                    "SELECT value FROM system_settings WHERE key = ?", [key]
                ).fetchone()
                if res:
                    return json.loads(res[0])
        except Exception as e:
            logger.error(f"Error retrieving setting {key}: {e}")
        return default

    async def get_setting(self, key: str, default: Any = None) -> Any:
        return await asyncio.to_thread(self._get_setting_sync, key, default)

    def _set_setting_sync(self, key: str, value: Any) -> None:
        try:
            with duckdb.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                    [key, json.dumps(value)],
                )
        except Exception as e:
            logger.error(f"Error saving setting {key}: {e}")

    async def set_setting(self, key: str, value: Any) -> None:
        await asyncio.to_thread(self._set_setting_sync, key, value)

    # ─── 하위 호환: get_history (load_context로 대체) ───

    async def get_history(self, session_id: str) -> list[ChatMessage]:
        return await self.load_context(session_id)
