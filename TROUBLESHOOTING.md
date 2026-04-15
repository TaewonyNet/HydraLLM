# HydraLLM Troubleshooting Guide

> The Korean document ([TROUBLESHOOTING.ko.md](TROUBLESHOOTING.ko.md)) is the primary reference. This English version is kept as a secondary summary.

This document records solved problems and currently-open issues to help AI agents and developers diagnose HydraLLM from logs and test output.

---

## Log-Based Diagnosis Workflow

When a "problem occurred" report is received:

1. **Identify the Session/Request ID** — grep `gateway.log` for the session ID or the `[req_…]` context prefix that `src/core/logging.py` injects.
2. **Trace the lifecycle** — search for all logs sharing the same `req_…` to see the full flow: `Routing decision -> web_enrichment -> provider execution -> (fallback) -> response`.
3. **Check the usage envelope** — `response.usage.gateway_provider`, `gateway_key_index`, `gateway_model`, and `routing_reason` record exactly which resource served the request.
4. **Cross-reference metrics** — `MetricsService.record_request` writes every request with status, latency, and token counts; query the SQLite `gateway_sessions.sqlite` via the admin endpoints.

---

## Solved Issues and Patterns

### 1. TypeError: cannot unpack non-iterable NoneType object
- **Symptom**: 500 Internal Server Error, `TypeError` in `gateway.py` during `_process_with_retries` unpacking.
- **Root cause**: The retry loop finished without returning a value or raising an exception (usually a silently failed fallback), causing an implicit `None` return.
- **Fix**: Ensure `raise last_exception` is present outside the retry loop and that every fallback path returns a valid `(ChatResponse, list[dict])` tuple.

### 2. 404 Model Not Found (Local Agents)
- **Symptom**: `Agent: ollama (Model: llama3) - Error: 404 Not Found`.
- **Root cause**: Hardcoded model names (like `llama3`) in adapter defaults that do not exist on the local machine.
- **Fix**: Call `adapter.discover_models()` at execution time and map generic hints (`ollama`, `auto`) to the first discovered model.

### 3. 429 Quota Exceeded (Gemini/Groq)
- **Symptom**: `Rate limit exceeded: 429` from one or more providers.
- **Root cause**: Free-tier limits reached on a specific API key.
- **Fix**:
  - **Strict recovery** — `KeyManager` keeps a 429-failed key in the failed pool until a probe actually succeeds.
  - **Provider fallback** — when all keys for provider A are failing, the retry loop switches to provider B.
  - **Local fallback** — if every external provider fails, `_final_fallback` routes to Ollama.

### 4. UI Load Issues (Dashboard/Status not updating)
- **Symptom**: UI shows "loading..." or fails to fetch dashboard stats.
- **Root cause**: Relative API paths in static HTML files pointed to the wrong origin once the app was served behind a proxy.
- **Fix**: `static/index.html` and `static/admin.html` use absolute URLs (`http://localhost:8000/v1/...`).

### 5. Gemini Search Failures (400 Bad Request)
- **Symptom**: Gemini requests fail with `400 google_search_retrieval is not supported`.
- **Root cause**: Google GenAI SDK renamed the tool from `google_search_retrieval` to `google_search`.
- **Fix**: `src/adapters/providers/gemini.py` now declares `tools = [{"google_search": {}}]`.

### 6. Cerebras Provider Failures
- **Symptom**: `Unexpected error: name 'CerebrasAdapter' is not defined`.
- **Root cause**: Missing import in `gateway.py` and missing initialization in `app.py`.
- **Fix**: Added the adapter import to `gateway.py` and wired `cerebras_keys` into `create_app`.

### 7. WebScraper vs Native Google Search
- **Behavior**: `WebScraper` fails on anti-bot sites (e.g., Brunch), causing fallback to Gemini's native `google_search`.
- **Reason**: If `WebContextService` fails to fetch a URL and `has_search` is true, the LLM uses its built-in tool to satisfy the intent.
- **How to detect**: grep `gateway.log` for `Scrapling failed` followed by `Sending request to Gemini with tools`.

### 8. URL Detection Failure (Regex Issue)
- **Symptom**: URLs followed by Korean text (e.g., `https://brunch.co.kr/@id/123` then a trailing phrase) were not detected for web fetching.
- **Root cause**: The original `_URL_PATTERN` regex in `web_context_service.py` was too strict and returned partial matches when additional characters followed the URL.
- **Fix**: Simplified the regex to `r"https?://[^\s()<>]+"` so it captures the whole URL string until whitespace or an invalid delimiter.

### 9. IndentationError in Streaming Endpoint
- **Symptom**: `IndentationError: unexpected indent` in `src/api/v1/endpoints.py` during test collection or server startup.
- **Root cause**: Duplicate code blocks and mismatched indentation inside the streaming `AsyncIterator` in `_handle_chat_completion`.
- **Fix**: `generate_stream` now has a single `try/except` pair and correct indentation for the `yield` / `return` statements.

