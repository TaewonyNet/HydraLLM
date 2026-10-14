"""검색 MCP 클라이언트(Streamable HTTP)·McpWebScraper 폴백 검증. (adapters/web/mcp_search.py)

실제 MCP 서버 없이 httpx.MockTransport 로 서버 응답을 흉내 낸다.
"""
import json

import httpx
import pytest

from src.adapters.web.mcp_search import (
    McpError,
    McpWebScraper,
    StreamableHttpMcpClient,
)
from src.domain.interfaces import IWebScraper

pytestmark = pytest.mark.unit

URL = "http://mcp.test/mcp"


class FakeMcpServer:
    """initialize / notifications / tools/call 을 처리하는 가짜 MCP 서버."""

    def __init__(self, *, sse: bool = False, tool_error: bool = False) -> None:
        self.sse = sse
        self.tool_error = tool_error
        self.calls: list[str] = []
        self.session = "s1"
        self.expire_next_call = False
        self.expire_always = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        self.calls.append(method)
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            return self._reply(
                body["id"],
                {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {}},
                headers={"mcp-session-id": self.session},
            )
        if request.headers.get("mcp-session-id") != self.session:
            return httpx.Response(400, text="missing session")
        if self.expire_always:
            return httpx.Response(404, text="session expired")
        if self.expire_next_call:
            self.expire_next_call = False
            self.session = "s2"
            return httpx.Response(404, text="session expired")
        if method == "tools/call":
            args = body["params"]["arguments"]
            text = f"{body['params']['name']}:{json.dumps(args, ensure_ascii=False)}"
            return self._reply(
                body["id"],
                {"content": [{"type": "text", "text": text}], "isError": self.tool_error},
            )
        return self._reply(body["id"], {"tools": [{"name": "fetch"}]})

    def _reply(
        self, msg_id: int, result: dict, headers: dict | None = None
    ) -> httpx.Response:
        message = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        if self.sse:
            text = f"event: message\ndata: {json.dumps(message)}\n\n"
            return httpx.Response(
                200, text=text, headers={"content-type": "text/event-stream", **(headers or {})}
            )
        return httpx.Response(200, json=message, headers=headers)


def _client(server: FakeMcpServer) -> StreamableHttpMcpClient:
    return StreamableHttpMcpClient(URL, transport=httpx.MockTransport(server.handler))


@pytest.mark.asyncio
@pytest.mark.parametrize("sse", [False, True])
async def test_call_tool_json_and_sse(sse: bool):
    server = FakeMcpServer(sse=sse)
    client = _client(server)
    text = await client.call_tool("fetch", {"url": "https://example.com"})
    assert text == 'fetch:{"url": "https://example.com"}'
    assert server.calls[:2] == ["initialize", "notifications/initialized"]
    await client.aclose()


@pytest.mark.asyncio
async def test_session_expiry_reinitializes_once():
    server = FakeMcpServer()
    client = _client(server)
    await client.call_tool("fetch", {"url": "a"})
    server.expire_next_call = True
    assert (await client.call_tool("fetch", {"url": "b"})).startswith("fetch:")
    assert server.calls.count("initialize") == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_repeated_session_expiry_falls_back():
    """재초기화 후에도 계속 404 면 McpError 로 올라와 폴백 경로를 탄다(예외 누수 방지)."""
    server = FakeMcpServer()
    fallback = FakeFallback()
    scraper = McpWebScraper(_client(server), fallback=fallback)
    await scraper.search_and_scrape("warmup")
    server.expire_always = True
    assert await scraper.search_and_scrape("q") == "fallback:q"


@pytest.mark.asyncio
async def test_tool_error_raises():
    client = _client(FakeMcpServer(tool_error=True))
    with pytest.raises(McpError):
        await client.call_tool("fetch", {"url": "a"})
    await client.aclose()


class FakeFallback(IWebScraper):
    def __init__(self) -> None:
        self.started = False

    async def startup(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        pass

    async def scrape_url(self, url, mode="standard", timeout_ms=30000):
        return f"fallback:{url}"

    async def search_and_scrape(self, query, num_results=3, mode="standard"):
        return f"fallback:{query}"


def _down_handler(request: httpx.Request) -> httpx.Response:
    msg = "down"
    raise httpx.ConnectError(msg, request=request)


@pytest.mark.asyncio
async def test_mcp_scraper_uses_server_when_up():
    scraper = McpWebScraper(_client(FakeMcpServer()), fallback=FakeFallback())
    result = await scraper.search_and_scrape("q", num_results=2)
    assert result.startswith("search_and_fetch:")
    assert '"num_results": 2' in result


@pytest.mark.asyncio
async def test_mcp_scraper_falls_back_lazily_when_down():
    fallback = FakeFallback()
    client = StreamableHttpMcpClient(URL, transport=httpx.MockTransport(_down_handler))
    scraper = McpWebScraper(client, fallback=fallback)
    await scraper.startup()  # 서버 다운이어도 예외 없이 기동
    assert fallback.started is False  # 브라우저는 필요할 때까지 띄우지 않는다
    assert await scraper.scrape_url("https://x.test") == "fallback:https://x.test"
    assert await scraper.search_and_scrape("q") == "fallback:q"
    assert fallback.started is True


@pytest.mark.asyncio
async def test_mcp_scraper_without_fallback_returns_no_results():
    client = StreamableHttpMcpClient(URL, transport=httpx.MockTransport(_down_handler))
    scraper = McpWebScraper(client)
    assert await scraper.scrape_url("https://x.test") is None
    assert isinstance(await scraper.search_and_scrape("q"), str)
