import asyncio
import logging
import re
from typing import Any, cast

from ..adapters.providers.gemini import GeminiAdapter
from ..adapters.providers.local_cli import LocalCLIAdapter
from ..adapters.providers.openai_compat import OpenAICompatAdapter
from ..core.config import settings
from ..core.exceptions import RateLimitError, ResourceExhaustedError
from ..domain.enums import AgentType, ProviderType, TierType
from ..domain.interfaces import ILLMProvider, IRouter
from ..domain.models import ChatMessage, ChatRequest, ChatResponse, RoutingDecision
from .analyzer import ContextAnalyzer
from .compressor import ContextCompressor
from .key_manager import KeyManager
from .scraper import ScrapeMode, WebScraper
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
        onboarding = self.session_manager._get_setting_sync(
            "onboarding_completed", False
        )
        enabled = self.session_manager._get_setting_sync("enabled_models", None)

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

        # ─── 세션 관리: load context → 새 메시지만 추가 ───
        if request.session_id:
            # 서버 DB에서 compaction 경계 이후 히스토리 로드
            history = await self.session_manager.load_context(request.session_id)

            # 새 user 메시지를 DB에 저장
            new_user_msgs = [m for m in request.messages if m.role == "user"]
            if new_user_msgs:
                last_user = new_user_msgs[-1]
                await self.session_manager.save_message(
                    request.session_id, last_user.role, last_user.content
                )

            # 히스토리 + 새 메시지 조립 (중복 제거)
            if history:
                seen = set()
                merged: list[ChatMessage] = []
                for msg in history + request.messages:
                    key = (msg.role, str(msg.content).strip()[:200])
                    if key not in seen:
                        merged.append(msg)
                        seen.add(key)
                request.messages = merged

        if do_compress and len(request.messages) > 4:
            estimated = request.estimate_token_count()
            if estimated > settings.max_tokens_fast_model:
                self._logger.info(f"🗜️ Compressing session history: {estimated} tokens")
                request.messages = await self._compress_session_history(
                    request.messages
                )

        content_text = ""
        last_msg = request.messages[-1]
        if last_msg.role == "user":
            if isinstance(last_msg.content, str):
                content_text = last_msg.content
            elif isinstance(last_msg.content, list):
                for part in last_msg.content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        content_text += part.get("text", "")

        web_required = self.analyzer.detect_web_intent(request)
        self._logger.info(
            f"Analysis: web_required={web_required}, auto_fetch={do_auto_fetch}"
        )

        # 1) URL 추출: do_auto_fetch이거나, web_intent가 있으면 URL 감지

        urls_to_fetch: list[str] = []
        if (do_auto_fetch or web_required) and content_text:
            urls_to_fetch = re.findall(
                r"https?://[^\s/$.?#].[^\sㄱ-ㅎㅏ-ㅣ가-힣]*", content_text
            )

        if request.web_fetch and request.web_fetch not in urls_to_fetch:
            urls_to_fetch.append(request.web_fetch)

        scrape_mode = cast(ScrapeMode, settings.default_scrape_mode)

        # 2) URL이 있으면 개별 fetch
        context_blocks: list[str] = []
        for url in urls_to_fetch:
            self._logger.info(f"🌐 Fetching content for: {url}")
            try:
                raw_content = await self.scraper.scrape_url(
                    url,
                    mode=scrape_mode,
                )

                if raw_content and not any(
                    err in raw_content for err in ["Failed to fetch", "Error scraping"]
                ):
                    if do_compress:
                        self._logger.info(f"🗜️ Compressing fetched content for {url}")
                        raw_content = self.compressor.compress(
                            raw_content, instruction=f"Focus on {content_text}"
                        )

                    context_blocks.append(
                        f"--- SOURCE URL: {url} ---\n{raw_content}\n--- END CONTENT ---"
                    )
                else:
                    self._logger.warning(f"⚠️ Fetch failed or returned empty for {url}")
                    context_blocks.append(
                        f"--- SOURCE URL: {url} ---\n[STATUS: FETCH_FAILED]\n--- END CONTENT ---"
                    )
            except Exception as e:
                self._logger.error(f"❌ Error fetching {url}: {e}")
                context_blocks.append(
                    f"--- SOURCE URL: {url} ---\n[STATUS: ERROR: {str(e)}]\n--- END CONTENT ---"
                )

        # 3) 검색 트리거: has_search 플래그, 또는 web_intent인데 URL fetch 결과가 없을 때
        should_trigger_search = bool(request.has_search)
        if (
            not should_trigger_search
            and not context_blocks
            and content_text
            and web_required
        ):
            should_trigger_search = True
            self._logger.info(f"🔍 Auto-detected web intent: {content_text[:50]}...")

        if should_trigger_search and not context_blocks and content_text:
            self._logger.info(f"🔍 Performing web search for: {content_text[:50]}...")
            try:
                search_results = await self.scraper.search_and_scrape(
                    content_text,
                    mode=scrape_mode,
                )

                if search_results and "No search results found" not in search_results:
                    if do_compress:
                        self._logger.info("🗜️ Compressing search results")
                        search_results = self.compressor.compress(
                            search_results, instruction=content_text
                        )

                    context_blocks.append(
                        f"--- WEB SEARCH RESULTS ---\n{search_results}\n--- END SEARCH RESULTS ---"
                    )
                    self._logger.info("✅ Web search results successfully integrated")
                else:
                    self._logger.warning("⚠️ Web search returned no useful results")
                    context_blocks.append(
                        "--- WEB SEARCH RESULTS ---\n[STATUS: NO_RESULTS_FOUND]\n--- END SEARCH RESULTS ---"
                    )
            except Exception as e:
                self._logger.error(f"❌ Web search failed: {e}")
                context_blocks.append(
                    f"--- WEB SEARCH RESULTS ---\n[STATUS: ERROR: {str(e)}]\n--- END SEARCH RESULTS ---"
                )

            request.has_search = False

        if context_blocks:
            combined_context = "\n\n".join(context_blocks)
            request.messages.insert(
                0,
                ChatMessage(
                    role="system",
                    content="[REAL-TIME WEB CONTEXT ENABLED]\n"
                    "The system has attempted to retrieve real-time information from the web to answer your request. "
                    "Below is the retrieved data. Successful retrievals should be treated as the primary factual source. "
                    "If a retrieval shows [STATUS: FETCH_FAILED] or [STATUS: NO_RESULTS_FOUND], inform the user that you tried to access the web but couldn't get the data.\n\n"
                    + combined_context,
                    name=None,
                ),
            )

            if request.tools:
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
            self._logger.debug(
                f"FULL REQUEST BODY: {request.model_dump_json(indent=2)}"
            )

        response = await self._process_with_retries(request, decision)

        # ─── 응답 후 세션 저장 + overflow 체크 ───
        if request.session_id and response and response.choices:
            await self.session_manager.save_message(
                request.session_id, "assistant", response.choices[0].message.content
            )

            # 토큰 overflow 감지 → compaction 트리거
            if do_compress and self.session_manager.is_overflow(request.session_id):
                self._logger.info(
                    f"🗜️ Session {request.session_id} overflow detected, compacting..."
                )
                await self.session_manager.compact(request.session_id, self.compressor)

        if settings.debug:
            self._logger.debug(
                f"FULL RESPONSE BODY: {response.model_dump_json(indent=2)}"
            )

        return response

    async def _process_with_agent(
        self, request: ChatRequest, decision: RoutingDecision
    ) -> ChatResponse:
        agent_type = decision.agent
        if not agent_type:
            msg = "No agent selected"
            raise ResourceExhaustedError(msg)

        discovered_models = self.analyzer.get_all_discovered_models_info()
        agent_models = [
            m["id"]
            for m in discovered_models
            if m.get("owned_by") == "local-agent"
            and (
                m["id"].lower().startswith(agent_type.value.lower())
                or m.get("display_name", "")
                .lower()
                .startswith(agent_type.value.lower())
            )
        ]

        if not agent_models:
            self._logger.info(f"Refreshing models for agent {agent_type.value}...")
            adapter = self._get_agent_adapter(agent_type)
            new_models = await adapter.discover_models()
            for m_info in new_models:
                self.analyzer.register_model(m_info["id"], agent_type, m_info)
            agent_models = [m["id"] for m in new_models]

        original_model = request.model

        if (
            decision.model_name == agent_type.value
            or decision.model_name not in agent_models
        ):
            if agent_models:
                best_fallback = next(
                    (m for m in agent_models if "llama" in m.lower()), agent_models[0]
                )
                self._logger.info(
                    f"Mapping generic/missing model {decision.model_name} to discovered agent model: {best_fallback}"
                )
                decision.model_name = best_fallback
            else:
                self._logger.warning(
                    f"No models discovered for agent {agent_type.value}, using default."
                )

        request.model = decision.model_name

        try:
            adapter = self._get_agent_adapter(agent_type)
            response = await adapter.generate(request, api_key="local-agent")

            if not response.usage:
                response.usage = {}
            response.usage.update(
                {
                    "gateway_provider": agent_type.value,
                    "gateway_key_index": 0,
                    "gateway_model": decision.model_name,
                }
            )

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
                tier_str = model_info.get("tier", "free")
                min_tier = TierType(tier_str) if isinstance(tier_str, str) else tier_str

                try:
                    api_key = await self.key_manager.get_next_key(
                        provider_type, min_tier=min_tier
                    )
                except (ResourceExhaustedError, RateLimitError):
                    self._logger.warning(
                        f"Provider {provider_type.value} exhausted/rate-limited. Falling back..."
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
                                    if min_tier == TierType.FREE
                                    else settings.default_premium_model
                                )
                            elif p == ProviderType.GROQ:
                                decision.model_name = "llama-3.3-70b-versatile"
                            found_fallback = True
                            break
                        except ResourceExhaustedError:
                            continue
                    if not found_fallback:
                        self._logger.warning(
                            "All providers exhausted. Falling back to local agents..."
                        )
                        decision.agent = AgentType.OLLAMA
                        decision.model_name = "ollama"
                        return await self._process_with_agent(request, decision)

                if not api_key:
                    msg = "API key allocation failed"
                    raise ResourceExhaustedError(msg)

                request.model = decision.model_name
                adapter = self._get_provider_adapter(provider_type, api_key)
                response = await adapter.generate(request, api_key)

                await self.key_manager.report_success(provider_type, api_key)

                if not response.usage:
                    response.usage = {}

                key_index = self.key_manager.get_key_index(provider_type, api_key)

                response.usage.update(
                    {
                        "gateway_provider": provider_type.value,
                        "gateway_key_index": key_index,
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
                            provider_type, api_key, {"tier": TierType.FREE}
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

    async def _compress_session_history(
        self, messages: list[ChatMessage]
    ) -> list[ChatMessage]:
        if len(messages) <= 2:
            return messages

        system_msg = next((m for m in messages if m.role == "system"), None)
        last_msg = messages[-1]

        other_msgs = [
            m
            for m in messages
            if m != system_msg and m != last_msg and m.role != "system"
        ]

        if not other_msgs:
            return messages

        to_compress = ""
        for m in other_msgs:
            content = m.content
            if isinstance(content, list):
                content = " ".join([str(p) for p in content])
            to_compress += f"{m.role}: {content}\n"

        self._logger.info(f"Compressing {len(to_compress)} chars of history")
        summary = self.compressor.compress(
            to_compress, instruction="Summarize the conversation so far."
        )

        new_messages = []
        if system_msg:
            new_messages.append(system_msg)

        new_messages.append(
            ChatMessage(
                role="system",
                content=f"[CONVERSATION SUMMARY]\n{summary}",
                name="history_compressor",
            )
        )
        new_messages.append(last_msg)

        return new_messages

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

                if provider_type == ProviderType.GEMINI and models:
                    flash_models = [
                        m["id"]
                        for m in models
                        if "flash" in m["id"].lower() and "lite" not in m["id"].lower()
                    ]
                    if flash_models:
                        latest_flash = sorted(flash_models, reverse=True)[0]
                        if latest_flash > settings.default_free_model:
                            self._logger.info(
                                f"✨ Found newer Gemini Flash model via discovery: {latest_flash} (Current: {settings.default_free_model})"
                            )
                            settings.default_free_model = latest_flash
                            settings.default_premium_model = latest_flash

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
