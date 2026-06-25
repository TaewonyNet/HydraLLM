import asyncio
import logging
import time
import traceback
from datetime import datetime
from typing import Any, cast

from src.adapters.providers.cerebras import CerebrasAdapter
from src.adapters.providers.gemini import GeminiAdapter
from src.adapters.providers.local_cli import LocalCLIAdapter
from src.adapters.providers.openai_compat import OpenAICompatAdapter
from src.core.config import settings
from src.core.exceptions import (
    BaseAppError,
)
from src.core.logging import request_id_ctx
from src.domain.enums import AgentType, ModelType, ProviderType
from src.domain.interfaces import ILLMProvider, IRouter, ISessionManager
from src.domain.models import ChatMessage, ChatRequest, ChatResponse
from src.services.agent_executor import AgentExecutor
from src.services.analyzer import ContextAnalyzer
from src.services.circuit_breaker import CircuitBreaker
from src.services.compressor import ContextCompressor
from src.services.context_manager import ContextManager
from src.services.intent_classifier import IntentClassifier
from src.services.key_manager import KeyManager
from src.services.metrics_service import MetricsService
from src.services.observability import Observability
from src.services.resilience_manager import ResilienceManager
from src.services.scraper import WebScraper
from src.services.session_manager import SessionManager
from src.services.session_orchestrator import SessionOrchestrator
from src.services.web_context_service import WebContextService
from src.services.web_coordinator import WebEnrichmentCoordinator

logger = logging.getLogger(__name__)


class Gateway(IRouter):
    def __init__(
        self,
        analyzer: ContextAnalyzer | None = None,
        key_manager: KeyManager | None = None,
        session_manager: ISessionManager | None = None,
        scraper: WebScraper | None = None,
        compressor: ContextCompressor | None = None,
        metrics_service: MetricsService | None = None,
        intent_classifier: IntentClassifier | None = None,
    ):
        self.key_manager = key_manager or KeyManager()
        self.analyzer = analyzer or ContextAnalyzer()
        self.session_manager = session_manager or SessionManager()
        self.scraper = scraper or WebScraper()
        self.compressor = compressor or ContextCompressor()
        self.metrics_service = metrics_service or MetricsService(self.session_manager)
        self.context_manager = ContextManager()
        self.intent_classifier = intent_classifier

        self.web_context = WebContextService(
            self.analyzer,
            self.scraper,
            self.compressor,
            self.session_manager,
            intent_classifier=self.intent_classifier,
        )
        self.web_coordinator = WebEnrichmentCoordinator(self.web_context, self.session_manager)
        self.sessions = SessionOrchestrator(self.session_manager, self.compressor)

        self.max_retries = 3

        self._adapters: dict[tuple[ProviderType | AgentType, str], ILLMProvider] = {}
        self._breakers: dict[ProviderType, CircuitBreaker] = {
            p: CircuitBreaker() for p in ProviderType
        }
        self.agent_executor = AgentExecutor(self.analyzer, self._get_agent_adapter)
        # 백그라운드 recovery_task 와 admin /probe 의 동시 호출 직렬화(L1).
        self._recover_lock = asyncio.Lock()
        self.resilience = ResilienceManager(
            self.key_manager,
            self.analyzer,
            self._breakers,
            self.session_manager,
            self.max_retries,
            get_provider_adapter=self._get_provider_adapter,
            process_with_agent=self.agent_executor.process,
        )

    async def process_request(
        self, request: ChatRequest, endpoint: str = "chat"
    ) -> ChatResponse:
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

        # Inject current date and strict truth instructions as a system message
        today_str = datetime.now().strftime("%Y-%m-%d")
        date_msg = ChatMessage(
            role="system",
            content=(
                f"[SYSTEM CONTEXT] Today is {today_str}. "
                "You MUST prioritize the provided [WEB REFERENCE DATA] over your internal knowledge. "
                "Language Policy: Always respond in the SAME LANGUAGE as the user query (Default: Korean). "
                "Each search result may contain a [PUBLISHED_DATE]. ALWAYS check these dates to ensure accuracy."
            ),
        )

        if history:
            seen = set()
            merged: list[ChatMessage] = [date_msg]
            for m in history + request.messages:
                content_str = str(m.content).strip()
                key = (m.role, content_str[:300])
                if key not in seen:
                    merged.append(m)
                    seen.add(key)
            request.messages = merged
        else:
            request.messages = [date_msg] + request.messages

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
                "request_id": request_id,
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

        web_parts, web_text = await self.web_coordinator.enrich(request, request_id)

        llm_start = time.perf_counter()
        try:
            response, retry_parts = await self.resilience.execute_with_resilience(
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
                    "session_id": request.session_id,
                    "provider": provider_name,
                    "model": response.model,
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
                parts_list: list[dict[str, Any]] = list(all_parts)
                extra = msg.model_extra or {}
                extra["parts"] = parts_list
                msg.__dict__["__pydantic_extra__"] = extra

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
                    "session_id": request.session_id,
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
        # 동시 복구(백그라운드 60s + admin /probe) 중복 방지(L1): 진행 중이면 생략.
        if self._recover_lock.locked():
            return
        async with self._recover_lock:
            await self._recover_failed_keys_impl()

    async def _recover_failed_keys_impl(self) -> None:
        now = time.time()
        for p in ProviderType:
            failed = self.key_manager.get_failed_keys(p)
            for k in failed:
                # 60초 폴링은 주기일 뿐, 카테고리별 쿨다운(403→24h/quota→1h/기타→5m)을
                # 존중한다. cooldown_until 이 아직 미래면 재프로브하지 않고 건너뛴다.
                cooldown_until = self.key_manager.get_key_metadata(p, k).get(
                    "cooldown_until", 0
                )
                if cooldown_until > now:
                    continue
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
