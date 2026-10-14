from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.providers.gemini import GeminiAdapter
from src.core.exceptions import AuthenticationError, RateLimitError, ServiceUnavailableError
from src.domain.models import ChatMessage, ChatRequest

pytestmark = pytest.mark.unit


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
        # Check for google_search (currently used for all versions in this SDK)
        assert "google_search" in tool

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
        assert "google_search" in tool


@pytest.mark.asyncio
async def test_gemini_adapter_response_parsing_with_grounding():
    adapter = GeminiAdapter(api_key="test-key")

    # Mock response
    mock_response = MagicMock()
    # In reality, text might be empty if parts are not text parts,
    # but for this test we'll mock the candidate content parts

    # Mock grounding metadata
    mock_metadata = MagicMock()
    # In the code we use candidate.grounding_metadata.to_dict() if available
    mock_metadata.to_dict.return_value = {
        "search_entry_point": {"rendered_content": "html"}
    }

    mock_candidate = MagicMock()
    mock_candidate.grounding_metadata = mock_metadata

    # Mock candidate content parts
    mock_part = MagicMock()
    mock_part.text = "Grounded answer"
    mock_candidate.content.parts = [mock_part]

    mock_response.candidates = [mock_candidate]

    chat_response = adapter._convert_to_chat_response(mock_response, "gemini-1.5-pro")

    assert chat_response.choices[0].message.content == "Grounded answer"
    assert "grounding_metadata" in chat_response.usage
    assert chat_response.usage["grounding_metadata"] == {
        "search_entry_point": {"rendered_content": "html"}
    }


class TestGenerateErrorClassification:
    """실측(2026-10-13, 수집기 v2 Gemini 엔진 연동 중): 403을 ServiceUnavailableError
    로 분류하면 resilience_manager가 "공급자 장애"로 보고 circuit breaker 실패로도
    집계해(거부된 키 12/13개가 반복 걸림) 서킷이 OPEN되고, 멀쩡한 키 1개까지
    막혔다. 403은 AuthenticationError(그 키만 영구 제외, breaker는 안 건드림)로,
    503("overloaded")은 429와 같은 일시적 신호라 RateLimitError로 분류해야 한다."""

    def _request(self):
        return ChatRequest(
            model="gemini-2.5-flash",
            messages=[ChatMessage(role="user", content="hi")],
        )

    @pytest.mark.asyncio
    async def test_403_raises_authentication_error_not_service_unavailable(self):
        adapter = GeminiAdapter(api_key="test-key")
        with patch("google.generativeai.GenerativeModel") as mock_model_class:
            mock_model_class.return_value.generate_content_async = AsyncMock(
                side_effect=Exception("403 Your project has been denied access")
            )
            with pytest.raises(AuthenticationError):
                await adapter.generate(self._request(), api_key="test-key")

    @pytest.mark.asyncio
    async def test_429_still_raises_rate_limit_error(self):
        adapter = GeminiAdapter(api_key="test-key")
        with patch("google.generativeai.GenerativeModel") as mock_model_class:
            mock_model_class.return_value.generate_content_async = AsyncMock(
                side_effect=Exception("429 Resource has been exhausted")
            )
            with pytest.raises(RateLimitError):
                await adapter.generate(self._request(), api_key="test-key")

    @pytest.mark.asyncio
    async def test_503_overloaded_raises_rate_limit_error_not_service_unavailable(self):
        adapter = GeminiAdapter(api_key="test-key")
        with patch("google.generativeai.GenerativeModel") as mock_model_class:
            mock_model_class.return_value.generate_content_async = AsyncMock(
                side_effect=Exception("503 The model is overloaded. Please try again later.")
            )
            with pytest.raises(RateLimitError) as exc_info:
                await adapter.generate(self._request(), api_key="test-key")
            assert exc_info.value.retry_after == 15.0

    @pytest.mark.asyncio
    async def test_unknown_error_still_raises_service_unavailable_error(self):
        """진짜 분류 불가 오류는 여전히 ServiceUnavailableError(circuit breaker
        보호 유지) — 이번 수정이 보호 자체를 없애지 않았는지 확인."""
        adapter = GeminiAdapter(api_key="test-key")
        with patch("google.generativeai.GenerativeModel") as mock_model_class:
            mock_model_class.return_value.generate_content_async = AsyncMock(
                side_effect=Exception("boom, totally unrelated failure")
            )
            with pytest.raises(ServiceUnavailableError):
                await adapter.generate(self._request(), api_key="test-key")


class TestProbeKeyPropagatesFailures:
    """실측(2026-10-13): 과거 `probe_key`는 이중으로 예외를 삼켜(안쪽 "else:
    tier=free"가 403을 거짓 성공으로, 바깥 try/except가 재-raise 된 429조차
    `{"status": "failed"}` 딕셔너리로 변환) `gateway.probe_active_keys`의 분류·
    영구 제외 로직이 한 번도 안 불렸다. 이제 benign(limit:0/free_tier/404) 만
    빼고 전부 그대로 전파해야 한다(`openai_compat.py`의 같은 메서드와 동일 원칙)."""

    @pytest.mark.asyncio
    async def test_403_propagates_instead_of_returning_fake_success(self):
        adapter = GeminiAdapter(api_key="test-key")
        with patch("google.generativeai.GenerativeModel") as mock_model_class:
            mock_model_class.return_value.generate_content_async = AsyncMock(
                side_effect=Exception("403 Your project has been denied access")
            )
            with pytest.raises(Exception, match="403"):
                await adapter.probe_key("test-key")

    @pytest.mark.asyncio
    async def test_429_still_propagates(self):
        adapter = GeminiAdapter(api_key="test-key")
        with patch("google.generativeai.GenerativeModel") as mock_model_class:
            mock_model_class.return_value.generate_content_async = AsyncMock(
                side_effect=Exception("429 Resource has been exhausted")
            )
            with pytest.raises(Exception, match="429"):
                await adapter.probe_key("test-key")

    @pytest.mark.asyncio
    async def test_free_tier_limit_zero_is_not_an_error(self):
        """"limit: 0"(무료 티어 한도 모델) 메시지는 키 실패가 아니라 그냥
        free tier 로 분류돼 성공 반환된다(기존 동작 유지)."""
        adapter = GeminiAdapter(api_key="test-key")
        with patch("google.generativeai.GenerativeModel") as mock_model_class:
            mock_model_class.return_value.generate_content_async = AsyncMock(
                side_effect=Exception("limit: 0 for this model")
            )
            result = await adapter.probe_key("test-key")
        assert result == {"tier": "free", "status": "active", "can_list_models": True}

    @pytest.mark.asyncio
    async def test_successful_probe_returns_premium_tier(self):
        adapter = GeminiAdapter(api_key="test-key")
        with patch("google.generativeai.GenerativeModel") as mock_model_class:
            mock_model_class.return_value.generate_content_async = AsyncMock(return_value=MagicMock())
            result = await adapter.probe_key("test-key")
        assert result == {"tier": "premium", "status": "active", "can_list_models": True}
