from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.enums import ProviderType
from src.domain.models import ChatRequest, ChatResponse, RoutingDecision
from src.services.resilience_manager import ResilienceManager


@pytest.mark.asyncio
async def test_resilience_manager_retry_logic():
    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = []

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    mock_adapter = AsyncMock()
    mock_response = MagicMock(spec=ChatResponse)
    mock_response.usage = {}
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Success"
    mock_response.model = "test-model"
    mock_adapter.generate.side_effect = [Exception("api_key_invalid"), mock_response]

    get_adapter = MagicMock(return_value=mock_adapter)
    mock_agent_processor = AsyncMock(return_value=mock_response)
    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=get_adapter,
        process_with_agent=mock_agent_processor
    )


    key_manager.get_next_key = AsyncMock(return_value="test-key")
    key_manager.report_success = AsyncMock()
    key_manager.report_failure = AsyncMock()
    key_manager.get_available_keys_count.return_value = 1

    response, parts = await rm.execute_with_resilience(request=ChatRequest(model="test", messages=[]), decision=RoutingDecision(provider=ProviderType.GEMINI, model_name="gemini-pro", reason="test"))

    assert response == mock_response
    assert mock_adapter.generate.call_count == 2
    key_manager.report_failure.assert_called_once()
    key_manager.report_success.assert_called_once()

@pytest.mark.asyncio
async def test_resilience_manager_fallback_to_next_provider():
    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = ["gemini", "groq"]
    analyzer.get_default_model_for_provider.return_value = "groq-model"

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    gemini_adapter = AsyncMock()
    gemini_adapter.generate.side_effect = Exception("Gemini Down")

    groq_adapter = AsyncMock()
    mock_response = MagicMock(spec=ChatResponse)
    mock_response.usage = {}
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Groq Success"
    mock_response.model = "groq-model"
    groq_adapter.generate.return_value = mock_response

    def get_adapter(provider, api_key):
        if provider == ProviderType.GEMINI:
            return gemini_adapter
        if provider == ProviderType.GROQ:
            return groq_adapter
        return None

    mock_agent_processor = AsyncMock(return_value=mock_response)

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=get_adapter,
        process_with_agent=mock_agent_processor
    )

    key_manager.get_next_key = AsyncMock(return_value="key")
    key_manager.get_available_keys_count.side_effect = [1, 1, 1, 0, 1, 1, 1, 0]

    request = ChatRequest(model="auto", messages=[])
    decision = RoutingDecision(provider=ProviderType.GEMINI, model_name="gemini-pro", reason="test")

    response, parts = await rm.execute_with_resilience(request, decision)

    assert response == mock_response
    assert gemini_adapter.generate.call_count == 3
    assert response.model == "groq-model"


@pytest.mark.asyncio
async def test_no_cloud_keys_falls_back_to_local_agent():
    """클라우드 키가 하나도 없으면 auto 요청은 클라우드 시도를 건너뛰고 즉시
    로컬(Ollama) final_fallback 한다(graceful degradation·로컬 주권)."""
    key_manager = MagicMock()
    analyzer = MagicMock()
    analyzer.provider_priority = ["gemini", "groq", "cerebras"]

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    cloud_adapter = AsyncMock()  # 호출되면 안 됨
    get_adapter = MagicMock(return_value=cloud_adapter)

    agent_response = MagicMock(spec=ChatResponse)
    agent_response.usage = {}
    agent_response.choices = [MagicMock()]
    agent_response.choices[0].message.content = "local"
    agent_response.model = "LOCAL-AGENT/OLLAMA/llama3"
    mock_agent_processor = AsyncMock(return_value=agent_response)

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=get_adapter,
        process_with_agent=mock_agent_processor,
    )
    key_manager.get_available_keys_count.return_value = 0  # 모든 클라우드 키 부재
    key_manager.get_next_key = AsyncMock()

    request = ChatRequest(model="auto", messages=[])
    decision = RoutingDecision(
        provider=ProviderType.GEMINI, model_name="gemini-pro", reason="test"
    )
    response, _ = await rm.execute_with_resilience(request, decision)

    assert response == agent_response
    mock_agent_processor.assert_awaited()  # final_fallback 경유
    cloud_adapter.generate.assert_not_called()  # 클라우드 시도 건너뜀
