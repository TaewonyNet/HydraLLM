import asyncio
import logging
import time
import traceback
from typing import Any, cast

from src.adapters.providers.cerebras import CerebrasAdapter
from src.adapters.providers.gemini import GeminiAdapter
from src.adapters.providers.local_cli import LocalCLIAdapter
from src.adapters.providers.openai_compat import OpenAICompatAdapter
from src.core.config import settings
from src.core.exceptions import RateLimitError, ResourceExhaustedError
from src.domain.enums import AgentType, ModelType, ProviderType
from src.domain.interfaces import ILLMProvider, IRouter, ISessionManager
from src.domain.models import ChatMessage, ChatRequest, ChatResponse, RoutingDecision
from src.services.analyzer import ContextAnalyzer
from src.services.compressor import ContextCompressor
from src.services.context_manager import ContextManager
from src.services.key_manager import KeyManager
from src.services.metrics_service import MetricsService
from src.services.observability import Observability
from src.services.scraper import WebScraper
from src.services.session_orchestrator import SessionOrchestrator
from src.services.web_context_service import WebContextService

logger = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.last_failure_time: float = 0.0
        self.state = "CLOSED"

    def is_available(self) -> bool:
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "HALF_OPEN"
                return True
            return False
        return True

    def report_success(self) -> None:
        self.failures = 0
        self.state = "CLOSED"

    def report_failure(self) -> None:
        self.failures += 1
        self.last_failure_time = time.time()
        if self.failures >= self.failure_threshold:
            self.state = "OPEN"


