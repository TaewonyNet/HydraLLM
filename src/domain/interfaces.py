from abc import ABC, abstractmethod
from typing import Any

from .enums import ModelType, ProviderType
from .models import ChatRequest, ChatResponse, RoutingDecision


class ILLMProvider(ABC):
    """
    Abstract base class for LLM providers.
    """

    @abstractmethod
    async def generate(
        self,
        request: ChatRequest,
        api_key: str,
    ) -> ChatResponse:
        """
        Generate a response from the LLM provider.

        Args:
            request: The chat completion request
            api_key: The API key to use for this request

        Returns:
            The generated chat response

        Raises:
            ProviderError: If the provider returns an error
        """
        pass

    @abstractmethod
    def get_supported_models(self) -> list[ModelType]:
        """
        Get the list of supported models for this provider.

        Returns:
            List of supported model types
        """
        pass

    @abstractmethod
    def is_multimodal(self) -> bool:
        """
        Check if this provider supports multimodal inputs.

        Returns:
            True if the provider supports images, False otherwise
        """
        pass

    @abstractmethod
    def get_max_tokens(self) -> int:
        """
        Get the maximum token limit for this provider.

        Returns:
            Maximum number of tokens supported
        """
        pass

    @abstractmethod
    async def discover_models(self) -> list[dict[str, Any]]:
        """
        Discover available models from the provider with metadata.

        Returns:
            List of model info dictionaries
        """
        pass

    @abstractmethod
    async def probe_key(self, api_key: str) -> dict[str, Any]:
        """
        Probe the API key to determine its tier and limits.

        Returns:
            Dictionary with tier info (e.g., {"tier": "free", "rpm": 15})
        """
        pass


class IContextAnalyzer(ABC):
    """
    Interface for context analysis and routing decisions.
    """

    @abstractmethod
    async def analyze(
        self,
        request: ChatRequest,
        available_tiers: dict[ProviderType, set[str]] | None = None,
    ) -> RoutingDecision:
        """
        Analyze the request context and determine routing strategy.

        Args:
            request: The chat completion request

        Returns:
            RoutingDecision object
        """
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
    """
    Interface for API key management and rotation.
    """

    @abstractmethod
    async def get_next_key(self, provider: ProviderType, min_tier: str = "free") -> str:
        """
        Get the next available API key for the provider.

        Args:
            provider: The provider type to get a key for

        Returns:
            The next available API key

        Raises:
            ResourceExhaustedError: If no keys are available
        """
        pass

    @abstractmethod
    async def report_success(
        self,
        provider: ProviderType,
        api_key: str,
    ) -> None:
        """
        Report a successful API call for the key.

        Args:
            provider: The provider type
            api_key: The API key that succeeded
        """
        pass

    @abstractmethod
    async def report_failure(
        self,
        provider: ProviderType,
        api_key: str,
        error: Exception,
    ) -> None:
        """
        Report a failed API call for the key.

        Args:
            provider: The provider type
            api_key: The API key that failed
            error: The error that occurred
        """
        pass

    @abstractmethod
    def get_key_status(self) -> dict[ProviderType, dict[str, Any]]:
        """
        Get current key status for all providers.
        """
        pass


class IRouter(ABC):
    """
    Interface for request routing and orchestration.
    """

    @abstractmethod
    async def route_request(
        self,
        request: ChatRequest,
    ) -> ChatResponse:
        """
        Route the request through the appropriate provider.

        Args:
            request: The chat completion request

        Returns:
            The generated chat response

        Raises:
            RoutingError: If routing fails
        """
        pass

    @abstractmethod
    async def get_status(self) -> dict[str, Any]:
        """
        Get the current status of all providers.

        Returns:
            Dictionary with provider status
        """
        pass

    @abstractmethod
    def get_supported_models(self) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def get_all_models(self) -> list[dict[str, Any]]:
        pass
