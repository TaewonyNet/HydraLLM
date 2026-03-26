import sys
from pathlib import Path

test_dir = Path(__file__).parent
project_dir = test_dir.parent

src_path = project_dir / "src"
sys.path.insert(0, str(src_path))

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

# Import app from main.py directly
import main
from src.api.v1.dependencies import get_gateway
from src.domain.models import ChatChoice, ChatMessage, ChatRequest, ChatResponse
from src.services.gateway import Gateway


class TestIntegration:
    def setup_method(self):
        self.client = TestClient(main.app)

        # Create mock gateway with proper response
        self.mock_gateway = Mock(spec=Gateway)
        mock_response = ChatResponse(
            id="chatcmpl-test-123",
            object="chat.completion",
            created=1234567890,
            model="gemini-1.5-flash",
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content="Hello!"),
                    finish_reason="stop",
                )
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )
        self.mock_gateway.process_request = AsyncMock(return_value=mock_response)
        self.mock_gateway.key_manager = Mock()
        self.mock_gateway.key_manager.get_key_status = Mock(return_value={})

        # Override dependency
        main.app.dependency_overrides[get_gateway] = lambda: self.mock_gateway

    def teardown_method(self):
        main.app.dependency_overrides.clear()

    def test_app_exists(self):
        assert main.app is not None

    @pytest.mark.asyncio
    async def test_chat_completion_endpoint(self):
        """Test that the chat completion endpoint is accessible."""
        request_data = ChatRequest(
            model="gemini-1.5-flash",
            messages=[{"role": "user", "content": "Hello, how are you?"}],
            temperature=0.7,
        )

        response = self.client.post(
            "/v1/chat/completions", json=request_data.model_dump()
        )
        assert response.status_code == 200

    def test_chat_completion_endpoint_sync(self):
        request_data = {
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": "Hello, how are you?"}],
            "temperature": 0.7,
        }

        response = self.client.post("/v1/chat/completions", json=request_data)
        assert response.status_code == 200
        assert "choices" in response.json()
        assert "model" in response.json()

    def test_invalid_request(self):
        response = self.client.post("/v1/chat/completions", json={})
        assert response.status_code == 422

    def test_admin_keys_endpoint(self):
        """POST /v1/admin/keys — 런타임 키 추가."""
        response = self.client.post(
            "/v1/admin/keys",
            json={"provider": "gemini", "keys": ["test-key-1"]},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "success"
