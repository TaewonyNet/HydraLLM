import asyncio
import base64
import binascii
import ipaddress
import logging
import re
import socket
import urllib.parse
from collections.abc import Callable
from typing import Any, Literal

try:
    import curl_cffi.requests as _cffi_req

    if not hasattr(_cffi_req, "BrowserTypeLiteral"):
        _cffi_req.BrowserTypeLiteral = Any  # type: ignore
    if not hasattr(_cffi_req, "ProxySpec"):
        _cffi_req.ProxySpec = Any  # type: ignore
except ImportError:
    pass

from bs4 import BeautifulSoup
from playwright.async_api import Browser, Playwright, Route, async_playwright
from scrapling.fetchers import StealthyFetcher

try:
    # 본문→markdown 추출기(선택). 있으면 신호 밀도 높은 md 로 추출해 토큰 효율을 높이고,
    # 없으면 기존 BeautifulSoup 텍스트 추출로 폴백한다(무회귀).
    import trafilatura
except ImportError:
    trafilatura = None  # type: ignore[assignment]

from src.i18n import t, t_patterns

logger = logging.getLogger(__name__)

ScrapeMode = Literal["standard", "simple", "network_only"]

# SSRF 차단 대상: CGNAT 등 ipaddress 속성으로 안 잡히는 대역만 명시.
# 루프백/사설/링크로컬/예약/멀티캐스트/미지정은 _is_blocked_ip 의 속성 검사로 처리한다.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT (RFC 6598)
]


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    """내부/예약 대역 여부. IPv4-mapped IPv6(::ffff:x.x.x.x)는 IPv4 로 정규화해 검사."""
    # ::ffff:127.0.0.1 같은 매핑 주소가 IPv6 로 들어와 루프백 검사를 우회하는 것을 차단.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    return any(ip in network for network in _BLOCKED_NETWORKS)


def _unwrap_bing_redirect(url: str) -> str | None:
    """Bing 검색 결과 URL 에서 실제 목적지 URL 을 추출.

    Bing 은 `a.href` 를 `https://www.bing.com/ck/a?!&&p=...&u=a1<base64(url)>...` 형식의
    클릭 추적 URL 로 감싸서 내려준다. 외부 URL 이 바로 들어오면 그대로 반환하고,
    bing.com 내부 URL 이면 `u=a1` 파라미터를 base64 urlsafe 디코드해 실제 URL 을 복원한다.
    디코드 불가면 None 반환.
    """
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    if "bing.com" not in (parsed.netloc or "").lower():
        return url
    if "/ck/a" not in (parsed.path or "").lower():
        return None
    try:
        params = urllib.parse.parse_qs(parsed.query)
    except Exception:
        return None
    u_values = params.get("u") or []
    if not u_values:
        return None
    u = u_values[0]
    if u.startswith("a1"):
        u = u[2:]
    padded = u + "=" * (-len(u) % 4)
    decoders: tuple[Callable[[str], bytes], ...] = (
        base64.urlsafe_b64decode,
        base64.b64decode,
    )
    for decoder in decoders:
        try:
            decoded: str = decoder(padded).decode("utf-8", errors="ignore").strip()
        except (binascii.Error, ValueError):
            continue
        if decoded.startswith(("http://", "https://")):
            return decoded
    return None


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
        if _is_blocked_ip(ip):
            msg = f"Blocked: hostname '{hostname}' resolves to internal address"
            raise ValueError(msg)

    return url


