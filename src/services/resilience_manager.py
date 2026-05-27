import asyncio
import logging
import time
from typing import Any
from src.core.exceptions import RateLimitError, ResourceExhaustedError, ServiceUnavailableError
from src.domain.enums import AgentType, ProviderType
from src.domain.models import ChatRequest, ChatResponse, RoutingDecision
from src.domain.interfaces import ILLMProvider
from src.services.comm_logger import comm_log_buffer

logger = logging.getLogger(__name__)

class ResilienceManager:
    def __init__(
        self,
        key_manager,
        analyzer,
        breakers,
        session_manager,
        max_retries: int = 3,
        get_provider_adapter: Any = None,
        process_with_agent: Any = None,
    ):
        self.key_manager = key_manager
        self.analyzer = analyzer
        self.breakers = breakers
        self.session_manager = session_manager
        self.max_retries = max_retries
        self._get_provider_adapter = get_provider_adapter
        self._process_with_agent = process_with_agent

    async def execute_with_resilience(
        self, request: ChatRequest, decision: RoutingDecision
    ) -> tuple[ChatResponse, list[dict[str, Any]]]:
        retry_parts: list[dict[str, Any]] = []

        is_strict = False
        original_hint = (request.model or "").lower()
        
        has_prefix = "/" in original_hint
        is_local_path = "local-agent" in original_hint or any(a.value in original_hint for a in AgentType)

        if has_prefix or is_local_path:
            if is_local_path or any(p.value in original_hint for p in ProviderType) or "auto" in original_hint:
                is_strict = True
                logger.info(f"Using STRICT routing for {original_hint}")

        if is_strict and decision.agent:
            try:
                return await self._process_with_agent(request, decision), retry_parts
            except Exception as e:
                await self.session_manager.log_system_event(
                    level="ERROR",
                    category="AGENT_EXECUTION",
                    message=f"Strict agent execution failed: {str(e)[:100]}",
                    metadata={
                        "error": str(e),
                        "agent": decision.agent.value,
                        "model": decision.model_name,
                        "session_id": request.session_id
                    }
                )
                raise

        if not is_strict and decision.agent:
            try:
                return await self._process_with_agent(request, decision), retry_parts
            except Exception as e:
                await self.session_manager.log_system_event(
                    level="WARNING",
                    category="AGENT_EXECUTION",
                    message=f"Agent execution failed, falling back to cloud: {str(e)[:100]}",
                    metadata={
                        "error": str(e),
                        "agent": decision.agent.value,
                        "model": decision.model_name,
                        "session_id": request.session_id
                    }
                )
                logger.error(f"Agent execution failed, attempting cloud fallback: {e}")
                decision.agent = None

        providers_to_try = []
        if decision.provider:
            providers_to_try.append(decision.provider)


        if not providers_to_try or (original_hint in ["auto", "default", "mllm/auto"]):
            has_any_cloud_active = False
            for p in [ProviderType.GEMINI, ProviderType.GROQ, ProviderType.CEREBRAS]:
                if self.key_manager.get_available_keys_count(p) > 0:
                    has_any_cloud_active = True
                    break
            if not has_any_cloud_active and not is_strict:
                logger.warning("No cloud keys available. Skipping cloud attempts.")
                return await self.final_fallback(request, decision), retry_parts

        if not is_strict:
            for p_name in self.analyzer.provider_priority:
                try:
                    p_type = ProviderType(p_name)
                    if p_type not in providers_to_try:
                        providers_to_try.append(p_type)
                except (ValueError, KeyError):
                    continue

        if decision.agent:
            try:
                return await self._process_with_agent(request, decision), retry_parts
            except Exception as e:
                if is_strict:
                    raise
                logger.error(f"Agent execution failed, attempting cloud fallback: {e}")

        for provider_type in providers_to_try:
            if not self.breakers[provider_type].is_available():
                logger.warning(f"Skipping {provider_type.value}: Circuit is OPEN")
                if is_strict:
                    msg = f"Strict provider {provider_type.value} is currently unavailable (Circuit OPEN)"
                    raise ResourceExhaustedError(msg)
                continue

            if provider_type != decision.provider:
                new_model = self.analyzer.get_default_model_for_provider(provider_type)
                logger.info(f"Switching provider to {provider_type.value}, model to {new_model}")
                request.model = new_model
                decision.model_name = new_model
                decision.provider = provider_type
            
            if not is_strict and decision.agent and provider_type == providers_to_try[0]:
                continue

            for attempt in range(self.max_retries):
                api_key: str | None = None
                try:
                    api_key = await self.key_manager.get_next_key(provider_type)
                    adapter = self._get_provider_adapter(provider_type, api_key)

                    comm_log_buffer.record(
                        "request",
                        provider_type.value,
                        {
                            "model": request.model,
                            "messages_count": len(request.messages or []),
                            "has_search": request.has_search,
                            "stream": request.stream,
                        },
                    )

                    response = await adapter.generate(request, api_key)
                    if response and response.choices and len(response.choices) > 0:
                        comm_log_buffer.record(
                            "response",
                            provider_type.value,
                            {
                                "model": response.model,
                                "finish_reasons": [
                                    c.finish_reason for c in (response.choices or [])
                                ],
                                "usage": response.usage,
                            },
                        )

                        self.breakers[provider_type].report_success()
                        await self.key_manager.report_success(provider_type, api_key)
                        self.enrich_response_usage(response, provider_type, api_key, decision)
                        return response, retry_parts
                    else:
                        raise Exception("Empty response from adapter")

                except (RateLimitError, ResourceExhaustedError) as e:
                    self.breakers[provider_type].report_failure()
                    if api_key:
                        await self.key_manager.report_failure(provider_type, api_key, e)

                    if is_strict:
                        raise e

                    if self.key_manager.get_available_keys_count(provider_type) == 0:
                        logger.warning(f"Provider {provider_type.value} exhausted. Checking fallback...")
                        break

                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(1)

                except ServiceUnavailableError as e:
                    logger.error(f"Service error with {provider_type.value}: {e}")
                    self.breakers[provider_type].report_failure()
                    if api_key:
                        await self.key_manager.report_failure(provider_type, api_key, e)
                    
                    if is_strict:
                        raise e

                    if self.key_manager.get_available_keys_count(provider_type) == 0:
                        break
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(0.01)

                except Exception as e:
                    logger.error(f"Unexpected error with {provider_type.value}: {e}")
                    err_msg = str(e).lower()
                    if api_key and (
                        "403" in err_msg
                        or "denied" in err_msg
                        or "api_key_invalid" in err_msg
                        or "unauthorized" in err_msg
                    ):
                        await self.key_manager.report_failure(provider_type, api_key, e)
                    
                    if is_strict:
                        raise e
                    
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(1)
                    else:
                        break

        return await self.final_fallback(request, decision), retry_parts

    async def final_fallback(
        self, request: ChatRequest, decision: RoutingDecision
    ) -> ChatResponse:
        logger.warning("All primary paths failed. Triggering final local fallback.")
        decision.agent = AgentType.OLLAMA
        decision.model_name = ""
        request.model = ""
        return await self._process_with_agent(request, decision)

    def enrich_response_usage(
        self,
        response: ChatResponse,
        provider: ProviderType,
        key: str,
        decision: RoutingDecision,
    ) -> None:
        if not response.usage:
            response.usage = {}
        idx = self.key_manager.get_key_index(provider, key)

        reason_map = {
            "model_hint": "USER_HINT",
            "token_count": "TOKEN_OPTIMIZED",
            "image_present": "MULTIMODAL_ANALYSIS",
            "WEB_INTENT_REQUIRE_INTELLIGENCE": "WEB_INTENT_SEARCH",
            "key_availability": "KEY_AVAILABILITY",
        }
        display_reason = reason_map.get(decision.reason, str(decision.reason).upper())

        response.usage.update(
            {
                "gateway_provider": provider.value,
                "gateway_key_index": idx,
                "gateway_model": decision.model_name,
                "routing_reason": display_reason,
            }
        )
