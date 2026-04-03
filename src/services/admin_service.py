import logging
from typing import Any

from src.domain.interfaces import ISessionManager

logger = logging.getLogger(__name__)


class AdminService:
    def __init__(self, session_manager: ISessionManager):
        self.session_manager = session_manager

    async def get_dashboard_data(self) -> dict[str, Any]:
        try:
            usage_summary = await self.session_manager.get_usage_summary()
            recent_logs = await self.session_manager.get_recent_logs(limit=30)
            provider_health = await self.session_manager.get_all_provider_health()
            scraping_summary = await self.session_manager.get_scraping_summary()

            total_tokens = sum(u.get("total", 0) for u in usage_summary)
            request_count = sum(u.get("count", 0) for u in usage_summary)

            return {
                "total_tokens": total_tokens,
                "total_requests": request_count,
                "providers": usage_summary,
                "health": provider_health,
                "scraping": scraping_summary,
                "recent_logs": recent_logs,
            }
        except Exception as e:
            logger.error(f"Error gathering dashboard data: {e}")
            return {
                "total_tokens": 0,
                "total_requests": 0,
                "providers": [],
                "health": [],
                "recent_logs": [],
                "error": str(e),
            }

    async def log_auth_event(
        self, success: bool, details: str, metadata: dict | None = None
    ) -> None:
        level = "INFO" if success else "WARNING"
        await self.session_manager.log_system_event(
            level=level,
            category="AUTH",
            message=f"Auth {'success' if success else 'failed'}: {details}",
            metadata=metadata,
        )

    async def log_connection_error(self, provider: str, error: str) -> None:
        await self.session_manager.log_system_event(
            level="ERROR",
            category="CONNECTION",
            message=f"Connection failed for {provider}: {error}",
            metadata={"provider": provider, "error": error},
        )
