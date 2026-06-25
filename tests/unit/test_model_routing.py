from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.enums import AgentType, ProviderType
from src.domain.models import ChatMessage, ChatRequest
from src.services.gateway import Gateway


@pytest.mark.asyncio
async def test_specific_model_routing_no_auto():
    gateway = Gateway()

    # Given: Register a mock model for exact matching
    gateway.analyzer.register_model("gemini-1.5-pro", ProviderType.GEMINI)

    # Given: A request for a specific model (No auto-routing)
    model_name = "gemini/gemini-1.5-pro"
    request = ChatRequest(
        model=model_name,
        messages=[ChatMessage(role="user", content="Test specific model")]
    )

    # Given: In strict mode (model name has provider prefix), the analyzer should return that exact model
    decision = await gateway.analyzer.analyze(request)

    assert decision.provider == ProviderType.GEMINI
    assert decision.model_name == "gemini-1.5-pro"

@pytest.mark.asyncio
async def test_local_agent_specific_routing():
    gateway = Gateway()

    # Given: different local agent formats
    formats = [
        ("LOCAL-AGENT/OLLAMA/qwen2.5:7b-instruct", AgentType.OLLAMA, "qwen2.5:7b-instruct"),
        ("OPENCODE/github-copilot/gpt-4o", AgentType.OPENCODE, "github-copilot/gpt-4o"),
    ]

    for input_model, expected_agent, expected_model in formats:
        request = ChatRequest(
            model=input_model,
            messages=[ChatMessage(role="user", content="Test local agent")]
        )

        decision = await gateway.analyzer.analyze(request)

        assert decision.agent == expected_agent
        assert decision.model_name == expected_model
        assert decision.provider is None

@pytest.mark.asyncio
async def test_resilience_manager_strict_mode_fail_fast():
    from src.core.exceptions import ResourceExhaustedError
    from src.domain.models import RoutingDecision
    from src.services.resilience_manager import ResilienceManager

    key_manager = MagicMock()
    key_manager.report_failure = AsyncMock()
    analyzer = MagicMock()
    # Given: Analyzer priority is set, but we want to ensure it's ignored in strict mode
    analyzer.provider_priority = ["gemini", "groq"]

    breakers = {p: MagicMock() for p in ProviderType}
    for b in breakers.values():
        b.is_available.return_value = True

    # Given: Provider fails
    mock_adapter = AsyncMock()
    mock_adapter.generate.side_effect = ResourceExhaustedError("Quota reached")

    def get_adapter(p, k): return mock_adapter

    rm = ResilienceManager(
        key_manager=key_manager,
        analyzer=analyzer,
        breakers=breakers,
        session_manager=AsyncMock(),
        get_provider_adapter=get_adapter,
        process_with_agent=AsyncMock()
    )

    # Given: Strict routing request — strict 여부는 이제 decision.strict 로 전달된다(S1).
    # (analyzer 가 원본 "gemini/pro" 힌트를 보고 strict=True 로 결정해 실어줌)
    request = ChatRequest(model="gemini/pro", messages=[])
    decision = RoutingDecision(
        provider=ProviderType.GEMINI, model_name="pro", reason="test", strict=True
    )

    key_manager.get_available_keys_count.return_value = 0
    key_manager.get_next_key = AsyncMock(return_value="key")

    # Then: It should raise the error immediately instead of trying "groq"
    with pytest.raises(ResourceExhaustedError):
        await rm.execute_with_resilience(request, decision)

    # Then: Verify it only tried GEMINI
    assert mock_adapter.generate.call_count <= 3
