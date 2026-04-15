# HydraLLM: Intelligence Orchestrator

> The Korean document ([README.ko.md](README.ko.md)) is the primary reference. This English version is kept as a secondary summary.

**HydraLLM** is a context-aware gateway that routes requests across Gemini / Groq / Cerebras with per-provider circuit breakers, random key rotation with quota-aware cooldowns, and real-time web enrichment, all behind an OpenAI-compatible API built on a strict Clean Architecture (Domain then Services then Adapters then API).

- **Version**: `1.3.0` (`pyproject.toml`)
- **Python**: `3.10+`
- **Entry point**: `python main.py`
- **Unified UI**: `http://localhost:8000/ui`
- **OpenAI-compatible endpoint**: `POST /v1/chat/completions`

## Project Structure

```text
.
├── main.py                       # Uvicorn entry point (supports --debug, --port)
├── src/
│   ├── app.py                    # FastAPI factory, lifespan, static UI mount
│   ├── adapters/providers/       # gemini, openai_compat (Groq/Ollama), cerebras, local_cli
│   ├── api/v1/                   # endpoints.py, dependencies.py
│   ├── core/                     # config, container, exceptions, logging
│   ├── domain/                   # enums, interfaces, schemas, models
│   ├── services/                 # analyzer, gateway, key_manager, session_manager,
│   │                             # scraper, compressor, web_context_service,
│   │                             # admin_service, metrics_service, observability,
│   │                             # session_orchestrator, context_manager
│   └── utils/                    # ulid helpers
├── tests/
│   ├── unit/                     # analyzer, key_manager, adapters, ulid, stability
│   ├── integration/              # gateway failover, auto-models, provider validation
│   └── api/                      # FastAPI endpoint contract tests
├── static/                       # Unified SPA (Playground + Dashboard)
├── samples/                      # Request body samples
├── scripts/                      # analyze_logs.py, check_imports.py, run_final_test.py
├── examples/                     # cURL / SDK usage examples
├── pyproject.toml                # Poetry, ruff, mypy, pytest configuration
└── .env                          # Provider keys and runtime settings (gitignored)
```

## Key Capabilities

1. **Intelligent Routing** — `services/analyzer.py::ContextAnalyzer` picks a provider/model based on token count, multimodality, detected web intent, explicit model hints (`provider/model`), and available key tiers.
2. **Circuit Breaker + Cloud Failover** — `services/gateway.py` wraps every provider with a `CircuitBreaker` (5-failure threshold, 60s recovery) and retries across the `PROVIDER_PRIORITY` chain (Gemini then Groq then Cerebras).
3. **Final Local Fallback** — When all cloud providers are exhausted, the Gateway routes to Ollama via `OpenAICompatAdapter` pointing at `OLLAMA_BASE_URL`.
4. **Key Rotation with Cooldowns** — `services/key_manager.py::KeyManager` maintains per-provider pools, selects keys randomly from the active set, and applies longer cooldowns for quota (1h) or forbidden/403 (24h) errors.
5. **Web Enrichment** — `services/web_context_service.py` + `services/scraper.py::WebScraper` (Playwright + Scrapling) fetch explicit URLs or perform scraping when web intent is detected, with a 24-hour SQLite cache.
6. **Context Compression** — `services/compressor.py::ContextCompressor` uses LLMLingua-2 (optional `compression` extra) to prune long histories.
7. **Session Persistence** — `services/session_manager.py::SessionManager` stores messages and parts in SQLite (WAL), supports forking and compaction thresholds, and holds runtime settings.
8. **Unified Admin UI** — Single SPA at `/ui` combining playground, dashboard, key status, and model catalogue; all fetches use absolute URLs for proxy stability.
9. **OpenAI API Compatibility** — `/v1/chat/completions` including streaming SSE (`chat.completion.chunk` + `[DONE]`).

## API Surface

