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

        # 상세 스크래핑 내역 추가
        recent_scraping: list[dict[str, Any]] = []
        if hasattr(self.session_manager, "get_recent_scraping"):
            recent_scraping = await self.session_manager.get_recent_scraping(limit=20)

        provider_health = await self.session_manager.get_all_provider_health()

        total_tokens = sum(u.get("total", 0) for u in usage_summary)
        request_count = sum(u.get("count", 0) for u in usage_summary)

        # UI 호환: playground (index.html) 는 `scraping.recent`, admin.html 은
        # `recent_scraping` 을 사용한다. 두 키를 모두 채워 둔다.
        scraping_block = dict(scraping_summary) if scraping_summary else {}
        scraping_block["recent"] = recent_scraping

        return {
            "total_tokens": total_tokens,
            "total_requests": request_count,
            "providers": usage_summary,
            "health": provider_health,
            "scraping": scraping_block,
            "recent_scraping": recent_scraping,
        }

    async def get_dashboard_data(self) -> dict[str, Any]:
        stats = await self.get_stats()
        logs = await self.get_logs(limit=100)
        stats["recent_logs"] = logs
        return stats

    async def delete_session(self, session_id: str) -> None:
        await self.session_manager.delete_session(session_id)

    async def clear_logs(self) -> None:
        if hasattr(self.session_manager, "clear_system_logs"):
            await self.session_manager.clear_system_logs()

    async def cleanup_sessions(self, days: int | None = None) -> int:
        from src.core.config import settings

        retention = days if days is not None else settings.session_retention_days
        # 최대 1년(365일) 제한
        retention = min(max(1, retention), 365)
        if hasattr(self.session_manager, "cleanup_old_sessions"):
            return await self.session_manager.cleanup_old_sessions(retention)
        return 0

    async def get_onboarding_status(self) -> dict[str, Any]:
        """UI 호환성을 위한 온보딩 상태 조회."""
        from src.core.config import settings

        # 온보딩 화면이 렌더할 가용 모델 목록을 함께 제공.
        # session_manager 가 인터페이스로 노출하지 않으므로, 설정에 저장된
        # enabled_models 또는 빈 리스트를 반환한다 (엄격한 UX 보장은 UI 가 담당).
        available: list[dict[str, Any]] = []
        try:
            gateway = getattr(self, "_gateway", None)
            if gateway is not None and hasattr(gateway, "get_supported_models"):
                available = gateway.get_supported_models()
        except Exception:
            available = []

        return {
            "completed": settings.onboarding_completed,
            "enabled_models": settings.enabled_models,
            "available_models": available,
        }

    async def save_onboarding(self, payload: dict[str, Any]) -> dict[str, Any]:
        """온보딩 완료 처리 및 enabled_models 저장."""
        from src.core.config import settings

        enabled = payload.get("enabled_models") or []
        if not isinstance(enabled, list):
            enabled = []
        settings.enabled_models = enabled
        settings.onboarding_completed = True

        if hasattr(self.session_manager, "set_setting"):
            await self.session_manager.set_setting("enabled_models", enabled)
            await self.session_manager.set_setting("onboarding_completed", True)
        return {"status": "success", "enabled_models": enabled}

    async def get_settings(self) -> dict[str, Any]:
        """UI 호환성을 위한 설정 정보 조회."""
        from src.core.config import settings
        return {
            "session_retention_days": settings.session_retention_days,
            "max_tokens_fast_model": settings.max_tokens_fast_model,
            "provider_priority": settings.provider_priority,
            "locale": getattr(settings, "locale", "ko"),
            "debug_comm_log": bool(getattr(settings, "debug_comm_log", False)),
        }

    async def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """UI 일반 설정 저장 (locale / debug_comm_log).

        보관 기간 등 민감 항목은 별도 엔드포인트로 제한한다.
        """
        from src.core.config import settings

        changes: dict[str, Any] = {}
        if "locale" in payload:
            loc = str(payload["locale"])[:8]
            if loc in ("ko", "en"):
                settings.locale = loc
                changes["locale"] = loc
        if "debug_comm_log" in payload:
            settings.debug_comm_log = bool(payload["debug_comm_log"])
            changes["debug_comm_log"] = settings.debug_comm_log

        if changes and hasattr(self.session_manager, "set_setting"):
            for k, v in changes.items():
                await self.session_manager.set_setting(k, v)

        return {"status": "success", "changed": changes}

    async def get_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        """세션 메시지 이력 조회 (UI 복원용)."""
        if hasattr(self.session_manager, "load_messages_with_parts"):
            msgs = await self.session_manager.load_messages_with_parts(session_id)
            result: list[dict[str, Any]] = []
            for m in msgs:
                # text 파트를 합쳐 content 생성, web_* 파트는 별도로 보존
                text_chunks: list[str] = []
                web_parts: list[dict[str, Any]] = []
                for p in m.parts:
                    data = p.data if isinstance(p.data, dict) else {}
                    if p.type == "text":
                        text_chunks.append(str(data.get("text", "")))
                    elif p.type in ("web_search", "web_fetch"):
                        web_parts.append({"type": p.type, "data": data})
                result.append(
                    {
                        "id": m.id,
                        "role": m.role,
                        "content": "".join(text_chunks),
                        "parts": web_parts,
                        "created_at": m.created_at,
                    }
                )
            return result
        return []

    async def get_session_info(self, session_id: str) -> dict[str, Any] | None:
        if hasattr(self.session_manager, "get_session_info"):
            return await self.session_manager.get_session_info(session_id)
        return None
