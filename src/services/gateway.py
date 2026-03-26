import asyncio
import logging
import re
from typing import Any

from ..adapters.providers.gemini import GeminiAdapter
from ..adapters.providers.local_cli import LocalCLIAdapter
from ..adapters.providers.openai_compat import OpenAICompatAdapter
from ..core.config import settings
from ..core.exceptions import ResourceExhaustedError
from ..domain.enums import AgentType, ProviderType
from ..domain.interfaces import ILLMProvider, IRouter
from ..domain.models import ChatMessage, ChatRequest, ChatResponse, RoutingDecision
from .analyzer import ContextAnalyzer
from .compressor import ContextCompressor
from .key_manager import KeyManager
from .scraper import WebScraper
from .session_manager import SessionManager


class Gateway(IRouter):
    def __init__(
        self,
        analyzer: ContextAnalyzer | None = None,
        key_manager: KeyManager | None = None,
        session_manager: SessionManager | None = None,
        scraper: WebScraper | None = None,
        compressor: ContextCompressor | None = None,
    ):
        self.analyzer = analyzer or ContextAnalyzer()
        self.key_manager = key_manager or KeyManager()
        self.session_manager = session_manager or SessionManager()
        self.scraper = scraper or WebScraper()
        self.compressor = compressor or ContextCompressor()
        self.max_retries = 3
        self._adapters: dict[tuple[ProviderType | AgentType, str], ILLMProvider] = {}
        self._logger = logging.getLogger(__name__)

    def initialize_settings(self) -> None:
        from src.core.config import settings

        onboarding = self.session_manager.get_setting("onboarding_completed", False)
        enabled = self.session_manager.get_setting("enabled_models", None)

        settings.onboarding_completed = onboarding
        settings.enabled_models = enabled

        if onboarding:
            self._logger.info(
                f"Loaded persisted onboarding state: {onboarding}, {len(enabled) if enabled else 0} models enabled"
            )

    async def process_request(self, request: ChatRequest) -> ChatResponse:
        if not request.messages and request.prompt:
            request.messages = [
                ChatMessage(role="user", content=request.prompt, name=None)
            ]

        if not request.messages:
            msg = "Either 'messages' or 'prompt' must be provided"
            raise ValueError(msg)

        # per-request 오버라이드 → 서버 기본값 fallback
        do_auto_fetch = (
            request.auto_web_fetch
            if request.auto_web_fetch is not None
            else settings.enable_auto_web_fetch
        )
        do_compress = (
            request.compress_context
            if request.compress_context is not None
            else settings.enable_context_compression
        )

        if request.session_id:
            history = self.session_manager.get_history(request.session_id)
            if history:
                request.messages = self._merge_messages(history, request.messages)

            user_msg = request.messages[-1]
            if user_msg.role == "user":
                self.session_manager.save_message(
                    request.session_id, user_msg.role, user_msg.content
                )

        # 세션 히스토리가 길면 LLMLingua-2로 압축하여 GPT처럼 세션 유지
        if do_compress and len(request.messages) > 4:
            estimated = request.estimate_token_count()
            if estimated > settings.max_tokens_fast_model:
                self._logger.info(
                    f"🗜️ Compressing session history: {estimated} tokens estimated, "
                    f"{len(request.messages)} messages"
                )
                request.messages = self._compress_session_history(request.messages)

        content_text = ""
        last_msg = request.messages[-1]
        if last_msg.role == "user":
            if isinstance(last_msg.content, str):
                content_text = last_msg.content
            elif isinstance(last_msg.content, list):
                for part in last_msg.content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        content_text += part.get("text", "")

        # URL 자동 감지: 프롬프트에 URL이 있으면 자동으로 web_fetch
        urls_to_fetch = []
        if do_auto_fetch and content_text:
            urls_to_fetch = re.findall(r"https?://[^\s/$.?#].[^\s]*", content_text)

        if request.web_fetch and request.web_fetch not in urls_to_fetch:
            urls_to_fetch.append(request.web_fetch)

        context_blocks = []
        for url in urls_to_fetch:
            self._logger.info(f"🌐 Fetching content for: {url}")
            raw_content = await self.scraper.scrape_url(
                url,
                mode=settings.default_scrape_mode,  # type: ignore
            )

            if do_compress:
                self._logger.info(f"🗜️ Compressing fetched content for {url}")
                raw_content = self.compressor.compress(
                    raw_content, instruction=f"Focus on {content_text}"
                )

            context_blocks.append(
                f"--- START CONTENT FROM {url} ---\n{raw_content}\n--- END CONTENT ---"
            )

        if request.has_search and not context_blocks and content_text:
            self._logger.info(f"🔍 Performing web search for: {content_text[:50]}...")
            search_results = await self.scraper.search_and_scrape(
                content_text,
                mode=settings.default_scrape_mode,  # type: ignore
            )

            if do_compress:
                search_results = self.compressor.compress(
                    search_results, instruction=content_text
                )

            context_blocks.append(
                f"--- START SEARCH RESULTS ---\n{search_results}\n--- END SEARCH RESULTS ---"
            )
            request.has_search = False

        if context_blocks:
            combined_context = "\n\n".join(context_blocks)
            request.messages.insert(
                0,
                ChatMessage(
                    role="system",
                    content="YOU ARE A HELPFUL ASSISTANT WITH ACCESS TO WEB CONTENT.\n"
                    "The following real-time data was retrieved to help answer the user's request. "
                    "Treat this as the primary factual source.\n\n" + combined_context,
                    name=None,
                ),
            )

            if hasattr(request, "tools") and request.tools:
                request.tools = [
                    t
                    for t in request.tools
                    if t.get("function", {}).get("name") != "web_fetch"
                ]

        available_tiers: dict[ProviderType, set[str]] = {}
        for p in ProviderType:
            stats = self.key_manager.get_key_status().get(p, {})
            tiers = {
                k["tier"] for k in stats.get("keys", []) if k["status"] == "active"
            }
            available_tiers[p] = tiers

        decision = await self.analyzer.analyze(request, available_tiers=available_tiers)

        if settings.debug:
            self._logger.debug(f"ROUTING DECISION: {decision}")

        response = await self._process_with_retries(request, decision)

        if request.session_id and response and response.choices:
            self.session_manager.save_message(
                request.session_id, "assistant", response.choices[0].message.content
            )

        return response

    def _compress_session_history(
        self, messages: list[ChatMessage]
    ) -> list[ChatMessage]:
        """오래된 메시지를 LLMLingua-2로 압축하여 컨텍스트 한도 내에서 세션 유지."""
        if len(messages) <= 4:
            return messages

        # 최근 메시지 2개는 보존, 나머지를 압축
        recent = messages[-2:]
        older = messages[:-2]

        # system 메시지는 별도 보존
        system_msgs = [m for m in older if m.role == "system"]
        chat_msgs = [m for m in older if m.role != "system"]

        if not chat_msgs:
            return messages

        older_text = "\n".join(
            f"[{m.role}]: {m.content}"
            for m in chat_msgs
            if isinstance(m.content, str)
        )

        compressed = self.compressor.compress(
            older_text,
            instruction="Preserve key facts, decisions, and context from this conversation",
            target_token=1500,
        )

        self._logger.info(
            f"🗜️ Compressed {len(chat_msgs)} messages → ~{len(compressed)//4} tokens"
        )

        result: list[ChatMessage] = list(system_msgs)
        result.append(
            ChatMessage(
                role="system",
                content=f"[Compressed conversation history]\n{compressed}",
            )
        )
        result.extend(recent)
        return result

    def _merge_messages(
        self, history: list[ChatMessage], current: list[ChatMessage]
    ) -> list[ChatMessage]:
        merged = []
        seen = set()
        for msg in history + current:
            key = (msg.role, str(msg.content).strip())
            if key not in seen:
                merged.append(msg)
                seen.add(key)
        return merged

    async def _process_with_agent(
        self, request: ChatRequest, decision: RoutingDecision
    ) -> ChatResponse:
        agent_type = decision.agent
        if not agent_type:
            msg = "No agent selected"
            raise ResourceExhaustedError(msg)

        original_model = request.model
        request.model = decision.model_name

        try:
            adapter = self._get_agent_adapter(agent_type)
            response = await adapter.generate(request, api_key="local-agent")
            if original_model:
                response.model = original_model
            return response
        except Exception as e:
            error_msg = f"Agent: {agent_type.value} (Model: {decision.model_name}) - Error: {str(e)}"
            self._logger.error(error_msg)
            raise Exception(error_msg) from e

    def _get_agent_adapter(self, agent: AgentType) -> ILLMProvider:
        adapter_key = (agent, "local-agent")
        if adapter_key not in self._adapters:
            if agent == AgentType.OLLAMA:
                self._adapters[adapter_key] = OpenAICompatAdapter(
                    base_url=settings.ollama_base_url,
                    api_key="ollama",
                    default_model="llama3",
                )
            elif agent == AgentType.OPENCODE:
                self._adapters[adapter_key] = LocalCLIAdapter(
                    binary_path="opencode", agent_type="opencode"
                )
            elif agent == AgentType.OPENCLAW:
                self._adapters[adapter_key] = LocalCLIAdapter(
                    binary_path="openclaw", agent_type="openclaw"
                )
        return self._adapters[adapter_key]

    async def _process_with_retries(
        self, request: ChatRequest, decision: RoutingDecision
    ) -> ChatResponse:
        provider_type = decision.provider
        if not provider_type:
            if decision.agent:
                return await self._process_with_agent(request, decision)
            msg = "No provider or agent selected"
            raise ResourceExhaustedError(msg)

        original_model = request.model
        last_exception = None

        for attempt in range(self.max_retries):
            api_key = None
            try:
                model_list = self.analyzer.get_supported_models_info()
                model_info: dict[str, Any] = next(
                    (m for m in model_list if m["id"] == decision.model_name), {}
                )
                min_tier = model_info.get("tier", "free")

                try:
                    api_key = await self.key_manager.get_next_key(
                        provider_type, min_tier=min_tier
                    )
                except ResourceExhaustedError:
                    self._logger.warning(
                        f"Provider {provider_type.value} exhausted. Falling back..."
                    )
                    found_fallback = False
                    for p in [p for p in ProviderType if p != provider_type]:
                        try:
                            api_key = await self.key_manager.get_next_key(
                                p, min_tier=min_tier
                            )
                            provider_type = p
                            if p == ProviderType.GEMINI:
                                decision.model_name = (
                                    settings.default_free_model
                                    if min_tier == "free"
                                    else settings.default_premium_model
                                )
                            elif p == ProviderType.GROQ:
                                decision.model_name = "llama-3.3-70b-versatile"
                            found_fallback = True
                            break
                        except ResourceExhaustedError:
                            continue
                    if not found_fallback:
                        raise

                if not api_key:
                    msg = "API key allocation failed"
                    raise ResourceExhaustedError(msg)

                request.model = decision.model_name
                adapter = self._get_provider_adapter(provider_type, api_key)
                response = await adapter.generate(request, api_key)

                await self.key_manager.report_success(provider_type, api_key)

                if not response.usage:
                    response.usage = {}
                response.usage.update(
                    {
                        "gateway_key_id": api_key[:8] + "...",
                        "gateway_model": decision.model_name,
                    }
                )

                if original_model:
                    response.model = original_model
                return response

            except Exception as e:
                last_exception = e
                if provider_type and api_key:
                    err_s = str(e).lower()
                    if "limit: 0" in err_s and "free_tier" in err_s:
                        self.key_manager.update_key_metadata(
                            provider_type, api_key, {"tier": "free"}
                        )
                    await self.key_manager.report_failure(provider_type, api_key, e)

                if attempt == self.max_retries - 1:
                    error_msg = f"Model: {decision.model_name} (Provider: {provider_type.value}) - Error: {str(e)}"
                    self._logger.error(error_msg)
                    raise Exception(error_msg) from e
                await asyncio.sleep(1)

        raise last_exception or RuntimeError("Retry loop failed")

    def _get_provider_adapter(
        self, provider: ProviderType, api_key: str
    ) -> ILLMProvider:
        adapter_key = (provider, api_key)
        if adapter_key not in self._adapters:
            if provider == ProviderType.GEMINI:
                self._adapters[adapter_key] = GeminiAdapter(api_key)
            elif provider == ProviderType.GROQ:
                self._adapters[adapter_key] = OpenAICompatAdapter(
                    base_url="https://api.groq.com/openai/v1",
                    api_key=api_key,
                    default_model="llama-3.1-8b-instant",
                )
            elif provider == ProviderType.CEREBRAS:
                self._adapters[adapter_key] = OpenAICompatAdapter(
                    base_url="https://api.cerebras.ai/v1",
                    api_key=api_key,
                    default_model="llama3.1-8b",
                )
        return self._adapters[adapter_key]

    async def route_request(self, request: ChatRequest) -> ChatResponse:
        return await self.process_request(request)

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

    def get_supported_models(self) -> list[dict[str, Any]]:
        return self.analyzer.get_supported_models_info()

    def get_all_models(self) -> list[dict[str, Any]]:
        return self.analyzer.get_all_discovered_models_info()

    async def discover_all_models(self) -> None:
        for provider_type in ProviderType:
            try:
                key_status = self.key_manager.get_key_status().get(provider_type)
                if not key_status or key_status.get("total", 0) == 0:
                    continue
                api_key = await self.key_manager.get_next_key(provider_type)
                adapter = self._get_provider_adapter(provider_type, api_key)
                models = await adapter.discover_models()
                for m_info in models:
                    self.analyzer.register_model(m_info["id"], provider_type, m_info)
            except Exception as e:
                self._logger.warning(
                    f"Discovery failed for provider {provider_type.value}: {e}"
                )

        for agent_type in AgentType:
            try:
                adapter = self._get_agent_adapter(agent_type)
                models = await adapter.discover_models()
                for m_info in models:
                    self.analyzer.register_model(m_info["id"], agent_type, m_info)
                self._logger.info(
                    f"✅ Discovered {len(models)} models for agent {agent_type.value}"
                )
            except Exception as e:
                self._logger.warning(
                    f"Discovery failed for agent {agent_type.value}: {e}"
                )

    async def probe_all_keys(self) -> None:
        for provider_type in ProviderType:
            pools = self.key_manager._key_pools.get(provider_type, [])
            for key in pools:
                try:
                    adapter = self._get_provider_adapter(provider_type, key)
                    metadata = await adapter.probe_key(key)
                    self.key_manager.update_key_metadata(provider_type, key, metadata)
                    self._logger.info(
                        f"Probed key {key[:8]}... for {provider_type.value}: tier={metadata.get('tier')}"
                    )
                except Exception as e:
                    self._logger.warning(
                        f"Failed to probe key {key[:8]}... for {provider_type.value}: {e}"
                    )

    async def recover_failed_keys(self) -> None:
        for provider_type in ProviderType:
            failed_keys = self.key_manager.get_failed_keys(provider_type)
            if not failed_keys:
                continue
            self._logger.info(
                f"🔄 Attempting to recover {len(failed_keys)} failed keys for {provider_type.value}"
            )
            for key in failed_keys:
                try:
                    adapter = self._get_provider_adapter(provider_type, key)
                    metadata = await adapter.probe_key(key)
                    await self.key_manager.report_success(provider_type, key)
                    self.key_manager.update_key_metadata(provider_type, key, metadata)
                    self._logger.info(
                        f"✅ Key {key[:8]}... recovered for {provider_type.value}"
                    )
                except Exception as e:
                    self._logger.debug(
                        f"❌ Key {key[:8]}... still failing for {provider_type.value}: {e}"
                    )