async def _url_resolves_internal(url: str) -> bool:
    """url 의 호스트가 내부/예약 IP 로 resolve 되는지 검사(예외 없이 bool 반환).

    리다이렉트 후 최종 URL 재검증 및 Playwright route 가드에서 쓰는 best-effort 헬퍼.
    스킴 비정상·호스트 없음·resolve 실패는 안전하게 '차단'으로 본다. DNS 조회는
    이벤트 루프를 막지 않도록 executor 에서 수행한다. 단일 resolve 내 rebinding 은
    구조상 여전히 잔여 위험이다(SPEC §10).
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return True
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return True
    loop = asyncio.get_event_loop()
    try:
        addr_infos = await loop.run_in_executor(
            None, socket.getaddrinfo, parsed.hostname, parsed.port or 443
        )
    except socket.gaierror:
        return True
    return any(
        _is_blocked_ip(ipaddress.ip_address(info[4][0])) for info in addr_infos
    )


class WebScraper:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        # 동시 폴백이 shutdown/startup 을 인터리브하지 않도록 self-heal 직렬화(R5).
        self._restart_lock = asyncio.Lock()

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
    ) -> str | None:
        # 실패/차단 시 None 을 반환한다(로컬라이즈된 에러 문자열을 content 로 오인해
        # 주입·캐시하던 버그 방지, R3). 성공 시에만 "TITLE: ..." 문자열을 반환.
        try:
            _validate_url(url)
        except ValueError as e:
            logger.warning(f"SSRF blocked: {e}")
            return None

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
                return None

            # 리다이렉트 재검증: 최초 URL 은 통과했어도 30x 로 내부 주소에 도달했을 수
            # 있으므로 최종 URL(response.url)을 다시 검사한다(best-effort, SPEC §10).
            final_url = getattr(response, "url", "") or url
            if str(final_url) != url and await _url_resolves_internal(str(final_url)):
                logger.warning(f"SSRF blocked after redirect: {final_url}")
                return None

            title = response.css("title::text").get() or "No Title"

            if mode == "standard":
                raw_html = (
                    str(response.html_content)
                    if hasattr(response, "html_content")
                    else ""
                )
                if raw_html:
                    text = self._extract_clean_text(raw_html, mode)
                else:
                    text = response.get_all_text(separator="\n\n", strip=True)
            elif mode == "simple":
                text = response.get_all_text(separator=" ", strip=True)
            else:
                text = response.get_all_text()[:10000]

            text = self._strip_boilerplate(text)
            final_content = f"TITLE: {title}\n\n{text}"
            logger.debug(f"Scraped {len(final_content)} characters from {url}")

            return final_content

        except Exception as e:
            logger.error(f"Scrapling failed for {url}: {e}")
            return await self._fallback_playwright_scrape(url, mode, timeout_ms)

    async def _fallback_playwright_scrape(
        self, url: str, mode: ScrapeMode, timeout_ms: int
    ) -> str | None:
        context = None
        try:
            if not self._browser or not self._browser.is_connected():
                # Self-healing: 동시 폴백이 중복 재시작하지 않도록 락 + 이중 확인(R5).
                async with self._restart_lock:
                    if not self._browser or not self._browser.is_connected():
                        await self.shutdown()
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

            # SSRF route 가드: 문서(네비게이션/리다이렉트) 요청의 목적지가 내부 주소면
            # 네트워크 레벨에서 abort 한다. 서브리소스는 그대로 통과(content 추출 대상이
            # 아니므로 SSRF 핵심 경로는 document 요청). 가드 오류 시 fail-safe 로 continue.
            async def _ssrf_route_guard(route: Route) -> None:
                req = route.request
                try:
                    if req.resource_type == "document" and await _url_resolves_internal(
                        req.url
                    ):
                        logger.warning(f"SSRF blocked (navigation): {req.url}")
                        await route.abort()
                        return
                    await route.continue_()
                except Exception:
                    try:
                        await route.continue_()
                    except Exception:
                        pass

            await context.route("**/*", _ssrf_route_guard)

            page = await context.new_page()
            try:
                await page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )

                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

                # 리다이렉트 후 최종 URL 재검증(route 가드를 빠져나간 경우의 방어선).
                if await _url_resolves_internal(page.url):
                    logger.warning(f"SSRF blocked after redirect: {page.url}")
                    return None

                title = await page.title()
                content = await page.content()
                text = self._extract_clean_text(content, mode)
                return f"TITLE: {title}\n\n{text}"
            finally:
                await page.close()
        except Exception as e:
            logger.error(f"Playwright fallback failed for {url}: {e}")
            return None
        finally:
            if context:
                await context.close()

    def _extract_clean_text(self, html: str, mode: ScrapeMode) -> str:
        soup = BeautifulSoup(html, "html.parser")

        # Extract potential publication date
        pub_date = self._extract_publish_date(soup)

        # 0. trafilatura 가 있으면 본문을 markdown 으로 추출(잡음 제거·신호 밀도↑ →
        #    같은 토큰 예산에 더 많은 정보 → fast tier 도 충분히 답변). 실패 시 아래 BS4 폴백.
        if trafilatura is not None:
            md = trafilatura.extract(
                html,
                output_format="markdown",
                include_comments=False,
                include_tables=True,
            )
            if md:
                md = md.strip()
                if pub_date:
                    md = f"[PUBLISHED_DATE: {pub_date}]\n{md}"
                return md[:25000]

        # 1. Remove obvious non-content elements
        for element in soup(
            [
                "script",
                "style",
                "iframe",
                "form",
                "button",
                "input",
                "nav",
                "footer",
                "header",
                "aside",
                "noscript",
                "svg",
                "path",
                "canvas",
                "link",
                "meta",
            ]
        ):
            element.decompose()

        # 2. Target specific noise-heavy containers by common class/id patterns
        noise_selectors = [
            "div[class*='sidebar']",
            "div[class*='ad-']",
            "div[class*='advertisement']",
            "div[class*='banner']",
            "div[id*='sidebar']",
            "aside",
            ".footer",
            ".nav",
            ".menu",
            ".widget",
        ]
        for selector in noise_selectors:
            for element in soup.select(selector):
                element.decompose()

        # 3. Find main content
        main_content = (
            soup.find("article")
            or soup.find("main")
            or soup.find(id=re.compile(r"content|post|article|main", re.I))
            or soup.find(class_=re.compile(r"content|post|article|main", re.I))
        )

        if main_content:
            text = main_content.get_text(separator="\n")
        else:
            text = soup.get_text(separator="\n")

        # 4. Final cleaning
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = "\n".join(chunk for chunk in chunks if chunk)

        # Prefix text with publication date if found
        if pub_date:
            text = f"[PUBLISHED_DATE: {pub_date}]\n{text}"

        # Limit length to avoid overwhelming context while preserving most relevant info
        return text[:25000]

    def _extract_publish_date(self, soup: BeautifulSoup) -> str | None:
        """Extract publication date from meta tags or common patterns."""
        # 1. Check meta tags (OpenGraph, Schema.org, etc.)
        meta_selectors = [
            {"property": "article:published_time"},
            {"name": "pubdate"},
            {"name": "publish-date"},
            {"property": "og:published_time"},
            {"itemprop": "datePublished"},
        ]
        for selector in meta_selectors:
            meta = soup.find("meta", attrs=dict(selector))
            if meta and meta.get("content"):
                date_str = str(meta.get("content"))
                return date_str[:10]  # Return YYYY-MM-DD

        # 2. Heuristic: Look for common date patterns in text (e.g. 2026-04-17)
        date_pattern = re.compile(r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})")
        match = date_pattern.search(soup.get_text())
        if match:
            return match.group(1)

        return None

    @staticmethod
    def _strip_boilerplate(text: str) -> str:
        # Load localized noise patterns
        noise_patterns = t_patterns("boilerplate_patterns")
        for pattern in noise_patterns:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)

        # Additional heuristic: remove lines that look like ads or very short nav items
        lines = []
        for ln in text.splitlines():
            clean_ln = ln.strip()
            # Skip empty or tiny lines
            if len(clean_ln) < 4:
                continue
            # Skip lines that are likely navigation or meta info (heuristic)
            if any(
                marker in clean_ln
                for marker in [
                    "로그인",
                    "회원가입",
                    "비밀번호",
                    "개인정보처리방침",
                    "Copyright",
                    "All rights reserved",
                ]
            ):
                if len(clean_ln) < 100:  # Only skip if it's a short meta line
                    continue
            lines.append(clean_ln)

        return "\n".join(lines)

    async def search_and_scrape(
        self, query: str, num_results: int = 3, mode: ScrapeMode = "standard"
    ) -> str:
        logger.info(f"Performing search for: {query}")

        top_links = await self._search_links_duckduckgo(query, num_results)
        if not top_links:
            logger.warning(
                f"DuckDuckGo returned 0 links for '{query}', trying Bing fallback"
            )
            top_links = await self._search_links_bing(query, num_results)

        if not top_links:
            logger.warning(f"All search engines returned 0 links for '{query}'")
            return str(t("web.no_search_results"))

        # 검색 결과 URL도 SSRF 검증
        safe_links = []
        for search_link in top_links:
            try:
                _validate_url(search_link)
                safe_links.append(search_link)
            except ValueError as e:
                logger.warning(f"SSRF blocked search result: {e}")

        if not safe_links:
            return str(t("web.no_safe_results"))

        logger.info(f"Search yielded {len(safe_links)} links, scraping top results")
        tasks = [self.scrape_url(url, mode) for url in safe_links]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        combined_results = []
        for url, content in zip(safe_links, results, strict=False):
            if isinstance(content, BaseException):
                logger.warning(f"scrape_url error for {url}: {content}")
                continue
            if not content:  # 실패/차단(None) 결과는 건너뛴다(R3)
                continue
            combined_results.append(f"--- SOURCE: {url} ---\n{content}\n")

        if not combined_results:
            return str(t("web.no_search_results"))

        return "\n".join(combined_results)

    async def _search_links_duckduckgo(
        self, query: str, num_results: int
    ) -> list[str]:
        search_url = (
            f"https://duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}"
        )
        try:
            fetcher = StealthyFetcher()
            response = await fetcher.async_fetch(search_url, headless=self.headless)
        except Exception as e:
            logger.error(f"DuckDuckGo fetch failed: {e}")
            return []

        # DDG HTML 은 주기적으로 selector 가 바뀐다. 여러 셀렉터를 순회하며 시도.
        candidate_selectors = [
            "a.result__a::attr(href)",
            "a.result__url::attr(href)",
            "div.result h2 a::attr(href)",
            "div.results_links a::attr(href)",
            "a[data-testid='result-title-a']::attr(href)",
        ]
        raw_links: list[str] = []
        for sel in candidate_selectors:
            hits: list[str] = []
            try:
                hits = list(response.css(sel).getall())
            except Exception:
                hits = []
            if hits:
                raw_links = hits
                logger.debug(f"DDG selector matched ({sel}): {len(hits)} links")
                break

        if not raw_links:
            logger.warning(
                "DuckDuckGo parser matched no links — HTML shape may have changed"
            )
            return []

        top_links: list[str] = []
        for link in raw_links:
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
        return top_links

    async def _search_links_bing(self, query: str, num_results: int) -> list[str]:
        search_url = (
            f"https://www.bing.com/search?q={urllib.parse.quote_plus(query)}"
        )
        try:
            fetcher = StealthyFetcher()
            response = await fetcher.async_fetch(search_url, headless=self.headless)
        except Exception as e:
            logger.error(f"Bing fetch failed: {e}")
            return []

        candidate_selectors = [
            "li.b_algo h2 a::attr(href)",
            "li.b_algo a::attr(href)",
            "main li h2 a::attr(href)",
        ]
        raw_links: list[str] = []
        for sel in candidate_selectors:
            hits: list[str] = []
            try:
                hits = list(response.css(sel).getall())
            except Exception:
                hits = []
            if hits:
                raw_links = hits
                logger.debug(f"Bing selector matched ({sel}): {len(hits)} links")
                break

        if not raw_links:
            return []

        top_links: list[str] = []
        dropped_internal = 0
        dropped_decode = 0
        for link in raw_links:
            abs_link = response.urljoin(link)
            unwrapped = _unwrap_bing_redirect(abs_link)
            if unwrapped is None:
                # bing 내부 페이지인데 decode 실패
                if "bing.com" in abs_link:
                    dropped_decode += 1
                else:
                    dropped_internal += 1
                continue
            if "bing.com" in urllib.parse.urlparse(unwrapped).netloc:
                dropped_internal += 1
                continue
            top_links.append(unwrapped)
            if len(top_links) >= num_results:
                break
        if not top_links and raw_links:
            logger.warning(
                f"Bing: {len(raw_links)} links extracted but all dropped "
                f"(internal={dropped_internal}, decode_failed={dropped_decode})"
            )
        return top_links
