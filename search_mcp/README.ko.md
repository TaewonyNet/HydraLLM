# Hydra 검색 MCP 서버

HydraLLM 게이트웨이의 웹 검색·본문 추출(`src/services/scraper.py::WebScraper`)을 독립 프로세스의 **MCP 서버(Streamable HTTP)** 로 제공합니다. 게이트웨이는 `WEB_BACKEND=mcp`일 때 이 서버를 호출하고, 다른 MCP 클라이언트(Claude Code 등)도 같은 서버를 붙여 쓸 수 있습니다.

```
HydraLLM ──(MCP)──▶ Hydra 검색 MCP 서버 ──▶ duckduckgo / bing / openserp / searxng
```

- LLM 호출 횟수는 늘지 않습니다. 게이트웨이가 검색 여부를 판정(키워드·정규식·임베딩)한 뒤 MCP 도구를 정해진 순서로 직접 호출합니다.
- 브라우저(Playwright/Scrapling)가 게이트웨이 밖에서 돌기 때문에, 검색 쪽을 고치거나 재시작해도 게이트웨이는 그대로입니다.
- SSRF 차단(사설·내부 주소 거부)은 `WebScraper`의 검사를 그대로 사용합니다.

## 1. 설치 (별도 venv 필수)

공식 MCP SDK는 `anyio>=4`가 필요하고 게이트웨이(FastAPI 0.104)는 `anyio<4`를 요구해 **같은 venv에 설치할 수 없습니다.** 프로젝트 루트에서:

```bash
python3 -m venv search_mcp/.venv
search_mcp/.venv/bin/pip install -r search_mcp/requirements.txt
search_mcp/.venv/bin/python -m playwright install chromium   # 게이트웨이에서 이미 설치했다면 생략 가능
```

게이트웨이 쪽은 추가 설치가 없습니다(기존 `httpx`로 MCP 클라이언트를 구현).

## 2. 실행

```bash
search_mcp/.venv/bin/python search_mcp/server.py            # 기본 http://127.0.0.1:8790/mcp
search_mcp/.venv/bin/python search_mcp/server.py --port 8790 --host 127.0.0.1
```

- 서버는 프로젝트 루트의 `.env`를 읽습니다. 엔진 체인 `SEARCH_PROVIDERS`, `OPENSERP_*`, `SEARXNG_*`, `LOCALE` 등이 그대로 적용됩니다.
- 로그는 stdout으로 나갑니다(게이트웨이의 `gateway.log`와 섞지 않음).
- 정상 기동 로그: `Search engine chain: [...]`, `StreamableHTTP session manager started`

## 3. 게이트웨이 연결 (`.env`)

```env
WEB_BACKEND=mcp                               # 기본 inprocess (기존 동작)
SEARCH_MCP_URL=http://127.0.0.1:8790/mcp
SEARCH_MCP_TIMEOUT=75                         # 검색+본문 추출 최대 대기(초)
SEARCH_MCP_FALLBACK=true                      # MCP 실패 시 게이트웨이 내장 WebScraper 로 대신 수행
SEARCH_MCP_TOKEN=                             # 설정 시 서버·게이트웨이 모두 Bearer 토큰 사용
```

게이트웨이를 재시작하면 로그에 `Web backend: search MCP server (...)`, `Search MCP connected: tools=[...]`가 찍힙니다. 요청마다 `Search MCP search_and_fetch: N chars in Xs`가 남습니다.

- MCP 서버가 꺼져 있어도 게이트웨이는 정상 기동합니다. 호출 시 실패하면 `SEARCH_MCP_FALLBACK=true`일 때 내장 스크래퍼로 대신 처리합니다. 내장 브라우저는 처음 폴백할 때만 띄웁니다.
- 서버는 상태를 두지 않는(stateless) 모드라 서버만 재시작해도 게이트웨이 재시작은 필요 없습니다.

## 4. 도구

| 도구 | 인자 | 반환 |
|---|---|---|
| `web_search` | `query`, `num_results`(기본 5, 최대 10) | `{"engine": 응답한 엔진, "links": [URL…]}` |
| `fetch` | `url`, `mode`(`standard`/`simple`/`network_only`), `timeout_ms` | 페이지 본문 텍스트(실패·차단 시 빈 문자열) |
| `search_and_fetch` | `query`, `num_results`(기본 3), `mode` | `--- SOURCE: <url> ---` 블록으로 이어 붙인 본문 |
| `list_engines` | 없음 | 현재 엔진 체인과 사용 가능한 엔진 |

## 5. 보안

- 기본 바인드는 `127.0.0.1`입니다. 외부 인터페이스에 열 때는 `fetch` 도구가 열린 프록시가 되지 않도록 반드시 `SEARCH_MCP_TOKEN`을 설정하세요. 토큰 없이 외부 바인드하면 경고 로그가 남습니다.
- 토큰이 설정되면 `Authorization: Bearer <토큰>`이 없는 요청은 401로 거부합니다.
- localhost 바인드 시 MCP SDK의 DNS 리바인딩 보호가 자동으로 켜집니다.

## 6. Claude Code 등 다른 클라이언트에서 쓰기

```bash
claude mcp add --transport http hydra-search http://127.0.0.1:8790/mcp
# 토큰 사용 시: --header "Authorization: Bearer <토큰>"
```
