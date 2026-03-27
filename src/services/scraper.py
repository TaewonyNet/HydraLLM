import asyncio
import logging
import urllib.parse
from typing import Literal, cast

from bs4 import BeautifulSoup  # type: ignore
from playwright.async_api import Browser, Playwright, async_playwright
from scrapling.fetchers import StealthyFetcher

logger = logging.getLogger(__name__)

ScrapeMode = Literal["standard", "simple", "network_only"]


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
        search_url = f"https://duckduckgo.com/html/?q={query.replace(' ', '+')}"

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

            tasks = [self.scrape_url(url, mode) for url in top_links]
            results = await asyncio.gather(*tasks)

            combined_results = []
            for url, content in zip(top_links, results, strict=False):
                combined_results.append(f"--- SOURCE: {url} ---\n{content}\n")

            return "\n".join(combined_results)
        except Exception as e:
            logger.error(f"Search failed with Scrapling: {e}")
            return f"Search failed: {str(e)}"