class Gateway(IRouter):
    def __init__(
        self,
        analyzer: ContextAnalyzer | None = None,
        key_manager: KeyManager | None = None,
        session_manager: ISessionManager | None = None,
        scraper: WebScraper | None = None,
        compressor: ContextCompressor | None = None,
        metrics_service: MetricsService | None = None,
    ):
        from .session_manager import SessionManager

        self.analyzer = analyzer or ContextAnalyzer()
        self.key_manager = key_manager or KeyManager()
        self.session_manager = session_manager or SessionManager()
        self.scraper = scraper or WebScraper()
        self.compressor = compressor or ContextCompressor()
        self.metrics_service = metrics_service or MetricsService(self.session_manager)
        self.context_manager = ContextManager()

        self.web_context = WebContextService(
            self.analyzer, self.scraper, self.compressor, self.session_manager
        )
        self.sessions = SessionOrchestrator(self.session_manager, self.compressor)

        self.max_retries = 3

        self._adapters: dict[tuple[ProviderType | AgentType, str], ILLMProvider] = {}
        self._breakers: dict[ProviderType, CircuitBreaker] = {
            p: CircuitBreaker() for p in ProviderType
        }

    async def process_request(
        self, request: ChatRequest, endpoint: str = "chat"
    ) -> ChatResponse:
        from src.core.logging import request_id_ctx

        request_id = request_id_ctx.get()
        Observability.start_trace(request_id)

        original_request_model = request.model

        if not request.messages and request.prompt:
            request.messages = [
                ChatMessage(role="user", content=request.prompt, name=None)
            ]
        if not request.messages:
            error_msg = "Messages required"
            raise ValueError(error_msg)

        history = await self.sessions.load_history(request)
        await self.sessions.save_user_message(request)

        if history:
            seen = set()
            merged: list[ChatMessage] = []
            for m in history + request.messages:
                content_str = str(m.content).strip()
                key = (m.role, content_str[:300])
                if key not in seen:
                    merged.append(m)
                    seen.add(key)
            request.messages = merged

        routing_start = time.time()
        available_tiers = self._get_available_tiers()
        decision = await self.analyzer.analyze(request, available_tiers=available_tiers)

        tokens = request.estimate_token_count()
        routing_log = (
            f"Routing decision for session {request.session_id}: "
            f"provider={decision.provider.value if decision and decision.provider else 'N/A'}, "
            f"model={decision.model_name if decision else 'N/A'}, reason={decision.reason if decision else 'N/A'}, total_tokens={tokens}"
        )
        logger.info(routing_log)

        await self.session_manager.log_system_event(
            level="INFO",
            category="ROUTING",
            message=routing_log,
            metadata={
                "session_id": request.session_id,
                "decision": {
                    "provider": decision.provider.value
                    if decision and decision.provider
                    else None,
                    "model": decision.model_name if decision else None,
                    "reason": decision.reason if decision else None,
                    "tokens": tokens,
                },
            },
        )

        Observability.record_step(
            "routing",
            time.time() - routing_start,
            {
                "model": decision.model_name if decision else "N/A",
                "reason": decision.reason if decision else "N/A",
                "tokens": tokens,
            },
        )

        if decision:
            request.model = decision.model_name

        web_start = time.time()
        web_parts = await self.web_context.enrich_request(request)
        Observability.record_step("web_enrichment", time.time() - web_start)

        llm_start = time.perf_counter()
        try:
            response, retry_parts = await self._execute_with_full_resilience(
                request, decision
            )
            latency_ms = int((time.perf_counter() - llm_start) * 1000)

            provider_name = (
                response.usage.get("gateway_provider", "unknown")
                if response.usage
                else "unknown"
            )

            await self.session_manager.log_system_event(
                level="INFO",
                category="LLM_EXECUTION",
                message=f"Request successful via {provider_name} ({latency_ms}ms)",
                metadata={
                    "request_id": request_id,
                    "provider": provider_name,
                    "latency_ms": latency_ms,
                    "usage": response.usage,
                },
            )

            if response.usage:
                try:
                    await self.metrics_service.record_request(
                        request_id=request_id,
                        provider=response.usage.get("gateway_provider", "unknown"),
                        model=response.usage.get("gateway_model", decision.model_name),
                        prompt_tokens=response.usage.get("prompt_tokens", 0),
                        completion_tokens=response.usage.get("completion_tokens", 0),
                        latency_ms=latency_ms,
                        status="success",
                        endpoint=endpoint,
                    )
                except Exception as me:
                    logger.warning(f"Failed to record metrics (non-fatal): {me}")

            all_parts = (web_parts or []) + (retry_parts or [])
            if response.choices and all_parts:
                msg = response.choices[0].message
                # Inject parts into message extra data
                parts_list = [
                    p if isinstance(p, dict) else p.model_dump() for p in all_parts
                ]
                if not hasattr(msg, "model_extra") or msg.model_extra is None:
                    msg.model_extra = {}
                msg.model_extra["parts"] = parts_list

            try:
                await self.sessions.save_assistant_response(
                    request, response, extra_parts=all_parts
                )
            except Exception as se:
                logger.warning(f"Failed to save session (non-fatal): {se}")

            if original_request_model:
                response.model = original_request_model

            self.context_manager.cleanup()
            Observability.finalize_trace()
            return response

        except Exception as e:
            self.context_manager.cleanup()
            latency_ms = int((time.perf_counter() - llm_start) * 1000)
            from src.core.exceptions import BaseAppError

            category = "INTERNAL_ERROR"
            if isinstance(e, BaseAppError):
                category = (
                    e.category.value
                    if hasattr(e.category, "value")
                    else str(e.category)
                )

            logger.error(f"[{category}] Request failed: {str(e)}")

            await self.session_manager.log_system_event(
                level="ERROR",
                category="LLM_EXECUTION",
                message=f"Request failed: {category} - {str(e)[:100]}",
                metadata={
                    "request_id": request_id,
                    "error": str(e),
                    "category": category,
                    "traceback": traceback.format_exc()
                    if category == "INTERNAL_ERROR"
                    else None,
                },
            )

            provider_name = "unknown"
            if decision and decision.provider:
                provider_name = decision.provider.value
            elif decision and decision.agent:
                provider_name = decision.agent.value

            await self.metrics_service.record_request(
                request_id=request_id,
                provider=provider_name,
                model=decision.model_name if decision else "unknown",
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=latency_ms,
                status=f"error: {category}: {str(e)[:50]}",
                endpoint=endpoint,
            )
            Observability.finalize_trace()
            raise

    def _get_available_tiers(self) -> dict[ProviderType, set[str]]:
        tiers: dict[ProviderType, set[str]] = {}
        for p in ProviderType:
            if not self._breakers[p].is_available():
                tiers[p] = set()
                continue
            stats = self.key_manager.get_key_status().get(p, {})
            tiers[p] = {
                k["tier"] for k in stats.get("keys", []) if k["status"] == "active"
            }
        return tiers

    async def _execute_with_full_resilience(
        self, request: ChatRequest, decision: RoutingDecision
    ) -> tuple[ChatResponse, list[dict[str, Any]]]:
        retry_parts: list[dict[str, Any]] = []
        last_exception: Exception | None = None

        is_strict = False
        original_hint = request.model.lower() if request.model else ""
        if "/" in original_hint and original_hint.split("/")[1] == "auto":
            is_strict = True
            logger.info(f"Using STRICT routing for {original_hint}")

        providers_to_try = []
        if decision.provider:
            providers_to_try.append(decision.provider)

        if not is_strict:
            for p_name in self.analyzer._provider_priority:
                try:
                    p_type = ProviderType(p_name)
                    if p_type not in providers_to_try:
                        providers_to_try.append(p_type)
                except (ValueError, KeyError):
                    continue

        for provider_type in providers_to_try:
            if not self._breakers[provider_type].is_available():
                logger.warning(f"Skipping {provider_type.value}: Circuit is OPEN")
                if is_strict:
                    raise ResourceExhaustedError(
                        f"Strict provider {provider_type.value} is currently unavailable (Circuit OPEN)"
                    )
                continue

            if provider_type != decision.provider:
                new_model = self.analyzer._get_default_model_for_provider(provider_type)
                logger.info(
                    f"🔄 Switching provider to {provider_type.value}, model to {new_model}"
                )
                request.model = new_model
                decision.model_name = new_model
                decision.provider = provider_type

            for attempt in range(self.max_retries):
                api_key: str | None = None
                try:
                    api_key = await self.key_manager.get_next_key(provider_type)
                    adapter = self._get_provider_adapter(provider_type, api_key)

                    response = await adapter.generate(request, api_key)

                    self._breakers[provider_type].report_success()
                    await self.key_manager.report_success(provider_type, api_key)
                    self._enrich_response_usage(
                        response, provider_type, api_key, decision
                    )
                    return response, retry_parts

                except (RateLimitError, ResourceExhaustedError) as e:
                    last_exception = e
                    self._breakers[provider_type].report_failure()
                    if api_key:
                        await self.key_manager.report_failure(provider_type, api_key, e)

                    if self.key_manager.get_available_keys_count(provider_type) == 0:
                        logger.warning(
                            f"Provider {provider_type.value} exhausted. Checking fallback..."
                        )
                        if is_strict:
                            raise e
                        break

                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(1)

                except Exception as e:
                    last_exception = e
                    logger.error(f"Unexpected error with {provider_type.value}: {e}")
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(1)
                    else:
                        break

        return await self._final_fallback(request, decision), retry_parts

    async def _final_fallback(
        self, request: ChatRequest, decision: RoutingDecision
    ) -> ChatResponse:
        logger.warning("All primary paths failed. Triggering final local fallback.")
        decision.agent = AgentType.OLLAMA
        decision.model_name = ""
        request.model = ""
        return await self._process_with_agent(request, decision)

    def _enrich_response_usage(
        self,
        response: ChatResponse,
        provider: ProviderType,
        key: str,
        decision: RoutingDecision,
    ) -> None:
        if not response.usage:
            response.usage = {}
        idx = self.key_manager.get_key_index(provider, key)
        response.usage.update(
            {
                "gateway_provider": provider.value,
                "gateway_key_index": idx,
                "gateway_model": decision.model_name,
                "routing_reason": decision.reason,
            }
        )

    # --- 기존 헬퍼 메서드 유지 및 최적화 ---
    def _get_provider_adapter(
        self, provider: ProviderType, api_key: str
    ) -> ILLMProvider:
        adapter_key = (provider, api_key)
        if adapter_key not in self._adapters:
            if provider == ProviderType.GEMINI:
                self._adapters[adapter_key] = GeminiAdapter(
                    api_key, self.context_manager
                )
            elif provider == ProviderType.GROQ:
                self._adapters[adapter_key] = OpenAICompatAdapter(
                    "https://api.groq.com/openai/v1",
                    api_key,
                    default_model=ModelType.GROQ_LLAMA_3_3_70B.value,
                )
            elif provider == ProviderType.CEREBRAS:
                self._adapters[adapter_key] = CerebrasAdapter(api_key)
        return self._adapters[adapter_key]

    async def _process_with_agent(
        self, request: ChatRequest, decision: RoutingDecision
    ) -> ChatResponse:
        agent = decision.agent
        if not agent:
            msg = "No agent"
            raise ResourceExhaustedError(msg)

        discovered_models = self.analyzer.get_all_discovered_models_info()
        agent_models = [
            m["id"]
            for m in discovered_models
            if m.get("owned_by") == "local-agent"
            and (
                m["id"].lower().startswith(agent.value.lower())
                or m.get("display_name", "").lower().startswith(agent.value.lower())
            )
        ]

        if not agent_models:
            adapter = self._get_agent_adapter(agent)
            try:
                new_models = await adapter.discover_models()
                for m_info in new_models:
                    self.analyzer.register_model(m_info["id"], agent, m_info)
                agent_models = [m["id"] for m in new_models]
            except Exception:
                pass

        if (
            not decision.model_name
            or decision.model_name == agent.value
            or decision.model_name == "auto"
            or decision.model_name not in agent_models
        ):
            if agent_models:
                target = next(
                    (m for m in agent_models if "llama" in m.lower()), agent_models[0]
                )
                decision.model_name = target
                request.model = target
            else:
                fallback_model = "llama3"
                decision.model_name = fallback_model
                request.model = fallback_model

        adapter = self._get_agent_adapter(agent)
        resp = await adapter.generate(request, "local-agent")

        full_model_path = f"LOCAL-AGENT/{agent.value}/{decision.model_name}"
        if not resp.usage:
            resp.usage = {}
        resp.usage.update(
            {
                "gateway_provider": "LOCAL-AGENT",
                "gateway_key_index": 0,
                "gateway_model": full_model_path,
                "routing_reason": decision.reason,
            }
        )
        resp.model = full_model_path
        return resp

    def _get_agent_adapter(self, agent: AgentType) -> ILLMProvider:
        key = (agent, "local-agent")
        if key not in self._adapters:
            if agent == AgentType.OLLAMA:
                self._adapters[key] = OpenAICompatAdapter(
                    base_url=settings.ollama_base_url,
                    api_key="ollama",
                    default_model=None,
                )

            else:
                self._adapters[key] = LocalCLIAdapter(agent.value, agent.value)
        return self._adapters[key]

    async def recover_failed_keys(self) -> None:
        for p in ProviderType:
            failed = self.key_manager.get_failed_keys(p)
            for k in failed:
                try:
                    meta = await self._get_provider_adapter(p, k).probe_key(k)
                    if meta.get("status") == "failed":
                        continue
                    await self.key_manager.report_success(p, k)
                    self.key_manager.update_key_metadata(p, k, meta)
                except Exception:
                    pass

            stats = self.key_manager.get_key_status().get(p, {})

            status = "healthy" if stats.get("active", 0) > 0 else "offline"
            await self.session_manager.update_provider_health(
                provider=p.value,
                status=status,
                active=stats.get("active", 0),
                failed=stats.get("failed", 0),
            )

    async def discover_all_models(self) -> None:
        for p in ProviderType:
            try:
                k = await self.key_manager.get_next_key(p)
                models = await self._get_provider_adapter(p, k).discover_models()
                for m in models:
                    self.analyzer.register_model(m["id"], p, m)
            except Exception:
                pass
        for a in AgentType:
            try:
                models = await self._get_agent_adapter(a).discover_models()
                for m in models:
                    self.analyzer.register_model(m["id"], a, m)
            except Exception:
                pass

    def initialize_settings(self) -> None:
        from .session_manager import SessionManager

        sm = cast(SessionManager, self.session_manager)
        onboarding = sm._get_setting_sync("onboarding_completed", False)
        enabled = sm._get_setting_sync("enabled_models", None)
        settings.onboarding_completed = onboarding
        settings.enabled_models = enabled

    async def route_request(self, request: ChatRequest) -> ChatResponse:
        return await self.process_request(request)

    def get_supported_models(self) -> list[dict[str, Any]]:
        return self.analyzer.get_supported_models_info()

    def get_all_models(self) -> list[dict[str, Any]]:
        return self.analyzer.get_all_discovered_models_info()

    async def get_status(self) -> dict[str, Any]:
        key_status = self.key_manager.get_key_status()
        return {
            "status": "healthy",
            "providers": {
                p.value: {
                    "healthy": data["active"] > 0,
                    "active_keys": data["active"],
                    "failed_keys": data["failed"],
                    "total_keys": data["total"],
                }
                for p, data in key_status.items()
                if isinstance(p, ProviderType)
            },
            "agents": {
                "ollama": {"healthy": True, "base_url": settings.ollama_base_url},
                "opencode": {"healthy": True, "base_url": settings.opencode_base_url},
                "openclaw": {"healthy": True, "base_url": settings.openclaw_base_url},
            },
        }

    async def probe_all_keys(self) -> None:
        for p in ProviderType:
            stats = self.key_manager.get_key_status().get(p, {})
            active = stats.get("active", 0)
            failed = stats.get("failed", 0)
            status = "healthy" if active > 0 else "offline"

            await self.session_manager.update_provider_health(
                provider=p.value,
                status=status,
                active=active,
                failed=failed,
                last_error=None,
            )
        logger.info("✅ All provider health statuses initialized in database")
