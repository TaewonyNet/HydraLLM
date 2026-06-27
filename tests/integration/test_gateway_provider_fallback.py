from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import settings
from src.core.exceptions import RateLimitError
from src.domain.enums import ProviderType
from src.domain.models import ChatMessage, ChatRequest
from src.services.gateway import Gateway


@pytest.mark.asyncio
async def test_gateway_provider_fallback_with_model_resolution():
    gateway = Gateway()

    # 호출 순서가 아니라 provider 별로 결정(라우팅 변경에 견고). GROQ는 키 0 → 첫 실패
    # 후 즉시 GEMINI 로 폴백, GEMINI는 키 있음.
    gateway.key_manager.get_available_keys_count = MagicMock(
        side_effect=lambda p: 1 if p == ProviderType.GEMINI else 0
    )
    gateway.key_manager.get_next_key = AsyncMock(
        side_effect=["groq-key-1", "gemini-key-1"]
    )

    gateway.analyzer._provider_priority = ["groq", "gemini"]

    mock_decision = MagicMock()
    mock_decision.provider = ProviderType.GROQ
    mock_decision.agent = None
    mock_decision.model_name = "llama-3.3-70b-versatile"
    mock_decision.strict = False  # 비-strict: 교차 공급자 폴백 허용(S1 계약)
    gateway.analyzer.analyze = AsyncMock(return_value=mock_decision)

    mock_groq_adapter = AsyncMock()
    mock_groq_adapter.generate = AsyncMock(
        side_effect=RateLimitError("Groq Quota Exceeded")
    )

    mock_gemini_adapter = AsyncMock()
    mock_gemini_response = MagicMock()
    mock_gemini_response.choices = [MagicMock()]
    mock_gemini_response.choices[0].message.content = "Gemini Fallback Success"

    mock_gemini_response.usage = {
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
    }
    mock_gemini_adapter.generate = AsyncMock(return_value=mock_gemini_response)

    def get_adapter_mock(provider, key):
        if provider == ProviderType.GROQ:
            return mock_groq_adapter
        if provider == ProviderType.GEMINI:
            return mock_gemini_adapter
        return MagicMock()

    # resilience 가 생성 시점에 캡처한 어댑터 팩토리를 교체해야 실제로 mock 이 적용된다.
    gateway.resilience._get_provider_adapter = MagicMock(side_effect=get_adapter_mock)

    request = ChatRequest(
        model="auto", messages=[ChatMessage(role="user", content="Hi fallback")]
    )

    response = await gateway.process_request(request)

    assert response.choices[0].message.content == "Gemini Fallback Success"

    args, kwargs = mock_gemini_adapter.generate.call_args
    # fallback 시 analyzer.get_default_model_for_provider(GEMINI) = settings.default_free_model 전달.
    assert args[0].model == settings.default_free_model
    assert args[0].model != "llama-3.3-70b-versatile"


@pytest.mark.asyncio
async def test_strict_hint_no_cross_provider_fallback():
    """명시적 provider/model 힌트(strict)는 해당 공급자 실패 시 교차 폴백하지 않고 raise.

    analyzer(실제) 가 "gemini/..." 힌트로 decision.strict=True 를 설정하고, gateway 경유
    end-to-end 로 strict 가 보존됨을 검증(S1). 이전엔 request.model 덮어쓰기로 무력화됐다.
    """
    gateway = Gateway()
    gateway.key_manager.get_available_keys_count = MagicMock(return_value=1)
    gateway.key_manager.get_next_key = AsyncMock(return_value="key")
    gateway.key_manager.report_failure = AsyncMock()
    gateway.web_context.enrich_request = AsyncMock(return_value=([], None))

    gemini_adapter = AsyncMock()
    gemini_adapter.generate = AsyncMock(side_effect=RateLimitError("Gemini down"))
    other_adapter = AsyncMock()  # 교차 폴백되면 호출됨 — 호출되면 안 됨

    def get_adapter(provider, key):
        return gemini_adapter if provider == ProviderType.GEMINI else other_adapter

    gateway.resilience._get_provider_adapter = MagicMock(side_effect=get_adapter)

    request = ChatRequest(
        model="gemini/gemini-1.5-pro",
        messages=[ChatMessage(role="user", content="hi")],
    )

    with pytest.raises(RateLimitError):  # strict → 즉시 실패(폴백 없음)
        await gateway.process_request(request)

    other_adapter.generate.assert_not_called()  # Groq/Cerebras 로 폴백하지 않음
