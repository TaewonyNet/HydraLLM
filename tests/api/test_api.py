from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

# Import app from main.py directly
import main
from src.api.v1.dependencies import get_gateway
from src.core.exceptions import RateLimitError, ResourceExhaustedError
from src.domain.models import ChatChoice, ChatMessage, ChatRequest, ChatResponse
from src.services.gateway import Gateway


class TestAPI:
    def setup_method(self):
        self.client = TestClient(main.app)

        # Create mock gateway
        self.mock_gateway = Mock(spec=Gateway)
        self.mock_gateway.process_request = AsyncMock()

        # Override dependency
        main.app.dependency_overrides[get_gateway] = lambda: self.mock_gateway

    def teardown_method(self):
        main.app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful chat completion with mock response."""
        mock_response = ChatResponse(
            id="chatcmpl-test-123",
            object="chat.completion",
            created=1234567890,
            model="gemini-1.5-flash",
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant", content="Hello! How can I help you?"
                    ),
                    finish_reason="stop",
                )
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

        self.mock_gateway.process_request.return_value = mock_response

        request_data = ChatRequest(
            model="gemini-1.5-flash",
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.7,
        )

        response = self.client.post(
            "/v1/chat/completions", json=request_data.model_dump()
        )

        assert response.status_code == 200
        assert response.json()["id"] == "chatcmpl-test-123"
        assert response.json()["model"] == "gemini-1.5-flash"
        assert (
            response.json()["choices"][0]["message"]["content"]
            == "Hello! How can I help you?"
        )

    @pytest.mark.asyncio
    async def test_chat_completion_invalid_request(self):
        """Test chat completion with invalid request."""
        response = self.client.post("/v1/chat/completions", json={})

        assert response.status_code == 422
        assert "detail" in response.json()

    @pytest.mark.asyncio
    async def test_chat_completion_with_streaming(self):
        """Test chat completion with streaming enabled."""
        # Set up mock for streaming test
        mock_response = ChatResponse(
            id="chatcmpl-stream-123",
            object="chat.completion",
            created=1234567890,
            model="gemini-1.5-flash",
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content="Streaming response"),
                    finish_reason="stop",
                )
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )
        self.mock_gateway.process_request.return_value = mock_response

        request_data = ChatRequest(
            model="gemini-1.5-flash",
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.7,
            stream=True,
        )

        response = self.client.post(
            "/v1/chat/completions", json=request_data.model_dump()
        )

        assert response.status_code == 200
        assert b"data:" in response.content
        assert b"chatcmpl-stream-123" in response.content

    @pytest.mark.asyncio
    async def test_chat_completion_with_all_parameters(self):
        """Test chat completion with all parameters."""
        # Set up mock for this test
        mock_response = ChatResponse(
            id="chatcmpl-full-123",
            object="chat.completion",
            created=1234567890,
            model="gemini-1.5-flash",
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content="Full response"),
                    finish_reason="stop",
                )
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )
        self.mock_gateway.process_request.return_value = mock_response

        request_data = ChatRequest(
            model="gemini-1.5-flash",
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.7,
            max_tokens=4096,
            presence_penalty=0.0,
            top_p=1.0,
            frequency_penalty=0.0,
            stop=["###", "\n"],
        )

        response = self.client.post(
            "/v1/chat/completions", json=request_data.model_dump()
        )

        assert response.status_code == 200
        assert "id" in response.json()

    @pytest.mark.asyncio
    async def test_gateway_exception_handling(self):
        """Test exception handling from gateway."""
        self.mock_gateway.process_request.side_effect = ValueError("Invalid request")

        request_data = ChatRequest(
            model="gemini-1.5-flash",
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.7,
        )

        response = self.client.post(
            "/v1/chat/completions", json=request_data.model_dump()
        )

        assert response.status_code == 400
        assert "Invalid request" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_gateway_resource_exhausted(self):
        """Test resource exhausted exception handling."""
        self.mock_gateway.process_request.side_effect = ResourceExhaustedError(
            "All providers exhausted"
        )

        request_data = ChatRequest(
            model="gemini-1.5-flash",
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.7,
        )

        response = self.client.post(
            "/v1/chat/completions", json=request_data.model_dump()
        )

        assert response.status_code == 503
        assert "All providers exhausted" in response.json()["detail"]
        assert "Retry-After" not in response.headers  # retry_after 모르면 헤더 생략

    @pytest.mark.asyncio
    async def test_gateway_circuit_open_sets_retry_after_header(self):
        """Circuit OPEN 거부(strict, retry_after 동반)는 503 + Retry-After 헤더로
        남은 복구 시간을 알려줘야 한다(item 2)."""
        self.mock_gateway.process_request.side_effect = ResourceExhaustedError(
            "Strict provider groq is currently unavailable (Circuit OPEN)",
            retry_after=42.0,
        )

        request_data = ChatRequest(
            model="groq/openai/gpt-oss-120b",
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.7,
        )

        response = self.client.post(
            "/v1/chat/completions", json=request_data.model_dump()
        )

        assert response.status_code == 503
        assert response.headers["Retry-After"] == "42"

    @pytest.mark.asyncio
    async def test_gateway_rate_limit_returns_429_with_retry_after(self):
        """429(레이트리밋)가 strict 재-raise 로 올라오면 500 이 아니라 HTTP 429 +
        Retry-After(공급자 값)로 응답해야 한다(실측 사고: 이전엔 endpoints.py 의
        except 에 RateLimitError 가 없어 일반 500 으로 떨어졌다)."""
        self.mock_gateway.process_request.side_effect = RateLimitError(
            "429 rate limit", retry_after=15.0
        )

        request_data = ChatRequest(
            model="groq/openai/gpt-oss-120b",
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.7,
        )

        response = self.client.post(
            "/v1/chat/completions", json=request_data.model_dump()
        )

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "15"

    @pytest.mark.asyncio
    async def test_gateway_rate_limit_without_provider_value_uses_computed_default(self):
        """공급자가 Retry-After 를 안 알려줘도(retry_after=None) 429 응답엔 계산값
        (분당 한도 기준 60s)으로 헤더를 채운다 — 헤더 자체가 없어지면 안 된다."""
        self.mock_gateway.process_request.side_effect = RateLimitError("429 rate limit")

        request_data = ChatRequest(
            model="groq/openai/gpt-oss-120b",
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.7,
        )

        response = self.client.post(
            "/v1/chat/completions", json=request_data.model_dump()
        )

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"

    @pytest.mark.asyncio
    async def test_gateway_internal_error(self):
        """Test internal server error handling."""
        self.mock_gateway.process_request.side_effect = Exception("Unexpected error")

        request_data = ChatRequest(
            model="gemini-1.5-flash",
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.7,
        )

        response = self.client.post(
            "/v1/chat/completions", json=request_data.model_dump()
        )

        assert response.status_code == 500
        # 내부 예외 원문은 마스킹되고 일반 메시지만 노출된다(정보 누출 방지).
        assert response.json()["detail"] == "Internal server error"

    def test_root_endpoint(self):
        """Test root endpoint."""
        response = self.client.get("/")

        assert response.status_code == 200
        assert response.json()["message"] == "HydraLLM API"
        assert response.json()["docs"] == "/docs"
        assert response.json()["openapi"] == "/openapi.json"

    def test_openapi_endpoint(self):
        """Test OpenAPI endpoint."""
        response = self.client.get("/openapi.json")

        assert response.status_code == 200
        assert "openapi" in response.json()
        assert "info" in response.json()
        assert "paths" in response.json()

    def test_docs_endpoint(self):
        """Test docs endpoint."""
        response = self.client.get("/docs")

        assert response.status_code == 200
        assert "Swagger UI" in response.text

    def test_invalid_endpoint(self):
        """Test invalid endpoint."""
        response = self.client.get("/invalid")

        assert response.status_code == 404
        assert "Not Found" in response.text
