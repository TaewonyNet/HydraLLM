"""웹 fetch / 강제 웹 fetch 흐름 검증 통합 테스트.

시나리오:
    1. 자동 웹 fetch: messages 안에 URL이 포함되면 scraper가 자동 호출되어
       컨텍스트가 주입되어야 한다.
    2. 강제 웹 fetch: `request.web_fetch="https://..."` 로 명시 지정하면,
       메시지에 URL이 없어도 해당 URL이 scrape 대상이 된다.
    3. 강제 웹 검색: `request.has_search=True` 이면 URL이 없어도 검색을 수행한다.
"""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.domain.models import ChatMessage, ChatRequest
from src.services.analyzer import ContextAnalyzer
from src.services.compressor import ContextCompressor
from src.services.session_manager import SessionManager
from src.services.web_context_service import WebContextService

pytestmark = pytest.mark.integration


@pytest.fixture()
def sm():
    db_path = os.path.join(
        tempfile.gettempdir(), f"test_web_{uuid4().hex[:8]}.sqlite"
    )
    manager = SessionManager(db_path=db_path)
    yield manager
    manager.close()
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.unlink(p)


def _build_service(sm: SessionManager) -> tuple[WebContextService, MagicMock]:
    analyzer = ContextAnalyzer()
    compressor = ContextCompressor()
    scraper = MagicMock()
    scraper.scrape_url = AsyncMock(
        return_value="# Example Page\n\nThis is scraped content about the Python language."
    )
    scraper.search_and_scrape = AsyncMock(
        return_value="Search result: Python is a popular programming language."
    )
    service = WebContextService(
        analyzer=analyzer,
        scraper=scraper,
        compressor=compressor,
        session_manager=sm,
    )
    return service, scraper


@pytest.mark.asyncio
async def test_auto_web_fetch_when_url_present(sm):
    """사용자 메시지에 URL이 있으면 자동으로 scrape_url이 호출된다."""
    service, scraper = _build_service(sm)

    request = ChatRequest(
        messages=[
            ChatMessage(
                role="user",
                content="이 링크 요약해줘 https://example.com/article",
            )
        ]
    )

    parts, context_text = await service.enrich_request(request)

    scraper.scrape_url.assert_awaited()
    called_url = scraper.scrape_url.call_args.args[0]
    assert called_url == "https://example.com/article"

    assert context_text is not None
    assert "Python" in context_text or "scraped content" in context_text
    assert any(p.get("type") == "web_fetch" for p in parts)


@pytest.mark.asyncio
async def test_forced_web_fetch_via_explicit_url(sm):
    """메시지에 URL이 없어도 request.web_fetch 지정 시 강제로 scrape 된다."""
    service, scraper = _build_service(sm)

    request = ChatRequest(
        messages=[ChatMessage(role="user", content="이 문서 어떤 내용인지 알려줘")],
        web_fetch="https://forced.example.com/doc",
    )

    parts, context_text = await service.enrich_request(request)

    scraper.scrape_url.assert_awaited()
    fetched_url = scraper.scrape_url.call_args.args[0]
    assert fetched_url == "https://forced.example.com/doc"
    assert context_text is not None
    assert len(context_text) > 0


@pytest.mark.asyncio
async def test_forced_web_search_via_has_search_flag(sm):
    """URL 없이도 has_search=True 면 search_and_scrape가 호출된다."""
    service, scraper = _build_service(sm)

    request = ChatRequest(
        messages=[ChatMessage(role="user", content="파이썬 최신 릴리즈 정보")],
        has_search=True,
    )

    parts, context_text = await service.enrich_request(request)

    scraper.search_and_scrape.assert_awaited()
    assert context_text is not None
    assert "Search result" in context_text or "Python" in context_text
    assert any(p.get("type") == "web_search" for p in parts)


@pytest.mark.asyncio
async def test_no_web_fetch_when_not_needed(sm):
    """일반 인사처럼 URL도 없고 has_search도 없으면 scraper 호출되지 않아야 한다."""
    service, scraper = _build_service(sm)

    request = ChatRequest(messages=[ChatMessage(role="user", content="안녕하세요")])

    parts, context_text = await service.enrich_request(request)

    scraper.scrape_url.assert_not_awaited()
    scraper.search_and_scrape.assert_not_awaited()
    assert context_text is None
    assert parts == []


@pytest.mark.asyncio
async def test_auto_web_fetch_disabled_skips_intent_classifier(sm):
    """auto_web_fetch=False 면 intent_classifier 기반 자동 검색이 억제된다.

    (키워드 스토어가 키워드를 감지하지 못하는 중립 쿼리를 사용하여
    intent_classifier 경로만 테스트)
    """
    service, scraper = _build_service(sm)

    # analyzer의 키워드 감지를 강제로 False 로 만들어
    # '자동 intent 경로'만 검증한다.
    service.analyzer.detect_web_intent = MagicMock(return_value=False)

    intent = MagicMock()
    intent.is_ready = True
    intent.needs_web_search = AsyncMock(return_value=True)
    service.intent_classifier = intent

    request = ChatRequest(
        messages=[ChatMessage(role="user", content="중립적인 일상 대화입니다")],
        auto_web_fetch=False,
    )

    parts, context_text = await service.enrich_request(request)

    intent.needs_web_search.assert_not_called()
    scraper.search_and_scrape.assert_not_awaited()
    assert context_text is None
