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

    gateway.key_manager.get_available_keys_count = MagicMock(side_effect=[0, 1, 1])
    gateway.key_manager.get_next_key = AsyncMock(
        side_effect=["groq-key-1", "gemini-key-1"]
    )

    gateway.analyzer._provider_priority = ["groq", "gemini"]

    mock_decision = MagicMock()
    mock_decision.provider = ProviderType.GROQ
    mock_decision.model_name = "llama-3.3-70b-versatile"
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

    gateway._get_provider_adapter = MagicMock(side_effect=get_adapter_mock)

    request = ChatRequest(
        model="auto", messages=[ChatMessage(role="user", content="Hi fallback")]
    )

    response = await gateway.process_request(request)

    assert response.choices[0].message.content == "Gemini Fallback Success"

    args, kwargs = mock_gemini_adapter.generate.call_args
    # fallback 시 analyzer.get_default_model_for_provider(GEMINI) = settings.default_free_model 전달.
    assert args[0].model == settings.default_free_model
    assert args[0].model != "llama-3.3-70b-versatile"