### 10. WebScraper Bypassed for Explicit URLs
- **Symptom**: URLs in the user query were not fetched when `auto_web_fetch` was `False`.
- **Root cause**: `enrich_request` in `web_context_service.py` returned early when `auto_web_fetch` and intent detection were both false, even if `urls_to_fetch` had entries.
- **Fix**: The URL extraction now runs before the early-return check, and the check includes `urls_to_fetch` existence.

---

## Open Issues (2026-04-15 validation snapshot)

These are detected by the current `pytest` / `mypy` / `ruff` runs. They are intentionally not masked — they are tracked so that future fixes can reference a consistent baseline.

### 11. `POST /v1/admin/keys` returns 422 (integration test failing)
- **Reproduce**: `pytest tests/integration/test_integration.py::TestIntegration::test_admin_keys_endpoint`
- **Symptom**: `assert 422 == 200` — the endpoint rejects `{"provider": "gemini", "keys": ["test-key-1"]}` sent as JSON.
- **Root cause**: `src/api/v1/endpoints.py::add_keys` is declared as `async def add_keys(provider: str, keys: list[str] = Body(...), ...)`. Because only `keys` carries a `Body(...)` annotation, FastAPI binds `provider` to a query parameter. The test sends both fields inside the JSON body, so FastAPI reports `provider` missing and `keys` embedded incorrectly.
- **Proposed fix** (not applied): either wrap both fields in a single Pydantic payload model, or annotate `provider: str = Body(...)` / use `Body(..., embed=True)` consistently.

### 12. Ollama final fallback picks an embedding model (integration test failing)
- **Reproduce**: `pytest tests/integration/test_auto_models_functionality.py::test_auto_models_functionality`
- **Symptom**: `400 - "bge-m3:latest" does not support chat` after `All primary paths failed. Triggering final local fallback.`
- **Root cause**: `Gateway._final_fallback` sets `decision.model_name = ""` and defers to `_process_with_agent`, which selects the first model returned by `OpenAICompatAdapter.discover_models()` (Ollama lists every served model, including embedders). When no `llama` substring match exists, the first model wins — so embedding models like `bge-m3:latest` can be chosen for chat. `OpenAICompatAdapter.discover_models` only filters names containing `embed`, `rerank`, or `vision-adapter`, and `bge-m3` slips through because it does not include those keywords.
- **Proposed fix** (not applied): filter Ollama's discovered list by capability (reject `embedding` / known non-chat architectures) or expose a configurable preferred-model pattern instead of the `llama`-only heuristic currently in `_process_with_agent`.

### 13. mypy gaps (`mypy src/` — 35 errors)
- **Reproduce**: `mypy src/`
- **Notable errors**:
  - `services/admin_service.py:49` — `AdminService.delete_session` calls `session_manager.delete_session`, but that method exists on neither `ISessionManager` nor the concrete `SessionManager`. `DELETE /v1/admin/sessions/{id}` will raise `AttributeError` at runtime until the method is added to both.
  - `services/analyzer.py:104`, `adapters/providers/gemini.py:81` — "missing named argument" errors for `RoutingDecision` / `ChatMessage`. These fields carry defaults but mypy cannot see them without the Pydantic v2 plugin.
  - `services/gateway.py:211,214` — `dict` vs `BaseModel` union narrowing around `model_dump`/`model_extra`.
  - `services/key_manager.py:117` — `cooldown_seconds` branches between `int` and `timedelta.total_seconds()` (float).
  - `services/scraper.py:13-15` — `ProxySpec` typing alias is reassigned as a class.
  - `api/v1/endpoints.py`, `app.py`, `services/context_manager.py`, `services/session_manager.py` — missing return annotations on several small helpers.
- **Proposed fixes** (not applied): install `pydantic.mypy` plugin in `pyproject.toml`, align `ISessionManager`/`SessionManager` with the `AdminService` call, tighten `key_manager` numeric types, and add return annotations.

### 14. ruff lint backlog (`ruff check .` — 54 errors)
- **Reproduce**: `ruff check .`
- **Notable categories**:
  - `B904` (14 occurrences) in `api/v1/endpoints.py` — `raise HTTPException(...)` inside `except` clauses should use `raise ... from err`.
  - `F401` unused `re` import in `api/v1/endpoints.py`.
  - `F403` wildcard re-exports in `src/domain/{enums,interfaces,schemas}/__init__.py`.
  - `F841` unused `last_exception` in `services/gateway.py`.
  - `EM101`/`EM102` string-literal exceptions in `adapters/providers/gemini.py` and `services/gateway.py`.
  - `E402` import-order violations in test files that prepend `sys.path` before importing from `src/` (per `tests/AGENTS.md` convention). If the tests move to a proper package layout these can be removed.
- **Proposed fixes** (not applied): `ruff check . --fix` handles four of them automatically; the rest require small refactors.

---

## Log Optimization Tips
- Every log entry MUST include `[req_…]` for correlation — the `setup_logging` configuration in `src/core/logging.py` injects it via `request_id_ctx`.
- Use `DEBUG` level for full request/response payloads when `settings.debug` is enabled (`python main.py --debug`).
- Avoid JSON log formatting for human readability, but keep field order consistent so `scripts/analyze_logs.py` can parse it.

---
*Last Updated: 2026-04-15*
