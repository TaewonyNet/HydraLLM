import logging
import re
import time
from typing import Any, cast

from src.core.config import settings
from src.domain.interfaces import ISessionManager
from src.domain.models import ChatMessage, ChatRequest
from src.services.analyzer import ContextAnalyzer
from src.services.compressor import ContextCompressor
from src.services.scraper import ScrapeMode, WebScraper

logger = logging.getLogger(__name__)
_URL_PATTERN = re.compile(r"https?://[^\s/$.?#].[^\sㄱ-ㅎㅏ-ㅣ가-힣]*")


class WebContextService:
    def __init__(
        self,
        analyzer: ContextAnalyzer,
        scraper: WebScraper,
        compressor: ContextCompressor,
        session_manager: ISessionManager,
    ):
        self.analyzer = analyzer
        self.scraper = scraper
        self.compressor = compressor
        self.session_manager = session_manager

    async def enrich_request(self, request: ChatRequest) -> list[dict[str, Any]]:
        do_auto_fetch = (
            request.auto_web_fetch
            if request.auto_web_fetch is not None
            else settings.enable_auto_web_fetch
        )

        content_text = self._extract_user_content(request)
        clean_query = self._sanitize_query(content_text)

        web_required = self.analyzer.detect_web_intent(request)
        if not (do_auto_fetch or web_required):
            return []

        parts: list[dict[str, Any]] = []
        urls_to_fetch = _URL_PATTERN.findall(clean_query)
        if request.web_fetch and request.web_fetch not in urls_to_fetch:
            urls_to_fetch.append(request.web_fetch)

        scrape_mode = cast(ScrapeMode, settings.default_scrape_mode)
        context_blocks: list[str] = []

        for url in urls_to_fetch:
            start_fetch = time.time()
            try:
                # 1. 캐시 확인
                cached_content = await self.session_manager.get_web_cache(
                    url, ttl_hours=settings.web_cache_ttl_hours
                )
                if cached_content:
                    logger.info(f"✨ Using cached content for {url}")
                    context_blocks.append(
                        f"--- SOURCE: {url} ---\n{cached_content}\n--- END ---"
                    )
                    parts.append(
                        {
                            "type": "web_fetch",
                            "data": {"url": url, "status": "cache_hit"},
                        }
                    )
                    await self.session_manager.record_scraping(
                        url, "cache_hit", len(cached_content), 0
                    )
                    continue

                # 2. 실제 페칭
                raw_content = await self.scraper.scrape_url(url, mode=scrape_mode)
                latency = int((time.time() - start_fetch) * 1000)
                success = raw_content and not any(
                    err in raw_content for err in ["Failed to fetch", "Error scraping"]
                )

                if success:
                    optimized = self.compressor.compress(
                        raw_content,
                        instruction=f"Extract info relevant to: {clean_query}",
                    )
                    await self.session_manager.set_web_cache(
                        url, optimized, scrape_mode
                    )
                    context_blocks.append(
                        f"--- SOURCE: {url} ---\n{optimized}\n--- END ---"
                    )
                    parts.append(
                        {"type": "web_fetch", "data": {"url": url, "status": "success"}}
                    )
                    await self.session_manager.record_scraping(
                        url, "success", len(optimized), latency
                    )
                else:
                    context_blocks.append(
                        f"--- SOURCE: {url} ---\n[STATUS: FETCH_FAILED]\n--- END ---"
                    )
                    parts.append(
                        {"type": "web_fetch", "data": {"url": url, "status": "failed"}}
                    )
                    await self.session_manager.record_scraping(
                        url, "failed", 0, latency
                    )
            except Exception as e:
                latency = int((time.time() - start_fetch) * 1000)
                logger.error(f"Error fetching {url}: {e}")
                parts.append(
                    {
                        "type": "web_fetch",
                        "data": {"url": url, "status": "error", "message": str(e)},
                    }
                )
                await self.session_manager.record_scraping(url, "failed", 0, latency)

        # 3. 검색 처리
        if (web_required or request.has_search) and not context_blocks and clean_query:
            start_search = time.time()
            try:
                search_results = await self.scraper.search_and_scrape(
                    clean_query, mode=scrape_mode
                )
                latency = int((time.time() - start_search) * 1000)
                if search_results and "No search results found" not in search_results:
                    optimized = self.compressor.compress(
                        search_results, instruction=clean_query
                    )
                    context_blocks.append(
                        f"--- WEB SEARCH RESULTS ---\n{optimized}\n--- END ---"
                    )
                    parts.append(
                        {
                            "type": "web_search",
                            "data": {"query": clean_query, "status": "success"},
                        }
                    )
                    await self.session_manager.record_scraping(
                        f"search:{clean_query[:30]}", "success", len(optimized), latency
                    )
                else:
                    context_blocks.append(
                        "--- WEB SEARCH RESULTS ---\n[STATUS: NO_RESULTS_FOUND]\n--- END ---"
                    )
                    parts.append(
                        {
                            "type": "web_search",
                            "data": {"query": clean_query, "status": "no_results"},
                        }
                    )
                    await self.session_manager.record_scraping(
                        f"search:{clean_query[:30]}", "failed", 0, latency
                    )
            except Exception as e:
                latency = int((time.time() - start_search) * 1000)
                logger.error(f"Search failed: {e}")
                parts.append(
                    {
                        "type": "web_search",
                        "data": {"query": clean_query, "status": "error"},
                    }
                )
                await self.session_manager.record_scraping(
                    f"search:{clean_query[:30]}", "failed", 0, latency
                )

        if context_blocks:
            combined = "\n\n".join(context_blocks)
            request.messages.insert(
                -1,
                ChatMessage(
                    role="system",
                    content="[IMPORTANT: REAL-TIME WEB DATA]\n"
                    "Use the following optimized real-time data to answer accurately.\n\n"
                    + combined,
                    name="web_context_provider",
                ),
            )

        return parts

    def _extract_user_content(self, request: ChatRequest) -> str:
        if not request.messages:
            return ""
        last_msg = request.messages[-1]
        if isinstance(last_msg.content, str):
            return last_msg.content
        if isinstance(last_msg.content, list):
            return "".join(
                [
                    p.get("text", "")
                    for p in last_msg.content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
            )
        return ""

    def _sanitize_query(self, text: str) -> str:
        if not text:
            return ""
        if "Sender (untrusted metadata):" in text:
            parts = text.split("```", 2)
            if len(parts) >= 3:
                text = parts[2].strip()
        return re.sub(r"\[\w{3} \d{4}-\d{2}-\d{2} [^\]]+\]", "", text).strip()
