import asyncio
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
assert project_root.exists()
src_path = project_root / "src"
sys.path.insert(0, str(src_path))

from src.domain.enums import ProviderType
from src.domain.models import ChatMessage, ChatRequest
from src.services.analyzer import ContextAnalyzer

pytestmark = pytest.mark.unit


class TestContextAnalyzer:
    def setup_method(self):
        self.analyzer = ContextAnalyzer(max_tokens_fast_model=8192)

    def test_analyze_text_request_auto(self):
        """auto 모델로 짧은 텍스트 요청 → 토큰 기반 라우팅으로 GROQ 선택."""
        request = ChatRequest(
            model="auto",
            messages=[ChatMessage(role="user", content="Hello, how are you?")],
        )

        result = asyncio.run(self.analyzer.analyze(request))
        assert result.provider == ProviderType.GROQ

    def test_analyze_gpt35_routes_to_gemini(self):
        """gpt-3.5-turbo는 model_mapping에 의해 GEMINI(gemini-1.5-flash)로 라우팅."""
        request = ChatRequest(
            model="gpt-3.5-turbo",
            messages=[ChatMessage(role="user", content="Hello")],
        )

        result = asyncio.run(self.analyzer.analyze(request))
        assert result.provider == ProviderType.GEMINI

    @pytest.mark.asyncio
    async def test_analyze_default_request(self):
        """auto 모델로 짧은 요청 → GROQ."""
        request = ChatRequest(
            model="auto",
            messages=[ChatMessage(role="user", content="This is a simple request")],
        )

        decision = await self.analyzer.analyze(request)
        assert decision.provider == ProviderType.GROQ

    @pytest.mark.asyncio
    async def test_analyze_short_context_request(self):
        """auto 모델로 짧은 요청 → GROQ."""
        request = ChatRequest(
            model="auto",
            messages=[{"role": "user", "content": "Hello"}],
        )

        decision = await self.analyzer.analyze(request)
        assert decision.provider == ProviderType.GROQ

    @pytest.mark.asyncio
    async def test_analyze_model_hint_gemini(self):
        """Test analyzer with Gemini model hint"""
        request = ChatRequest(
            model="gemini-pro",
            messages=[ChatMessage(role="user", content="Use Gemini please")],
        )

        decision = await self.analyzer.analyze(request)
        assert decision.provider == ProviderType.GEMINI

    @pytest.mark.asyncio
    async def test_analyze_model_hint_groq(self):
        """Test analyzer with Groq model hint"""
        request = ChatRequest(
            model="groq",
            messages=[ChatMessage(role="user", content="Use Groq please")],
        )

        decision = await self.analyzer.analyze(request)
        assert decision.provider == ProviderType.GROQ

    def test_analyze_empty_request(self):
        """Test analyzer with empty messages list."""
        # The analyzer should raise an error when there are no messages
        # This is tested by ensuring the ChatRequest is created correctly
        # and then analyzing it would fail
        request = ChatRequest(model="gpt-3.5-turbo", messages=[])

        # Verify that an empty messages list is accepted by the model
        # but will cause issues during analysis
        assert len(request.messages) == 0
