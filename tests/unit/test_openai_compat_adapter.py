from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.providers.openai_compat import OpenAICompatAdapter
from src.domain.models import ChatMessage, ChatRequest

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_openai_compat_auto_model_mapping():
    default_model = "llama-3.3-70b-versatile"
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        default_model=default_model,
    )

    mock_choice = MagicMock()
    mock_choice.message.content = "Hello"
    mock_choice.finish_reason = "stop"

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 10
    mock_usage.completion_tokens = 5
    mock_usage.total_tokens = 15

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.id = "test-id"
    mock_response.object = "chat.completion"
    mock_response.created = 12345
    mock_response.model = default_model
    mock_response.usage = mock_usage

    adapter.client.chat.completions.create = AsyncMock(return_value=mock_response)

    auto_hints = ["auto", "mllm/auto", "groq", "llama"]

    for hint in auto_hints:
        request = ChatRequest(
            model=hint, messages=[ChatMessage(role="user", content="Hi")]
        )

        await adapter.generate(request, api_key="test-key")

        args, kwargs = adapter.client.chat.completions.create.call_args
        assert kwargs["model"] == default_model, f"Failed for hint: {hint}"
        assert kwargs["model"] != hint


@pytest.mark.asyncio
async def test_openai_compat_explicit_model_preserved():
    adapter = OpenAICompatAdapter(
        base_url="https://api.groq.com/openai/v1",
        api_key="test-key",
        default_model="llama-default",
    )

    mock_choice = MagicMock()
    mock_choice.message.content = "Hello"
    mock_choice.finish_reason = "stop"
    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 10
    mock_usage.completion_tokens = 5
    mock_usage.total_tokens = 15

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.id = "test-id"
    mock_response.object = "chat.completion"
    mock_response.created = 12345
    mock_response.model = "llama-3.1-8b-instant"
    mock_response.usage = mock_usage

    adapter.client.chat.completions.create = AsyncMock(return_value=mock_response)

    explicit_model = "llama-3.1-8b-instant"
    request = ChatRequest(
        model=explicit_model, messages=[ChatMessage(role="user", content="Hi")]
    )

    await adapter.generate(request, api_key="test-key")

    args, kwargs = adapter.client.chat.completions.create.call_args
    assert kwargs["model"] == explicit_model
