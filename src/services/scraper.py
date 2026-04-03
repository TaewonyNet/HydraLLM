import asyncio
import ipaddress
import logging
import socket
import urllib.parse
from typing import Literal, cast

from bs4 import BeautifulSoup  # type: ignore
from playwright.async_api import Browser, Playwright, async_playwright
from scrapling.fetchers import StealthyFetcher

logger = logging.getLogger(__name__)

ScrapeMode = Literal["standard", "simple", "network_only"]

# SSRF 차단 대상: private/reserved IP 대역
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _validate_url(url: str) -> str:
    """URL이 외부 호스트를 가리키는지 검증하고, 내부 네트워크 접근을 차단한다."""
    parsed = urllib.parse.urlparse(url)

    if parsed.scheme not in ("http", "https"):
        msg = f"Blocked: unsupported scheme '{parsed.scheme}'"
        raise ValueError(msg)

    hostname = parsed.hostname
    if not hostname:
        msg = "Blocked: empty hostname"
        raise ValueError(msg)

    # DNS 조회 → IP 검증 (리다이렉트 기반 SSRF 우회 방지)
    try:
        addr_infos = socket.getaddrinfo(hostname, parsed.port or 443)
    except socket.gaierror:
        msg = f"Blocked: cannot resolve hostname '{hostname}'"
        raise ValueError(msg) from None

    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                msg = f"Blocked: hostname '{hostname}' resolves to internal address"
                raise ValueError(msg)

    return url


class WebScraper:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def startup(self) -> None:
        """앱 시작 시 호출하여 Playwright 브라우저를 한 번만 실행."""
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless
            )
            logger.info("Playwright browser launched (reusable)")

    async def shutdown(self) -> None:
        """앱 종료 시 호출하여 Playwright 브라우저를 정리."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
            logger.info("Playwright browser closed")

    async def scrape_url(
        self, url: str, mode: ScrapeMode = "standard", timeout_ms: int = 30000
    ) -> str:
        try:
            _validate_url(url)
        except ValueError as e:
            logger.warning(f"SSRF blocked: {e}")
            return f"Blocked URL: {url}"

        logger.info(f"Scraping URL ({mode}) with Scrapling: {url}")

        try:
            fetcher = StealthyFetcher()
            response = await fetcher.async_fetch(
                url, timeout=timeout_ms, headless=self.headless
            )

            if not response or response.status not in [200, 201, 202]:
                logger.error(
                    f"Failed to fetch {url}, status: {response.status if response else 'No response'}"
                )
                return f"Failed to fetch {url}"

            title = response.css("title::text").get() or "No Title"

            if mode == "standard":
                text = response.get_all_text(separator="\n\n", strip=True)
            elif mode == "simple":
                text = response.get_all_text(separator=" ", strip=True)
            else:
                text = response.get_all_text()[:10000]

            final_content = f"TITLE: {title}\n\n{text}"
            logger.debug(f"Scraped {len(final_content)} characters from {url}")

            return final_content

        except Exception as e:
            logger.error(f"Scrapling failed for {url}: {e}")
            return await self._fallback_playwright_scrape(url, mode, timeout_ms)

    async def _fallback_playwright_scrape(
        self, url: str, mode: ScrapeMode, timeout_ms: int
    ) -> str:
        try:
            if not self._browser:
                await self.startup()

            assert self._browser is not None

            from browserforge.headers import HeaderGenerator

            headers_gen = HeaderGenerator()
            headers = headers_gen.generate()

            context = await self._browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=headers.get("User-Agent"),
                extra_http_headers={
                    k: v for k, v in headers.items() if k != "User-Agent"
                },
            )
            try:
                page = await context.new_page()
                await page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )

                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

                title = await page.title()
                content = await page.content()
                text = self._extract_clean_text(content, mode)
                return f"TITLE: {title}\n\n{text}"
            finally:
                await context.close()
        except Exception as e:
            return f"Error scraping {url}: {str(e)}"

    def _extract_clean_text(self, html: str, mode: ScrapeMode) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for element in soup(["script", "style", "iframe", "form", "button", "input"]):
            element.decompose()
        if mode == "standard":
            for element in soup(
                ["nav", "footer", "header", "aside", "noscript", "svg", "path"]
            ):
                element.decompose()
        text = soup.get_text(separator="\n")
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = "\n".join(chunk for chunk in chunks if chunk)
        return cast(str, text[:20000])

    async def search_and_scrape(
        self, query: str, num_results: int = 3, mode: ScrapeMode = "standard"
    ) -> str:
        logger.info(f"Performing search for: {query}")
        search_url = f"https://duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}"

        try:
            fetcher = StealthyFetcher()
            response = await fetcher.async_fetch(search_url, headless=self.headless)

            links = response.css("a.result__a::attr(href)").getall()
            top_links = []
            for link in links:
                abs_link = response.urljoin(link)
                if "duckduckgo.com/l/?" in abs_link:
                    parsed = urllib.parse.urlparse(abs_link)
                    params = urllib.parse.parse_qs(parsed.query)
                    if params.get("uddg"):
                        top_links.append(params["uddg"][0])
                    else:
                        top_links.append(abs_link)
                elif "duckduckgo.com" not in abs_link:
                    top_links.append(abs_link)

                if len(top_links) >= num_results:
                    break

            if not top_links:
                return "No search results found."

            # 검색 결과 URL도 SSRF 검증
            safe_links = []
            for search_link in top_links:
                try:
                    _validate_url(search_link)
                    safe_links.append(search_link)
                except ValueError as e:
                    logger.warning(f"SSRF blocked search result: {e}")

            if not safe_links:
                return "No safe search results found."

            tasks = [self.scrape_url(url, mode) for url in safe_links]
            results = await asyncio.gather(*tasks)

            combined_results = []
            for url, content in zip(safe_links, results, strict=False):
                combined_results.append(f"--- SOURCE: {url} ---\n{content}\n")

            return "\n".join(combined_results)
        except Exception as e:
            logger.error(f"Search failed with Scrapling: {e}")
            return f"Search failed: {str(e)}"
