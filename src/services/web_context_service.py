import asyncio
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
_URL_PATTERN = re.compile(r"https?://[^\s()<>]+")


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
        urls_to_fetch = _URL_PATTERN.findall(clean_query)

        if request.web_fetch and request.web_fetch not in urls_to_fetch:
            urls_to_fetch.append(request.web_fetch)

        if not (do_auto_fetch or web_required or urls_to_fetch):
            return []

        tasks = [self._process_url(url, clean_query) for url in urls_to_fetch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        context_blocks = []
        parts = []
        for res in results:
            if isinstance(res, tuple):
                block, part = res
                if block:
                    context_blocks.append(block)
                if part:
                    parts.append(part)

        if (web_required or request.has_search) and not context_blocks and clean_query:
            search_res = await self._process_search(clean_query)
            if search_res:
                block, part = search_res
                context_blocks.append(block)
                parts.append(part)

        if context_blocks:
            combined = "\n\n".join(context_blocks)
            max_chars = 6000
            if len(combined) > max_chars:
                combined = (
                    combined[:max_chars]
                    + "\n... [Context truncated to protect quota] ..."
                )

            request.messages.insert(
                -1,
                ChatMessage(
                    role="user",
                    content="[SYSTEM: REAL-TIME WEB DATA INJECTION]\n"
                    "The following is the latest information retrieved from the web. "
                    "Use this data to answer accurately. DO NOT respond with NO_REPLY if you found information. "
                    "ALWAYS mention the source names or URLs in your answer if relevant.\n\n"
                    + combined,
                    name="web_context_provider",
                ),
            )
        return parts

    async def _process_url(
        self, url: str, query: str
    ) -> tuple[str | None, dict[str, Any] | None]:
        start = time.time()
        try:
            cached = await self.session_manager.get_web_cache(
                url, ttl_hours=settings.web_cache_ttl_hours
            )
            if cached:
                await self.session_manager.record_scraping(
                    url,
                    "cache_hit",
                    len(cached),
                    0,
                    query=query,
                    summary=cached[:200],
                )
                return f"--- SOURCE: {url} ---\n{cached}\n--- END ---", {
                    "type": "web_fetch",
                    "data": {"url": url, "status": "cache_hit"},
                }

            raw = await self.scraper.scrape_url(
                url, mode=cast(ScrapeMode, settings.default_scrape_mode)
            )
            latency = int((time.time() - start) * 1000)
            if raw and not any(
                err in raw for err in ["Failed to fetch", "Error scraping"]
            ):
                optimized = self.compressor.compress(
                    raw, instruction=f"Extract info relevant to: {query}"
                )
                await self.session_manager.set_web_cache(
                    url, optimized, cast(ScrapeMode, settings.default_scrape_mode)
                )
                await self.session_manager.record_scraping(
                    url,
                    "success",
                    len(optimized),
                    latency,
                    query=query,
                    summary=optimized[:200],
                )
                return f"--- SOURCE: {url} ---\n{optimized}\n--- END ---", {
                    "type": "web_fetch",
                    "data": {"url": url, "status": "success"},
                }

            await self.session_manager.record_scraping(url, "failed", 0, latency)
            return None, {"type": "web_fetch", "data": {"url": url, "status": "failed"}}
        except Exception as e:
            latency = int((time.time() - start) * 1000)
            await self.session_manager.record_scraping(url, "error", 0, latency)
            logger.error(f"Error processing URL {url}: {e}")
            return None, {
                "type": "web_fetch",
                "data": {"url": url, "status": "error", "message": str(e)},
            }

    async def _process_search(
        self, query: str
    ) -> tuple[str | None, dict[str, Any] | None]:
        start = time.time()
        try:
            res = await self.scraper.search_and_scrape(
                query, mode=cast(ScrapeMode, settings.default_scrape_mode)
            )
            latency = int((time.time() - start) * 1000)
            if res and "No search results found" not in res:
                optimized = self.compressor.compress(res, instruction=query)
                await self.session_manager.record_scraping(
                    f"search:{query[:30]}",
                    "success",
                    len(optimized),
                    latency,
                    query=query,
                    summary=optimized[:200],
                )
                return f"--- WEB SEARCH RESULTS ---\n{optimized}\n--- END ---", {
                    "type": "web_search",
                    "data": {"query": query, "status": "success"},
                }
            return None, None
        except Exception:
            return None, None

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

        text = re.sub(
            r"Sender \(untrusted metadata\):[\s\S]*?```json[\s\S]*?```", "", text
        )
        text = re.sub(
            r"Conversation info \(untrusted metadata\):[\s\S]*?```json[\s\S]*?```",
            "",
            text,
        )
        text = re.sub(r"## Silent Replies[\s\S]*?✅ Right: NO_REPLY", "", text)

        if len(text) > 15000:
            text = text[-12000:]

        text = re.sub(r"\[\w{3} \d{4}-\d{2}-\d{2} [^\]]+\]", "", text)

        noise_patterns = [
            r"네이버\s*(밴드|블로그|카페|로그인|메일|뉴스|날씨|홈)",
            r"(카카오톡|페이스북|X|트위터|인스타그램)\s*공유하기",
            r"본문\s*바로가기",
        ]
        for pattern in noise_patterns:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)

        return text.strip()
