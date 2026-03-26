from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.providers.gemini import GeminiAdapter
from src.domain.models import ChatMessage, ChatRequest


@pytest.mark.asyncio
async def test_gemini_adapter_tools_selection():
    adapter = GeminiAdapter(api_key="test-key")

    # Mock genai.GenerativeModel
    with patch("google.generativeai.GenerativeModel") as mock_model_class:
        mock_model_instance = mock_model_class.return_value
        mock_model_instance.generate_content_async = AsyncMock()

        # Test Gemini 1.5 model
        request_1_5 = ChatRequest(
            model="gemini-1.5-pro",
            messages=[ChatMessage(role="user", content="Hello")],
            has_search=True,
        )
        await adapter.generate(request_1_5, api_key="test-key")

        # Check tools passed to generate_content_async
        _, kwargs = mock_model_instance.generate_content_async.call_args
        tools = kwargs.get("tools")
        assert tools is not None
        assert len(tools) == 1

        tool = tools[0]
        # Check for google_search_retrieval (currently used for all versions in this SDK)
        assert "google_search_retrieval" in tool

        # Test Gemini 2.0 model
        request_2_0 = ChatRequest(
            model="gemini-2.0-flash",
            messages=[ChatMessage(role="user", content="Hello")],
            has_search=True,
        )
        await adapter.generate(request_2_0, api_key="test-key")

        # Check tools passed to generate_content_async
        _, kwargs = mock_model_instance.generate_content_async.call_args
        tools = kwargs.get("tools")
        assert tools is not None
        assert len(tools) == 1

        tool = tools[0]
        assert "google_search_retrieval" in tool


@pytest.mark.asyncio
async def test_gemini_adapter_response_parsing_with_grounding():
    adapter = GeminiAdapter(api_key="test-key")

    # Mock response
    mock_response = MagicMock()
    mock_response.text = "Grounded answer"

    # Mock grounding metadata
    mock_metadata = MagicMock()
    # In the code we use candidate.grounding_metadata.to_dict() if available
    mock_metadata.to_dict.return_value = {
        "search_entry_point": {"rendered_content": "html"}
    }

    mock_candidate = MagicMock()
    mock_candidate.grounding_metadata = mock_metadata
    mock_response.candidates = [mock_candidate]

    chat_response = adapter._convert_to_chat_response(mock_response, "gemini-1.5-pro")

    assert chat_response.choices[0].message.content == "Grounded answer"
    assert "grounding_metadata" in chat_response.usage
    assert chat_response.usage["grounding_metadata"] == {
        "search_entry_point": {"rendered_content": "html"}
    }
