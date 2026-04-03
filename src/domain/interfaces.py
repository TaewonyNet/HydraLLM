from abc import ABC, abstractmethod
from typing import Any

from .enums import ModelType, ProviderType, TierType
from .models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    RoutingDecision,
    SessionMessage,
)


class ISessionManager(ABC):
    @abstractmethod
    async def create_session(
        self, session_id: str | None = None, title: str | None = None
    ) -> str:
        ...

    @abstractmethod
    async def save_message(
        self,
        session_id: str,
        role: str,
        content: Any,
        parts: list[dict[str, Any]] | None = None,
    ) -> str:
        ...

    @abstractmethod
    async def add_part(
        self,
        message_id: str,
        part_type: str,
        data: dict[str, Any],
    ) -> str:
        ...

    @abstractmethod
    async def load_context(self, session_id: str) -> list[ChatMessage]:
        ...

    @abstractmethod
    async def load_messages_with_parts(self, session_id: str) -> list[SessionMessage]:
        ...

    @abstractmethod
    def is_overflow(self, session_id: str) -> bool:
        ...

    @abstractmethod
    async def compact(self, session_id: str, compressor: Any) -> None:
        ...

    @abstractmethod
    async def fork_session(
        self,
        source_session_id: str,
        fork_point_message_id: str | None = None,
    ) -> str:
        ...

    @abstractmethod
    async def get_session_info(self, session_id: str) -> dict[str, Any] | None:
        ...

    @abstractmethod
    async def get_all_sessions(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    async def clear_session(self, session_id: str) -> None:
        ...

    @abstractmethod
    async def get_setting(self, key: str, default: Any = None) -> Any:
        ...

    @abstractmethod
    async def set_setting(self, key: str, value: Any) -> None:
        ...

    @abstractmethod
    async def log_system_event(
        self,
        level: str,
        category: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    async def get_usage_summary(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    async def get_recent_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    async def get_all_provider_health(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    async def update_daily_usage(
        self, provider: str, model: str, tokens: int, is_error: bool = False
    ) -> None:
        ...

    @abstractmethod
    async def get_web_cache(self, url: str, ttl_hours: int = 24) -> str | None:
        ...

    @abstractmethod
    async def set_web_cache(self, url: str, content: str, mode: str) -> None:
        ...

    @abstractmethod
    async def record_scraping(
        self, url: str, status: str, chars: int, latency: int
    ) -> None:
        ...

    @abstractmethod
    async def get_scraping_summary(self) -> dict[str, Any]:
        ...

    @abstractmethod
    async def update_provider_health(
        self,
        provider: str,
        status: str,
        active: int,
        failed: int,
        last_error: str | None = None,
    ) -> None:
        ...


class ILLMProvider(ABC):
    @abstractmethod
    async def generate(
        self,
        request: ChatRequest,
        api_key: str,
    ) -> ChatResponse:
        pass

    @abstractmethod
    def get_supported_models(self) -> list[ModelType]:
        pass

    @abstractmethod
    def is_multimodal(self) -> bool:
        pass

    @abstractmethod
    def get_max_tokens(self) -> int:
        pass

    @abstractmethod
    async def discover_models(self) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    async def probe_key(self, api_key: str) -> dict[str, Any]:
        pass


class IContextAnalyzer(ABC):
    @abstractmethod
    async def analyze(
        self,
        request: ChatRequest,
        available_tiers: dict[ProviderType, set[str]] | None = None,
    ) -> RoutingDecision:
        pass

    @abstractmethod
    def get_supported_models_info(self) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def get_all_discovered_models_info(self) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def register_model(
        self,
        model_name: str,
        provider: ProviderType | Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        pass


class IKeyManager(ABC):
    @abstractmethod
    async def get_next_key(
        self, provider: ProviderType, min_tier: TierType = TierType.FREE
    ) -> str:
        pass

    @abstractmethod
    async def report_success(
        self,
        provider: ProviderType,
        api_key: str,
    ) -> None:
        pass

    @abstractmethod
    async def report_failure(
        self,
        provider: ProviderType,
        api_key: str,
        error: Exception,
    ) -> None:
        pass

    @abstractmethod
    def get_key_status(self) -> dict[ProviderType, dict[str, Any]]:
        pass


class IRouter(ABC):
    @abstractmethod
    async def route_request(
        self,
        request: ChatRequest,
    ) -> ChatResponse:
        pass

    @abstractmethod
    async def get_status(self) -> dict[str, Any]:
        pass

    @abstractmethod
    def get_supported_models(self) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def get_all_models(self) -> list[dict[str, Any]]:
        pass
