"""검색 MCP 서버(search_mcp/server.py)를 웹 백엔드로 쓰는 어댑터.

게이트웨이 의존성(FastAPI 0.104 → anyio 3.x)과 공식 MCP SDK(anyio 4.x 필수)가 같은
venv 에 공존할 수 없어, 이미 의존성에 있는 httpx 로 MCP Streamable HTTP 전송의
클라이언트 측(initialize → tools/call, JSON·SSE 응답)만 최소 구현한다.

MCP 호출이 실패하면(서버 다운·타임아웃·도구 오류) 주입된 fallback(IWebScraper,
보통 프로세스 내 WebScraper)으로 같은 작업을 수행한다. fallback 의 브라우저는 처음
필요할 때만 띄운다(MCP 모드의 목적이 게이트웨이에서 브라우저를 빼는 것이므로).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from typing import Any

import httpx

from src.domain.interfaces.logic import IWebScraper
from src.i18n import t

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "hydrallm-gateway", "version": "1.3.0"}


class McpError(Exception):
    """MCP 전송·프로토콜·도구 오류."""


class StreamableHttpMcpClient:
    """MCP Streamable HTTP 클라이언트(도구 호출에 필요한 최소 기능)."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 75.0,
        token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        headers = {"Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            timeout=timeout, headers=headers, transport=transport
        )
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self._initialized = False
        self._session_id: str | None = None
        self._protocol_version = _PROTOCOL_VERSION

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_tools(self) -> list[str]:
        result = await self._request("tools/list", {})
        return [str(t.get("name")) for t in result.get("tools", []) if isinstance(t, dict)]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """도구를 호출해 text content 를 이어 붙여 반환. isError 면 McpError."""
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        text = "".join(
            str(c.get("text", ""))
            for c in result.get("content", [])
            if isinstance(c, dict) and c.get("type") == "text"
        )
        if result.get("isError"):
            msg = f"tool '{name}' failed: {text[:300]}"
            raise McpError(msg)
        return text

    # ------------------------------------------------------------------ 내부
    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_initialized()
        try:
            return await self._rpc(method, params)
        except _SessionExpiredError:
            # 서버 재시작 등으로 세션이 사라지면 한 번만 재초기화 후 재시도.
            self._initialized = False
            await self._ensure_initialized()
            return await self._rpc(method, params)

    def _is_initialized(self) -> bool:
        return self._initialized

    async def _ensure_initialized(self) -> None:
        if self._is_initialized():
            return
        async with self._lock:
            # 락 대기 중 다른 코루틴이 초기화를 끝냈을 수 있다.
            if self._is_initialized():
                return
            self._session_id = None
            result = await self._rpc(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
            )
            self._protocol_version = str(result.get("protocolVersion") or _PROTOCOL_VERSION)
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._initialized = True

    def _headers(self) -> dict[str, str]:
        headers = {"MCP-Protocol-Version": self._protocol_version}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            response = await self._http.post(self._url, json=payload, headers=self._headers())
        except httpx.HTTPError as e:
            msg = f"transport error: {e!r}"
            raise McpError(msg) from e
        if response.status_code == 404 and self._session_id:
            raise _SessionExpiredError()
        if response.status_code >= 400:
            msg = f"HTTP {response.status_code}: {response.text[:300]}"
            raise McpError(msg)
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        return response

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        msg_id = next(self._ids)
        response = await self._post(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        )
        message = _find_response(response, msg_id)
        if message is None:
            msg = f"no JSON-RPC response for {method} (id={msg_id})"
            raise McpError(msg)
        if "error" in message:
            msg = f"{method} error: {message['error']}"
            raise McpError(msg)
        result = message.get("result")
        return result if isinstance(result, dict) else {}


class _SessionExpiredError(McpError):
    """세션 만료(404). McpError 하위라 재시도에서도 만료되면 호출자의 폴백 경로로 간다."""


def _find_response(response: httpx.Response, msg_id: int) -> dict[str, Any] | None:
    """JSON 또는 SSE(text/event-stream) 본문에서 id 가 일치하는 JSON-RPC 응답을 찾는다."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        candidates: list[Any] = []
        data_lines: list[str] = []
        for line in response.text.splitlines() + [""]:
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                try:
                    candidates.append(json.loads("\n".join(data_lines)))
                except ValueError:
                    pass
                data_lines = []
    else:
        try:
            body = response.json()
        except ValueError:
            return None
        candidates = body if isinstance(body, list) else [body]
    for item in candidates:
        if isinstance(item, dict) and item.get("id") == msg_id:
            return item
    return None


class McpWebScraper(IWebScraper):
    """IWebScraper 를 검색 MCP 서버의 도구 호출로 구현한다."""

    def __init__(
        self, client: StreamableHttpMcpClient, fallback: IWebScraper | None = None
    ) -> None:
        self._client = client
        self._fallback = fallback
        self._fallback_started = False
        self._fallback_lock = asyncio.Lock()

    async def startup(self) -> None:
        # 비치명적: MCP 서버가 아직 안 떠 있어도 게이트웨이는 기동한다(호출 시 재시도/폴백).
        try:
            tools = await self._client.list_tools()
            logger.info(f"Search MCP connected: tools={tools}")
        except McpError as e:
            logger.warning(f"Search MCP not reachable at startup (will retry per call): {e}")

    async def shutdown(self) -> None:
        await self._client.aclose()
        if self._fallback is not None and self._fallback_started:
            await self._fallback.shutdown()

    async def scrape_url(
        self, url: str, mode: str = "standard", timeout_ms: int = 30000
    ) -> str | None:
        start = time.monotonic()
        try:
            text = await self._client.call_tool(
                "fetch", {"url": url, "mode": mode, "timeout_ms": timeout_ms}
            )
            logger.info(
                f"Search MCP fetch: {len(text)} chars in {time.monotonic() - start:.1f}s"
            )
            return text or None
        except McpError as e:
            fallback = await self._get_fallback("fetch", e)
            return await fallback.scrape_url(url, mode, timeout_ms) if fallback else None

    async def search_and_scrape(
        self, query: str, num_results: int = 3, mode: str = "standard"
    ) -> str:
        start = time.monotonic()
        try:
            text = await self._client.call_tool(
                "search_and_fetch",
                {"query": query, "num_results": num_results, "mode": mode},
            )
            logger.info(
                f"Search MCP search_and_fetch: {len(text)} chars in "
                f"{time.monotonic() - start:.1f}s"
            )
            return text
        except McpError as e:
            fallback = await self._get_fallback("search_and_fetch", e)
            if fallback is None:
                return str(t("web.no_search_results"))
            return await fallback.search_and_scrape(query, num_results, mode)

    async def _get_fallback(self, op: str, error: Exception) -> IWebScraper | None:
        if self._fallback is None:
            logger.error(f"Search MCP {op} failed, no fallback configured: {error}")
            return None
        logger.warning(f"Search MCP {op} failed → in-process fallback: {error}")
        if not self._fallback_started:
            async with self._fallback_lock:
                if not self._fallback_started:
                    try:
                        await self._fallback.startup()
                    except Exception as e:  # noqa: BLE001 — 브라우저 없이도 일부 모드는 동작
                        logger.error(f"Fallback scraper startup failed: {e}")
                    self._fallback_started = True
        return self._fallback
