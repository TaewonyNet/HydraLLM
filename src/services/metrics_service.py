import logging
from typing import Any

from src.domain.interfaces import ISessionManager

logger = logging.getLogger(__name__)


class MetricsService:
    def __init__(self, session_manager: ISessionManager):
        # 집계 통계는 SQLite(usage/daily_usage)가 단일 진실원이며 get_summary 가 거기서
        # 읽는다. 과거 인메모리 카운터는 쓰기만 하고 아무도 읽지 않는 dead 상태여서 제거했다.
        self.session_manager = session_manager

    async def record_request(
        self,
        request_id: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        status: str = "success",
        endpoint: str = "chat",
    ) -> None:
        tokens = prompt_tokens + completion_tokens
        is_error = "error" in status.lower()

        await self.session_manager.record_usage(
            request_id=request_id,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            status=status,
            endpoint=endpoint,
        )
        await self.session_manager.update_daily_usage(
            provider=provider, model=model, tokens=tokens, is_error=is_error
        )

    async def get_summary(self) -> dict[str, Any]:
        usage_summary = await self.session_manager.get_usage_summary()
        provider_health = await self.session_manager.get_all_provider_health()

        return {
            "total_tokens": sum(u.get("total", 0) for u in usage_summary),
            "total_requests": sum(u.get("count", 0) for u in usage_summary),
            "providers": usage_summary,
            "health": provider_health,
        }