All endpoints are mounted under `/v1` via `src/api/v1/endpoints.py`.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/chat/completions` | Primary chat entry (streaming supported) |
| `GET`  | `/v1/models` | List all discovered models |
| `GET`  | `/v1/admin/sessions` | List persisted sessions |
| `POST` | `/v1/admin/sessions/new` | Create a new session |
| `DELETE` | `/v1/admin/sessions/{session_id}` | Delete a session |
| `GET`  | `/v1/admin/logs?limit=50` | Recent system logs |
| `GET`  | `/v1/admin/stats` | Aggregate usage + health stats |
| `GET`  | `/v1/admin/dashboard` | Stats + recent logs for the UI |
| `GET`  | `/v1/admin/status` | Live provider/agent status |
| `POST` | `/v1/admin/refresh-models` | Re-run provider model discovery |
| `POST` | `/v1/admin/probe` | Probe all keys for health |
| `POST` | `/v1/admin/keys` | Add runtime keys (see Known Issues) |
| `GET`  | `/v1/admin/onboarding` | Onboarding status + available models |
| `POST` | `/v1/admin/onboarding` | Save onboarding choices |

Plus the root and UI routes:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Service banner (links to `/docs`, `/openapi.json`, `/ui`) |
| `GET` | `/ui` | Unified admin SPA (`static/index.html`) |
| `GET` | `/ui/static/*` | Static assets |

## Commands

```bash
# Run server (defaults to port 8000)
python main.py
python main.py --debug --port 8001

# Tests
pytest                    # full suite
pytest -m unit            # unit tests only
pytest -m integration     # integration tests only
pytest tests/unit/test_analyzer.py::test_auto_routing   # single test

# Code quality
ruff check .
ruff check --fix .
mypy src/
```

## Configuration

Settings are loaded from `.env` via `pydantic-settings` (`src/core/config.py::Settings`).
Key variables:

- **Keys (comma-separated pools)** — `GEMINI_KEYS`, `GROQ_KEYS`, `CEREBRAS_KEYS`
- **Priority** — `PROVIDER_PRIORITY=gemini,groq,cerebras,ollama,opencode,openclaw`
- **Routing defaults** — `DEFAULT_FREE_MODEL`, `DEFAULT_PREMIUM_MODEL`, `MAX_TOKENS_FAST_MODEL`
- **Local agents** — `OLLAMA_BASE_URL`, `OPENCODE_BASE_URL`, `OPENCLAW_BASE_URL`
- **Features** — `ENABLE_CONTEXT_COMPRESSION`, `ENABLE_AUTO_WEB_FETCH`, `WEB_CACHE_TTL_HOURS`
- **Admin** — `ADMIN_API_KEY` (optional; unset disables admin auth)

See `.env.example` for the full list with example values. `.env` is listed in `.gitignore` and must not be committed.

## Known Issues (validated 2026-04-15)

The following are tracked and intentionally left uncorrected in this documentation pass. Run details live in `TROUBLESHOOTING.ko.md` (Korean primary) and `TROUBLESHOOTING.md` (English summary), sections 11 through 14.

- **pytest**: 2 failed / 70 passed.
  - `tests/integration/test_integration.py::TestIntegration::test_admin_keys_endpoint` — `POST /v1/admin/keys` returns `422` because the endpoint signature (`provider: str, keys: list[str] = Body(...)`) makes `provider` a query parameter, while the test sends both fields in the JSON body.
  - `tests/integration/test_auto_models_functionality.py::test_auto_models_functionality` — the final local fallback calls `OpenAICompatAdapter` at `OLLAMA_BASE_URL` with the first discovered model, which can be an embedding model such as `bge-m3:latest`; Ollama then rejects the chat request with `400 does not support chat`.
  - unit + api tests (63 total) all pass; the other integration tests pass but `test_auto_models.py` is slow because it performs live LLM calls.
- **mypy**: 35 errors across 10 files.
  - `services/admin_service.py:49` — `AdminService.delete_session` calls `session_manager.delete_session`, but neither `ISessionManager` nor the concrete `SessionManager` defines that method today; only `clear_session` and `fork_session` exist. Calling `DELETE /v1/admin/sessions/{id}` will raise an `AttributeError` at runtime until the interface and the concrete class are aligned.
  - `services/analyzer.py:104` and `adapters/providers/gemini.py:81` — spurious "missing named argument" reports on `RoutingDecision` / `ChatMessage` because no Pydantic-v2 mypy plugin is configured; the fields do carry defaults at runtime.
  - `services/gateway.py:211,214` — type narrowing for `all_parts` elements (`dict | BaseModel`) and `msg.model_extra` assignment is not preserved through the current `dict[str, Any]` typing.
  - `services/key_manager.py:117` — `cooldown_seconds` is assigned `timedelta.total_seconds()` (float) on one branch and `int` on others; the variable is first inferred as `int`.
  - `services/scraper.py:13-15` — `ProxySpec` is redefined as a class after being imported as a typing alias.
  - `services/context_manager.py`, `services/session_manager.py`, `api/v1/endpoints.py`, `app.py` — several functions still lack explicit return annotations.
- **ruff**: 54 errors, none blocking runtime.
  - `B904` (14 occurrences) in `api/v1/endpoints.py` — `raise HTTPException(...)` inside `except` clauses does not use `raise ... from err`.
  - `F403` in `src/domain/{enums,interfaces,schemas}/__init__.py` — `from .logic import *` re-exports are opaque to static analysis.
  - `E402` across test files that manipulate `sys.path` before importing from `src` (per `tests/AGENTS.md` convention).
  - `EM101/EM102` in `gemini.py` and `gateway.py`, `F841` unused `last_exception` in `gateway.py`, `F401` unused `re` import in `endpoints.py`.
- **Version drift**: `pyproject.toml` declares `1.3.0`, but `src/app.py` still constructs `FastAPI(version="1.0.0")`, so the OpenAPI spec exposes the older value.

---
*Last Updated: 2026-04-15*
