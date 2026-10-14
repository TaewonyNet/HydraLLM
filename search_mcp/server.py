"""HydraLLM 검색 MCP 서버.

게이트웨이의 웹 검색·본문 추출(src/services/scraper.py::WebScraper)을 독립 프로세스의
MCP 서버(Streamable HTTP)로 제공한다. 엔진은 .env 의 SEARCH_PROVIDERS 순서
(duckduckgo / bing / openserp / searxng)로 시도하며, SSRF 차단 검사도 WebScraper 그대로
적용된다. 게이트웨이는 WEB_BACKEND=mcp 일 때 이 서버를 호출한다.

공식 MCP SDK 는 anyio 4.x 가 필요해 게이트웨이(FastAPI 0.104, anyio 3.x)와 같은 venv 에
설치할 수 없다. 반드시 별도 venv 에서 실행한다(search_mcp/README.ko.md 참고).

실행:
    search_mcp/.venv/bin/python search_mcp/server.py [--host 127.0.0.1] [--port 8790]
"""

from __future__ import annotations

import argparse
import hmac
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
# 어디서 실행하든 게이트웨이와 같은 .env(상대 경로)를 읽도록 프로젝트 루트로 이동.
os.chdir(_PROJECT_ROOT)

import uvicorn  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Mount  # noqa: E402
from starlette.types import ASGIApp, Receive, Scope, Send  # noqa: E402

from src.core.config import settings  # noqa: E402
from src.i18n import set_locale  # noqa: E402
from src.services.scraper import WebScraper  # noqa: E402

logger = logging.getLogger("search_mcp")

_MAX_RESULTS = 10

# 게이트웨이와 같은 로케일이어야 "검색 결과 없음" 메시지를 게이트웨이가 결과로 오인하지 않는다.
set_locale(settings.locale)
scraper = WebScraper()

mcp = MCPServer(
    name="hydra-search",
    version="1.0.0",
    instructions=(
        "웹 검색과 페이지 본문 추출 도구. search_and_fetch 는 검색 후 상위 페이지 본문을 "
        "'--- SOURCE: <url> ---' 블록으로 돌려준다. 사설·내부 주소는 차단된다."
    ),
)


def _clamp(n: int) -> int:
    return max(1, min(int(n), _MAX_RESULTS))


@mcp.tool()
async def web_search(query: str, num_results: int = 5) -> dict[str, Any]:
    """검색 엔진 체인(SEARCH_PROVIDERS)으로 링크만 찾는다. 응답한 엔진과 URL 목록을 반환."""
    engine, links = await scraper.search_links(query, _clamp(num_results))
    return {"engine": engine, "links": links}


@mcp.tool()
async def fetch(url: str, mode: str = "standard", timeout_ms: int = 30000) -> str:
    """페이지 본문을 추출한다. 실패·차단 시 빈 문자열. mode: standard | simple | network_only."""
    # 외부 클라이언트가 브라우저를 오래 붙잡지 못하도록 상한을 둔다.
    return await scraper.scrape_url(url, mode, max(1000, min(int(timeout_ms), 60000))) or ""


@mcp.tool()
async def search_and_fetch(query: str, num_results: int = 3, mode: str = "standard") -> str:
    """검색 후 상위 페이지 본문을 '--- SOURCE: <url> ---' 블록으로 이어 붙여 반환."""
    return await scraper.search_and_scrape(query, _clamp(num_results), mode)


@mcp.tool()
async def list_engines() -> dict[str, Any]:
    """현재 엔진 체인과 사용 가능한 엔진 이름."""
    return {
        "chain": list(scraper.search_providers),
        "available": ["duckduckgo", "bing", "openserp", "searxng"],
    }


class _BearerAuth:
    """SEARCH_MCP_TOKEN 이 설정된 경우 Authorization: Bearer 토큰을 요구하는 ASGI 미들웨어."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(auth, self._expected):
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self._app(scope, receive, send)


def build_app(host: str) -> ASGIApp:
    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # 세션 상태를 두지 않으므로 서버 재시작에도 게이트웨이가 재초기화 없이 이어진다.
        stateless_http=True,
        json_response=True,
        host=host,
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # Playwright 브라우저는 요청마다가 아니라 서버 수명 동안 한 번만 띄운다.
        try:
            await scraper.startup()
        except Exception as e:  # noqa: BLE001 — 브라우저 없이도 standard 모드는 동작
            logger.error(f"Scraper startup failed: {e}")
        logger.info(f"Search engine chain: {scraper.search_providers}")
        async with inner.router.lifespan_context(inner):
            yield
        await scraper.shutdown()

    app: ASGIApp = Starlette(routes=[Mount("/", app=inner)], lifespan=lifespan)
    if settings.search_mcp_token:
        app = _BearerAuth(app, settings.search_mcp_token)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="HydraLLM 검색 MCP 서버")
    parser.add_argument("--host", default=settings.search_mcp_host)
    parser.add_argument("--port", type=int, default=settings.search_mcp_port)
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )
    if args.host not in ("127.0.0.1", "localhost", "::1") and not settings.search_mcp_token:
        logger.warning(
            "외부 인터페이스에 토큰 없이 바인드합니다. fetch 도구가 열린 프록시가 되지 않도록 "
            "SEARCH_MCP_TOKEN 설정을 권장합니다."
        )
    logger.info(f"Hydra search MCP: http://{args.host}:{args.port}/mcp")
    uvicorn.run(build_app(args.host), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
