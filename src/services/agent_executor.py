import logging
from collections.abc import Callable

from src.core.exceptions import ResourceExhaustedError
from src.domain.enums import AgentType
from src.domain.interfaces import ILLMProvider
from src.domain.models import ChatRequest, ChatResponse, RoutingDecision
from src.services.analyzer import ContextAnalyzer

logger = logging.getLogger(__name__)


class AgentExecutor:
    """로컬 에이전트(Ollama/OpenCode/OpenClaw) 실행과 채팅 가능 모델 해석을 담당한다.

    Gateway 의 "얇은 오케스트레이터" 원칙에 따라 에이전트 모델 디스커버리·해석·실행
    로직을 분리한 협업자. ResilienceManager 가 `process` 를 주입받아 호출한다.
    어댑터 캐시는 Gateway 가 소유하므로 어댑터 팩토리(`get_agent_adapter`)를 주입받는다.
    """

    _CHAT_MODEL_PREFERENCE: tuple[str, ...] = (
        "llama",
        "qwen",
        "mistral",
        "gemma",
        "phi",
        "deepseek",
        "yi",
        "wizardlm",
    )

    _NON_CHAT_MODEL_MARKERS: tuple[str, ...] = (
        "embed",
        "embedding",
        "rerank",
        "vision-adapter",
        "bge-",
        "gte-",
        "e5-",
        "nomic-embed",
        "mxbai-embed",
        "jina-embed",
        "snowflake-arctic-embed",
        "whisper",
        "clip",
    )

    def __init__(
        self,
        analyzer: ContextAnalyzer,
        get_agent_adapter: Callable[[AgentType], ILLMProvider],
    ) -> None:
        self.analyzer = analyzer
        self._get_agent_adapter = get_agent_adapter

    def _is_chat_capable_model(self, model_id: str) -> bool:
        lowered = model_id.lower()
        return not any(marker in lowered for marker in self._NON_CHAT_MODEL_MARKERS)

    def _pick_preferred_chat_model(self, models: list[str]) -> str | None:
        chat_models = [m for m in models if self._is_chat_capable_model(m)]
        if not chat_models:
            return None
        for preferred in self._CHAT_MODEL_PREFERENCE:
            for m in chat_models:
                if preferred in m.lower():
                    return m
        return chat_models[0]

    async def process(
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
            if m.get("owned_by") == agent.value
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
            except Exception as disc_err:
                logger.warning(
                    f"Failed to discover models for agent {agent.value}: {disc_err}"
                )

        model_requested = decision.model_name
        needs_resolution = (
            not model_requested
            or model_requested == agent.value
            or model_requested == "auto"
            or model_requested not in agent_models
            or not self._is_chat_capable_model(model_requested)
        )

        if needs_resolution:
            target = self._pick_preferred_chat_model(agent_models)
            if target is None:
                fallback_model = "llama3"
                logger.warning(
                    f"Agent {agent.value} has no chat-capable model discovered; "
                    f"falling back to '{fallback_model}'"
                )
                decision.model_name = fallback_model
                request.model = fallback_model
            else:
                decision.model_name = target
                request.model = target

        adapter = self._get_agent_adapter(agent)
        resp = await adapter.generate(request, "local-agent")

        full_model_path = f"LOCAL-AGENT/{agent.value.upper()}/{decision.model_name}"
        if decision.model_name.startswith(agent.value + "/"):
            m_parts = decision.model_name.split("/", 1)
            full_model_path = f"LOCAL-AGENT/{m_parts[0].upper()}/{m_parts[1]}"

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
