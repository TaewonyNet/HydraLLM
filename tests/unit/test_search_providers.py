"""검색 엔진 폴백 체인(SEARCH_PROVIDERS)과 openserp 응답 파싱 검증. (services/scraper.py)"""
import pytest

from src.services.scraper import (
    WebScraper,
    _extract_result_links,
    _parse_search_providers,
)

pytestmark = pytest.mark.unit


def test_parse_search_providers_normalizes_and_dedupes():
    assert _parse_search_providers(" OpenSERP, ddg ,bing,duck") == [
        "openserp",
        "duckduckgo",
        "bing",
    ]
    assert _parse_search_providers(["bing"]) == ["bing"]
    assert _parse_search_providers("") == ["duckduckgo", "bing"]


def test_extract_result_links_v2_skips_non_organic_and_duplicates():
    payload = {
        "results": [
            {"type": "ad", "url": "https://ad.example.com"},
            {"type": "organic", "url": "https://a.example.com"},
            {"type": "organic", "url": "https://a.example.com"},
            {"type": "organic", "url": "javascript:alert(1)"},
            {"type": "organic", "url": "https://b.example.com"},
            {"type": "organic", "url": "https://c.example.com"},
        ]
    }
    assert _extract_result_links(payload, 2) == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_extract_result_links_v1_list_skips_ads():
    # openserp v1(배열) 형식. SearXNG 는 v2 와 같은 {"results": [...]} 형식이다.
    payload = [
        {"rank": 1, "url": "https://ad.example.com", "ad": True},
        {"rank": 2, "url": "https://a.example.com", "ad": False},
    ]
    assert _extract_result_links(payload, 3) == ["https://a.example.com"]
    assert _extract_result_links({"error": "blocked"}, 3) == []


@pytest.mark.asyncio
async def test_search_falls_through_providers_in_order(monkeypatch):
    scraper = WebScraper(search_providers=["openserp", "bing"])
    calls: list[str] = []

    async def empty(query, n):
        calls.append("openserp")
        return []

    async def bing(query, n):
        calls.append("bing")
        return ["https://example.com/page"]

    async def fake_scrape(url, mode="standard"):
        return f"content of {url}"

    scraper._search_engines["openserp"] = empty
    scraper._search_engines["bing"] = bing
    monkeypatch.setattr(scraper, "scrape_url", fake_scrape)
    monkeypatch.setattr("src.services.scraper._validate_url", lambda u: None)

    assert await scraper.search_links("query") == ("bing", ["https://example.com/page"])
    calls.clear()
    result = await scraper.search_and_scrape("query")

    assert calls == ["openserp", "bing"]
    assert "--- SOURCE: https://example.com/page ---" in result

