import logging
from typing import Any

from src.domain.interfaces import ISessionManager

logger = logging.getLogger(__name__)


class AdminService:
    def __init__(self, session_manager: ISessionManager):
        self.session_manager = session_manager

    async def get_sessions(self) -> list[dict[str, Any]]:
        try:
            return await self.session_manager.get_all_sessions()
        except Exception as e:
            logger.error(f"Error getting sessions: {e}")
            return []

    async def create_session(self) -> str:
        return await self.session_manager.create_session()

    async def get_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.session_manager.get_recent_logs(limit=limit)

    async def get_stats(self) -> dict[str, Any]:
        usage_summary = await self.session_manager.get_usage_summary()
        scraping_summary = await self.session_manager.get_scraping_summary()
        provider_health = await self.session_manager.get_all_provider_health()

        total_tokens = sum(u.get("total", 0) for u in usage_summary)
        request_count = sum(u.get("count", 0) for u in usage_summary)

        return {
            "total_tokens": total_tokens,
            "total_requests": request_count,
            "providers": usage_summary,
            "health": provider_health,
            "scraping": scraping_summary,
        }

    async def get_dashboard_data(self) -> dict[str, Any]:
        stats = await self.get_stats()
        logs = await self.get_logs(limit=30)
        stats["recent_logs"] = logs
        return stats

    async def delete_session(self, session_id: str) -> None:
        await self.session_manager.delete_session(session_id)
