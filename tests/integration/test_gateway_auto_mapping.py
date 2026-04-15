import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.enums import ProviderType
from src.domain.models import ChatMessage, ChatRequest
from src.services.gateway import Gateway


@pytest.mark.asyncio
async def test_gateway_auto_model_integration():
    gateway = Gateway()

    gateway.key_manager.get_next_key = AsyncMock(return_value="test-groq-key")
    gateway.key_manager.get_key_status = MagicMock(
        return_value={
            ProviderType.GROQ: {
                "active": 1,
                "failed": 0,
                "total": 1,
                "keys": [{"tier": "free", "status": "active"}],
            }
        }
    )

    mock_decision = MagicMock()
    mock_decision.provider = ProviderType.GROQ
    mock_decision.agent = None
    mock_decision.model_name = "llama-3.3-70b-versatile"
    gateway.analyzer.analyze = AsyncMock(return_value=mock_decision)

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Integration Success"
    mock_response.usage = {
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
    }

    mock_adapter = AsyncMock()
    mock_adapter.generate = AsyncMock(return_value=mock_response)
    gateway._get_provider_adapter = MagicMock(return_value=mock_adapter)

    request = ChatRequest(
        model="auto",
        messages=[ChatMessage(role="user", content=f"Hi {uuid.uuid4()}")],
        auto_web_fetch=False,
    )

    await gateway.process_request(request)

    gateway.analyzer.analyze.assert_called_once()

    args, kwargs = mock_adapter.generate.call_args
    assert args[0].model == "llama-3.3-70b-versatile"
    assert args[0].model != "auto"
