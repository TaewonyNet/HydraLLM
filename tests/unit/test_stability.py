import re

import pytest

from src.domain.models import ChatMessage, ChatRequest
from src.services.analyzer import ContextAnalyzer


def test_re_module_availability():
    """Verify that re module is available where needed."""
    assert re is not None
    # Test specific regex used in the system
    pattern = re.compile(r"https?://[^\s()<>]+")
    assert pattern.match("https://google.com")


def test_analyzer_detect_web_intent_exists():
    """Ensure detect_web_intent method is present and functional."""
    analyzer = ContextAnalyzer()
    assert hasattr(analyzer, "detect_web_intent")

    request = ChatRequest(
        messages=[ChatMessage(role="user", content="https://brunch.co.kr 요약해줘")]
    )
    assert analyzer.detect_web_intent(request) is True


def test_analyzer_detect_web_intent_keywords():
    """Ensure specific keywords trigger web intent."""
    analyzer = ContextAnalyzer()

    # Test '요약' keyword
    request = ChatRequest(
        messages=[
            ChatMessage(role="user", content="이 사이트 요약해줘 https://example.com")
        ]
    )
    assert analyzer.detect_web_intent(request) is True


@pytest.mark.asyncio
async def test_endpoints_import_stability():
    """Sanity check for endpoints logic without actual LLM call."""
    # This just checks if the module can be loaded and basic functions defined
    from src.api.v1 import endpoints

    assert endpoints.router is not None
