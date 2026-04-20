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
├── scripts/                      # analyze_logs.py (log analysis utility)
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
10. **Incremental Web-Intent Keyword Learning** — `services/keyword_store.py::KeywordStore` persists per-language (`ko`, `en`) keywords to JSON files (`data/web_keywords.{lang}.json`); `services/intent_classifier.py::IntentClassifier` substring-matches them before falling back to embedding similarity. `scripts/validate_flow.py` automatically registers false-negative queries to `/v1/admin/intent/keywords/learn` to grow the lexicon.

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
| `GET`  | `/v1/admin/intent/keywords` | List web-intent keywords per language |
| `POST` | `/v1/admin/intent/keywords` | `{lang,keywords[]}` manual keyword registration |
| `POST` | `/v1/admin/intent/keywords/learn` | `{query}` learn keywords from a false-negative query (LLM extraction + regex fallback) |

Plus the root and UI routes:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Service banner (links to `/docs`, `/openapi.json`, `/ui`) |
| `GET` | `/ui` | Unified admin SPA (`static/index.html`) |
| `GET` | `/ui/static/*` | Static assets |

## Installation

### 1. Create a virtual environment (recommended)

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

> ⚠️ **pydantic v2 required**: this project needs `pydantic>=2.5` and `pydantic-settings>=2.1`. If pydantic v1 is present in `~/.local`, you will see `ModuleNotFoundError: No module named 'pydantic._internal'`. Use a venv, or upgrade with `pip install --upgrade 'pydantic>=2.5'`.

### 2. Install dependencies (pick one)

```bash
# (A) pip
pip install -r requirements.txt

# (B) Poetry
poetry install
# (optional) enable the context-compression extra
poetry install -E compression
```

### 3. Install Playwright browsers

The web scraper (`services/scraper.py`) drives Chromium, so a one-time download is required.

```bash
python -m playwright install chromium
```

### 4. Configure environment

```bash
cp .env.example .env
# edit .env to set GEMINI_KEYS, GROQ_KEYS, CEREBRAS_KEYS, etc.
```

### 5. Smoke test

```bash
python main.py           # starts on port 8000
curl http://127.0.0.1:8000/   # {"status":"online", ...}
```

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
- **Web-intent keyword store** — `DATA_DIR` (default `data/`), `KEYWORD_EXTRACTION_MODEL` (Ollama small LLM name; regex fallback only when unset)

See `.env.example` for the full list with example values. `.env` is listed in `.gitignore` and must not be committed.

## Known Issues (validated 2026-04-16)

- **pytest (unit)**: 52/52 passed. Integration test `test_auto_models_functionality` may fail when the local Ollama instance returns an embedding-only model for chat (see `TROUBLESHOOTING.md` section 12).
- **mypy**: 0 errors (`mypy src/`). Pydantic v2 mypy plugin enabled.
- **ruff (src/)**: 0 errors. Test files retain `E402` import-order violations due to `sys.path` manipulation (by design).
- **Version**: `pyproject.toml` and `src/app.py` both declare `1.3.0`.

---
*Last Updated: 2026-04-16*
