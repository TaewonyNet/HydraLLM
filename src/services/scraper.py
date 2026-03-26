import asyncio
import logging
from typing import Literal, cast

from bs4 import BeautifulSoup  # type: ignore
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

ScrapeMode = Literal["standard", "simple", "network_only"]


class WebScraper:
    """
    Service for scraping web content using Playwright with multiple strategies.
    Provides flexible methods for content extraction based on performance and quality needs.
    """

    def __init__(self, headless: bool = True):
        self.headless = headless

    async def scrape_url(
        self, url: str, mode: ScrapeMode = "standard", timeout_ms: int = 30000
    ) -> str:
        """
        Scrape a single URL using a strategy defined by the mode parameter.
        - standard: full page load + high-quality text extraction.
        - simple: quick basic text extraction.
        - network_only: high-speed extraction by blocking unnecessary resources.
        """
        logger.info(f"Scraping URL ({mode}): {url}")
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=self.headless)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    ignore_https_errors=True,
                )

                page = await context.new_page()

                if mode == "network_only":
                    await page.route(
                        "**/*.{png,jpg,jpeg,gif,svg,css,font,woff,woff2,js}",
                        lambda route: route.abort(),
                    )

                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

                title = await page.title()
                if mode == "standard":
                    await asyncio.sleep(2)

                content = await page.content()
                text = self._extract_clean_text(content, mode)

                final_content = f"TITLE: {title}\n\n{text}"
                logger.debug(f"Scraped {len(final_content)} characters from {url}")

                await browser.close()
                return final_content

            except Exception as e:
                logger.error(f"Failed to scrape {url}: {e}")
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

        text_parts = []
        for element in soup.find_all(
            ["h1", "h2", "h3", "p", "li", "article", "section"]
        ):
            part = element.get_text(strip=True)
            if part and len(part) > 20:
                tag_name = element.name.upper()
                text_parts.append(f"[{tag_name}] {part}")

        if not text_parts:
            text = soup.get_text(separator="\n")
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = "\n".join(chunk for chunk in chunks if chunk)
        else:
            text = "\n\n".join(text_parts)

        return cast(str, text[:20000])

    async def search_and_scrape(
        self, query: str, num_results: int = 3, mode: ScrapeMode = "standard"
    ) -> str:
        """
        Execute a web search and return scraped content from top results.
        Uses DuckDuckGo HTML version for reliable results without heavy JavaScript.
        """
        logger.info(f"Performing search for: {query}")
        search_url = f"https://duckduckgo.com/html/?q={query.replace(' ', '+')}"

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=self.headless)
                page = await browser.new_page()
                await page.goto(search_url, wait_until="domcontentloaded")

                links = await page.eval_on_selector_all(
                    "a.result__a", "elements => elements.map(el => el.href)"
                )

                top_links = [
                    link
                    for link in links
                    if not link.startswith("https://duckduckgo.com")
                ][:num_results]
                await browser.close()

                if not top_links:
                    return "No search results found."

                tasks = [self.scrape_url(url, mode) for url in top_links]
                results = await asyncio.gather(*tasks)

                combined_results: list[str] = []
                for url, content in zip(top_links, results, strict=False):
                    combined_results.append(f"--- SOURCE: {url} ---\n{content}\n")

                final_str = str("\n".join(combined_results))
                return final_str

            except Exception as e:
                logger.error(f"Search failed: {e}")
                return f"Search failed: {str(e)}"
