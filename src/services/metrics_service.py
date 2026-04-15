import asyncio
import logging
from typing import Any

from src.domain.interfaces import ISessionManager

logger = logging.getLogger(__name__)


class MetricsService:
    def __init__(self, session_manager: ISessionManager):
        self.session_manager = session_manager
        self._lock = asyncio.Lock()
        self._total_requests = 0
        self._total_tokens = 0
        self._error_count = 0
        self._provider_stats: dict[str, dict[str, int]] = {}

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
        async with self._lock:
            self._total_requests += 1
            tokens = prompt_tokens + completion_tokens
            self._total_tokens += tokens

            if "error" in status.lower():
                self._error_count += 1

            if provider not in self._provider_stats:
                self._provider_stats[provider] = {
                    "tokens": 0,
                    "requests": 0,
                    "errors": 0,
                }

            self._provider_stats[provider]["tokens"] += tokens
            self._provider_stats[provider]["requests"] += 1
            if "error" in status.lower():
                self._provider_stats[provider]["errors"] += 1

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
