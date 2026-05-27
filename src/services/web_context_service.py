import asyncio
import logging
import re
import time
from typing import Any, cast

from src.core.config import settings
from src.domain.interfaces import ISessionManager
from src.domain.models import ChatRequest
from src.i18n import t
from src.services.analyzer import ContextAnalyzer
from src.services.compressor import ContextCompressor
from src.services.intent_classifier import IntentClassifier
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
        intent_classifier: IntentClassifier | None = None,
    ):
        self.analyzer = analyzer
        self.scraper = scraper
        self.compressor = compressor
        self.session_manager = session_manager
        self.intent_classifier = intent_classifier

    async def enrich_request(
        self, request: ChatRequest
    ) -> tuple[list[dict[str, Any]], str | None]:
        do_auto_fetch = (
            request.auto_web_fetch
            if request.auto_web_fetch is not None
            else settings.enable_auto_web_fetch
        )

        content_text = self._extract_user_content(request)
        clean_query = self._sanitize_query(content_text)

        has_url = self.analyzer.detect_web_intent(request)
        urls_to_fetch = _URL_PATTERN.findall(clean_query)

        if request.web_fetch and request.web_fetch not in urls_to_fetch:
            urls_to_fetch.append(request.web_fetch)

        web_required = has_url
        if (
            not web_required
            and do_auto_fetch
            and self.intent_classifier
            and self.intent_classifier.is_ready
        ):
            web_required = await self.intent_classifier.needs_web_search(clean_query)

        if not (web_required or urls_to_fetch):
            return [], None

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
                if block:
                    context_blocks.append(block)
                if part:
                    parts.append(part)

        if context_blocks:
            combined = "\n\n".join(context_blocks)
            max_chars = 6000
            if len(combined) > max_chars:
                combined = combined[:max_chars] + t("web.context_truncated")

            # Don't inject directly here anymore. Return as a part.
            # The Gateway will handle the final placement for maximum impact.
            return parts, combined
        return parts, None

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
                header = t("web.source_header", url=url)
                footer = t("web.source_footer")
                return f"{header}\n{cached}\n{footer}", {
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
                header = t("web.source_header", url=url)
                footer = t("web.source_footer")
                return f"{header}\n{optimized}\n{footer}", {
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
        cache_key = f"search:{self._normalize_search_key(query)}"
        try:
            cached = await self.session_manager.get_web_cache(
                cache_key, ttl_hours=settings.web_cache_ttl_hours
            )
            if cached:
                latency = int((time.time() - start) * 1000)
                await self.session_manager.record_scraping(
                    cache_key,
                    "cache_hit",
                    len(cached),
                    latency,
                    query=query,
                    summary=cached[:200],
                )
                header = t("web.search_results_header")
                footer = t("web.source_footer")
                logger.info(
                    f"Search cache hit for '{query[:60]}' ({len(cached)} chars)"
                )
                return f"{header}\n{cached}\n{footer}", {
                    "type": "web_search",
                    "data": {
                        "query": query,
                        "status": "cache_hit",
                        "cache_key": cache_key,
                    },
                }

            res = await self.scraper.search_and_scrape(
                query, mode=cast(ScrapeMode, settings.default_scrape_mode)
            )
            latency = int((time.time() - start) * 1000)
            no_results = t("web.no_search_results")
            if res and no_results not in res:
                optimized = self.compressor.compress(res, instruction=query)
                await self.session_manager.set_web_cache(
                    cache_key,
                    optimized,
                    "search",
                )
                await self.session_manager.record_scraping(
                    cache_key,
                    "success",
                    len(optimized),
                    latency,
                    query=query,
                    summary=optimized[:200],
                )
                header = t("web.search_results_header")
                footer = t("web.source_footer")
                return f"{header}\n{optimized}\n{footer}", {
                    "type": "web_search",
                    "data": {"query": query, "status": "success"},
                }
            return None, None
        except Exception:
            return None, None

    @staticmethod
    def _normalize_search_key(query: str) -> str:
        """동일 주제의 사소한 문구 변형이 캐시 키를 갈라놓지 않도록 정규화.

        - 소문자화, 양끝 구두점 제거
        - 공백 단일화
        - 한국어 조사/어미성 꼬리(알려줘/찾아줘/보여줘/지금/오늘/실시간/기준으로) 제거
        - 불용어 성격이 강한 접두사 제거 (거짓말 말고, 다시, 제발 등)
        """
        q = re.sub(r"\s+", " ", query.strip().lower())
        # 구두점 제거
        q = re.sub(r"[\.\?\!…,:;~\-–—\"'`]+", "", q)
        strip_suffixes = [
            "알려줘",
            "찾아줘",
            "보여줘",
            "검색해줘",
            "정리해줘",
            "요약해줘",
            "가르쳐줘",
        ]
        for s in strip_suffixes:
            if q.endswith(s):
                q = q[: -len(s)].rstrip()
        strip_prefixes = [
            "거짓말 말고 ",
            "거짓말 없이 ",
            "다시 ",
            "제발 ",
            "혹시 ",
            "please ",
        ]
        for p in strip_prefixes:
            if q.startswith(p):
                q = q[len(p) :]
        noise_terms = [
            "지금",
            "오늘",
            "최근",
            "기준으로",
            "실시간",
            "기준",
            "말고",
            "현재",
        ]
        # 조사성 접미어 단순 제거 (같은 토큰 여러 표기를 하나로 정규화).
        particle_suffix = re.compile(r"(에서|으로|부터|까지|에게|에|은|는|이|가|을|를|의)$")
        tokens: list[str] = []
        for tok in q.split():
            if tok in noise_terms:
                continue
            tokens.append(particle_suffix.sub("", tok))
        return " ".join(tokens)

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

        # Detect and neutralize potential prompt injection patterns in web data
        # We replace known trigger words with neutral variants
        malicious_patterns = [
            (r"ignore\s+(all\s+)?previous\s+instructions", "[FILTERED_INSTRUCTION]"),
            (r"you\s+must\s+output", "you are requested to show"),
            (r"system\s+override", "[FILTERED_OVERRIDE]"),
            (r"---[\s\S]*?---", "---"),  # Collapse nested separators
        ]
        for pattern, replacement in malicious_patterns:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

        return text.strip()
