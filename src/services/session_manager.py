import json
import logging
from typing import Any

import duckdb

from src.core.config import settings
from src.domain.models import ChatMessage

logger = logging.getLogger(__name__)

DB_PATH = "gateway_sessions.duckdb"


class SessionManager:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        try:
            with duckdb.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id VARCHAR PRIMARY KEY,
                        messages JSON,
                        summary TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
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
        except Exception as e:
            logger.error(f"Failed to initialize DuckDB: {e}")

    def get_setting(self, key: str, default: Any = None) -> Any:
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

    def set_setting(self, key: str, value: Any) -> None:
        try:
            with duckdb.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                    [key, json.dumps(value)],
                )
        except Exception as e:
            logger.error(f"Error saving setting {key}: {e}")

    def _enforce_limit(self) -> None:
        try:
            with duckdb.connect(self.db_path) as conn:
                res = conn.execute("SELECT count(*) FROM sessions").fetchone()
                if res:
                    count = res[0]
                    if count >= settings.max_sessions:
                        conn.execute(
                            """
                            DELETE FROM sessions
                            WHERE session_id IN (
                                SELECT session_id FROM sessions
                                ORDER BY updated_at ASC
                                LIMIT ?
                            )
                        """,
                            [count - settings.max_sessions + 1],
                        )
                        logger.info(
                            f"Evicted {count - settings.max_sessions + 1} old sessions."
                        )
        except Exception as e:
            logger.error(f"Error enforcing session limit: {e}")

    def get_history(self, session_id: str) -> list[ChatMessage]:
        try:
            with duckdb.connect(self.db_path) as conn:
                res = conn.execute(
                    "SELECT messages FROM sessions WHERE session_id = ?", [session_id]
                ).fetchone()

                if res and res[0]:
                    msgs_data = json.loads(res[0])
                    return [ChatMessage(**m) for m in msgs_data]
        except Exception as e:
            logger.error(f"Error retrieving history for {session_id}: {e}")
        return []

    def save_message(self, session_id: str, role: str, content: Any) -> None:
        try:
            self._enforce_limit()
            history = self.get_history(session_id)
            history.append(ChatMessage(role=role, content=content, name=None))

            messages_json = json.dumps([m.model_dump() for m in history])

            with duckdb.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sessions (session_id, messages, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                    [session_id, messages_json],
                )
        except Exception as e:
            logger.error(f"Error saving message for {session_id}: {e}")

    def clear_session(self, session_id: str) -> None:
        try:
            with duckdb.connect(self.db_path) as conn:
                conn.execute("DELETE FROM sessions WHERE session_id = ?", [session_id])
        except Exception as e:
            logger.error(f"Error clearing session {session_id}: {e}")

    def get_all_sessions(self) -> list[dict[str, Any]]:
        try:
            with duckdb.connect(self.db_path) as conn:
                res = conn.execute(
                    "SELECT session_id, updated_at FROM sessions ORDER BY updated_at DESC"
                ).fetchall()
                return [{"id": r[0], "updated_at": r[1]} for r in res]
        except Exception as e:
            logger.error(f"Error listing sessions: {e}")
            return []
