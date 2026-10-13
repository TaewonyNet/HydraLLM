"""auto_web_fetch=False 는 어떤 웹 검색·수집도 하지 않아야 한다(실측 회귀 가드).

배경: POST /v1/chat/completions 에 "auto_web_fetch": false 를 줬는데도 본문 일부를
검색어로 DuckDuckGo/Bing 검색을 시도했다. 원인은 WebContextService.enrich_request 가
analyzer.detect_web_intent(URL/동적 키워드 탐지)를 do_auto_fetch 와 무관하게 호출해
web_required 를 True 로 만들고, has_search/명시 URL 여부와 섞어 검색을 트리거했기
때문이다. 이 테스트는 auto_web_fetch=False 면 analyzer.detect_web_intent 조차 호출되지
않고, scraper(url fetch/search)도 전혀 호출되지 않음을 검증한다.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import settings
from src.domain.models import ChatMessage, ChatRequest
from src.services.web_context_service import WebContextService

pytestmark = pytest.mark.unit


def _service() -> tuple[WebContextService, MagicMock, AsyncMock]:
    analyzer = MagicMock()
    analyzer.detect_web_intent = MagicMock(return_value=True)
    scraper = AsyncMock()
    compressor = MagicMock()
    session_manager = AsyncMock()
    intent_classifier = AsyncMock()
    intent_classifier.needs_web_search = AsyncMock(return_value=True)

    svc = WebContextService(
        analyzer=analyzer,
        scraper=scraper,
        compressor=compressor,
        session_manager=session_manager,
        intent_classifier=intent_classifier,
    )
    return svc, analyzer, scraper


@pytest.mark.asyncio
async def test_auto_web_fetch_false_skips_everything_even_with_url_and_search_flag():
    """명시적 URL + has_search=True + 동적 키워드가 다 있어도 auto_web_fetch=False 면
    analyzer.detect_web_intent 호출조차 안 되고, 스크레이핑/검색도 전혀 안 일어난다."""
    svc, analyzer, scraper = _service()

    request = ChatRequest(
        model="auto",
        messages=[ChatMessage(role="user", content="오늘 날씨 알려줘 https://example.com")],
        has_search=True,
        web_fetch="https://example.com",
        auto_web_fetch=False,
    )

    parts, text = await svc.enrich_request(request)

    assert parts == []
    assert text is None
    analyzer.detect_web_intent.assert_not_called()
    scraper.scrape_url.assert_not_called()
    scraper.search_and_scrape.assert_not_called()


@pytest.mark.asyncio
async def test_auto_web_fetch_false_overrides_server_default_enabled():
    """요청이 명시적으로 auto_web_fetch=False 를 주면, 서버 기본값(enable_auto_web_fetch=True)
    과 무관하게 꺼진다."""
    assert settings.enable_auto_web_fetch is True  # 서버 기본값은 여전히 켜짐(가정 확인)

    svc, analyzer, scraper = _service()
    request = ChatRequest(
        model="auto",
        messages=[ChatMessage(role="user", content="주가 알려줘")],
        auto_web_fetch=False,
    )

    parts, text = await svc.enrich_request(request)

    assert (parts, text) == ([], None)
    analyzer.detect_web_intent.assert_not_called()


@pytest.mark.asyncio
async def test_auto_web_fetch_true_still_triggers_search_path():
    """회귀 가드의 대조군: auto_web_fetch=True(또는 미지정) 면 기존처럼 웹 의도 탐지가
    동작해야 한다 — 이번 수정이 기능 자체를 죽이지 않았는지 확인."""
    svc, analyzer, scraper = _service()
    svc._process_search = AsyncMock(return_value=None)  # 검색 실행 경로만 확인

    request = ChatRequest(
        model="auto",
        messages=[ChatMessage(role="user", content="오늘 날씨 알려줘")],
        auto_web_fetch=True,
    )

    await svc.enrich_request(request)

    analyzer.detect_web_intent.assert_called_once()
    svc._process_search.assert_awaited_once()


@pytest.mark.asyncio
async def test_intent_checked_even_when_embeddings_not_ready():
    """임베딩(Ollama) 초기화 실패로 is_ready=False 여도 키워드·정규식 판정은 돌아야 한다.

    실측: is_ready 가드 때문에 "Rust 1.95 새 기능 검색해서 정리해줘" 가 검색 없이
    LLM 에 넘어가 지어낸 답이 나왔다.
    """
    svc, analyzer, scraper = _service()
    analyzer.detect_web_intent = MagicMock(return_value=False)
    svc.intent_classifier.is_ready = False
    scraper.search_and_scrape = AsyncMock(return_value="--- SOURCE: https://a.test ---\nbody")
    svc.session_manager.get_web_cache = AsyncMock(return_value=None)
    svc.compressor.compress = MagicMock(side_effect=lambda text, **_: text)

    request = ChatRequest(
        model="auto",
        messages=[ChatMessage(role="user", content="Rust 1.95 새 기능 검색해서 정리해줘")],
    )
    await svc.enrich_request(request)

    svc.intent_classifier.needs_web_search.assert_awaited_once()
    scraper.search_and_scrape.assert_awaited_once()
