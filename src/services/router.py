import asyncio
import logging
from typing import Any

from src.core.exceptions import ProviderError, ResourceExhaustedError
from src.domain.enums import ProviderType
from src.domain.interfaces import IContextAnalyzer, IKeyManager, ILLMProvider, IRouter
from src.domain.models import ChatRequest, ChatResponse


class Router(IRouter):
    """
    Orchestrates request routing with retry logic and provider management.
    """

    def __init__(
        self,
        providers: dict[ProviderType, ILLMProvider],
        analyzer: IContextAnalyzer,
        key_manager: IKeyManager,
        max_retries: int = 3,
        retry_delay: float = 0.1,
    ):
        self._providers = providers
        self._analyzer = analyzer
        self._key_manager = key_manager
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._logger = logging.getLogger(__name__)

    async def route_request(self, request: ChatRequest) -> ChatResponse:
        """
        Route the request through the appropriate provider.
        """
        # Analyze request context
        self._logger.debug(f"Analyzing request for model: {request.model}")
        analysis_result = await self._analyzer.analyze(request)

        # Extract routing decision
        provider_type = analysis_result.provider
        if not provider_type:
            msg = "No provider selected in routing decision"
            raise ProviderError(msg)

        model_name = analysis_result.model_name

        self._logger.info(f"Routing to {provider_type.value} with model {model_name}")

        # Get provider instance
        provider = self._providers.get(provider_type)
        if not provider:
            msg = f"Provider {provider_type.value} not configured"
            raise ProviderError(msg)

        # Get API key
        try:
            api_key = await self._key_manager.get_next_key(provider_type)
        except ResourceExhaustedError as e:
            msg = f"Key exhaustion for provider {provider_type.value}: {str(e)}"
            self._logger.error(msg)
            raise ProviderError(msg) from e

        # Retry logic
        last_error = None
        for attempt in range(self._max_retries):
            try:
                # Generate response
                response = await provider.generate(request, api_key)

                # Report success
                if provider_type:
                    await self._key_manager.report_success(provider_type, api_key)

                self._logger.info(
                    f"Successfully generated response from {provider_type.value if provider_type else 'unknown'}"
                )
                return response

            except ProviderError as e:
                last_error = e
                self._logger.warning(
                    f"Attempt {attempt + 1} failed for {provider_type.value if provider_type else 'unknown'}: {str(e)}"
                )

                # Report failure
                if provider_type:
                    await self._key_manager.report_failure(provider_type, api_key, e)

                # Check if we should try a different provider
                if attempt < self._max_retries - 1:
                    # Re-analyze with different strategy
                    analysis_result = await self._analyzer.analyze(request)
                    provider_type = analysis_result.provider
                    if not provider_type:
                        continue
                    model_name = analysis_result.model_name

                    self._logger.info(f"Retrying with {provider_type.value}")

                    # Get new provider and key
                    provider = self._providers.get(provider_type)
                    if not provider:
                        msg = f"Provider {provider_type.value} not configured"
                        raise ProviderError(msg) from last_error

                    try:
                        api_key = await self._key_manager.get_next_key(provider_type)
                    except ResourceExhaustedError:
                        # Try next provider
                        continue

                # Wait before retry
                await asyncio.sleep(self._retry_delay)

        # If all retries failed
        self._logger.error(f"All {self._max_retries} attempts failed")
        msg = f"All routing attempts failed: {str(last_error)}"
        raise ProviderError(msg) from last_error

    async def get_status(self) -> dict[str, Any]:
        """
        Get the current status of all providers.
        """
        status = {}

        for provider_type, provider in self._providers.items():
            key_status = self._key_manager.get_key_status()
            provider_status = key_status.get(provider_type, {})

            status[provider_type.value] = {
                "provider": provider_type.value,
                "model": provider.get_supported_models(),
                "multimodal": provider.is_multimodal(),
                "max_tokens": provider.get_max_tokens(),
                "available_keys": provider_status.get("active", 0),
                "total_keys": provider_status.get("total", 0),
                "healthy": provider_status.get("active", 0) > 0,
                "last_error": None,
                "usage": provider_status.get("usage", {}),
            }

        return status
