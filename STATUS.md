# HydraLLM Development Status

> The Korean document ([STATUS.ko.md](STATUS.ko.md)) is the primary reference. This English version is kept as a secondary summary.

This document summarizes the current technical maturity, completed features, and the outstanding work for the HydraLLM project. It is refreshed against the real codebase on every documentation pass.

- **Version**: `1.3.0` (`pyproject.toml`)
- **Python target**: `3.10+`
- **Last validated**: `2026-04-15`

---

## Completed

### 1. Core Architecture and Routing
- [x] **Clean Architecture** — strict separation of `domain/`, `services/`, `adapters/`, and `api/` layers (see `AGENTS.ko.md`).
- [x] **Intelligent context routing** — `ContextAnalyzer` chooses a provider/model using token count, multimodality, detected web intent, explicit hints, and tier availability.
- [x] **Provider integration** — first-class support for Google Gemini (native GenAI SDK), Groq (OpenAI-compatible adapter), and Cerebras.
- [x] **Local agent wrapping** — Ollama (via `OpenAICompatAdapter`) plus OpenCode/OpenClaw (via `LocalCLIAdapter`) with runtime model discovery.

### 2. Resilience and Stability
- [x] **Circuit breaker** — per-provider `CircuitBreaker` (5-failure threshold, 60s recovery) wraps every cloud provider call.
- [x] **Multi-provider failover** — `_execute_with_full_resilience` retries the decided provider, then walks `PROVIDER_PRIORITY` until success or exhaustion.
- [x] **Final local fallback** — `_final_fallback` routes exhausted traffic to Ollama via the OpenAI-compatible adapter.
- [x] **Key recovery loop** — `Gateway.recover_failed_keys` runs every 60s as a background task to re-probe failed keys.
- [x] **Quota / forbidden cooldowns** — `KeyManager.report_failure` distinguishes 403 (24h), quota (1h), and generic (5m) cooldowns.

### 3. Data and Session Management
- [x] **SQLite WAL persistence** — `SessionManager` stores sessions, messages, parts, system logs, usage, scraping cache, and runtime settings.
- [x] **Session compaction** — `SessionOrchestrator` + `ContextCompressor` (LLMLingua-2) prune histories beyond `session_compact_threshold`.
- [x] **Session forking** — `SessionManager.fork_session` branches an existing conversation from a specific message.
- [x] **Runtime settings store** — onboarding status and enabled-models allowlist are read from SQLite on startup.

### 4. Information Retrieval and Enrichment
- [x] **Web context service** — `WebContextService.enrich_request` fetches explicit URLs and web-intent queries, injecting `WebFetchPartData` / `WebSearchPartData` into the message parts.
- [x] **Scrapling + Playwright scraper** — `WebScraper` handles headless browsing with SSRF-safe URL handling.
- [x] **Web cache** — 24-hour SQLite-backed cache with metadata visible on the dashboard.
- [x] **Metadata stripping** — search queries drop untrusted metadata from upstream agents before being dispatched.

### 5. Monitoring and Administration
- [x] **Unified Admin UI** — single SPA at `/ui` combining playground, dashboard, provider status, and model catalogue; all fetches use absolute URLs.
- [x] **Metrics service** — `MetricsService.record_request` persists per-request tokens, latency, status, endpoint, and provider.
- [x] **Observability tracing** — `Observability` records routing/enrichment/LLM steps per `req_…` ID.
- [x] **Admin API surface** — sessions CRUD, logs, stats, dashboard, provider status, probe, refresh-models, onboarding, runtime key injection.
- [x] **Log analysis utility** — `scripts/analyze_logs.py` performs automated issue diagnosis over `gateway.log`.
- [x] **Request ID tracing** — every log line carries the `[req_…]` context for correlation.

### 6. API and Compatibility
- [x] **OpenAI-compatible chat endpoint** — `POST /v1/chat/completions` with streaming SSE (`chat.completion.chunk` + `[DONE]`).
- [x] **Model discovery endpoint** — `GET /v1/models` lists all registered models including virtual `auto`/`<provider>/auto` entries.
- [x] **FastAPI lifespan discovery** — initial provider model discovery and key probing run as background tasks on startup.

---

## Outstanding Issues (2026-04-15 validation snapshot)

These are kept as explicit known issues rather than silently masked. See `TROUBLESHOOTING.ko.md` (Korean primary) and `TROUBLESHOOTING.md` (English) sections 11 through 14 for full details.

### Test failures (`pytest` — 2 failed / 70 passed)
- unit + api: 63 passed (single run).
- `tests/integration/test_integration.py::TestIntegration::test_admin_keys_endpoint` — `POST /v1/admin/keys` returns `422` because the endpoint signature treats `provider` as a query parameter while the test sends both fields in the JSON body.
- `tests/integration/test_auto_models_functionality.py::test_auto_models_functionality` — the local Ollama fallback selects the first discovered model, which can be an embedding-only model such as `bge-m3:latest`; Ollama rejects the chat request with `400 does not support chat`.

### Type errors (`mypy src/` — 35 errors / 10 files)
- `services/admin_service.py` calls `session_manager.delete_session`. This method exists on neither `ISessionManager` nor the concrete `SessionManager`, so `DELETE /v1/admin/sessions/{id}` will fail at runtime with `AttributeError` until the interface and implementation add it.
- `services/analyzer.py` / `adapters/providers/gemini.py` — spurious "missing named argument" reports on `RoutingDecision` and `ChatMessage` because no Pydantic-v2 mypy plugin is configured.
- `services/gateway.py` — type narrowing gaps around `msg.model_extra` and `dict | BaseModel` parts.
- `services/key_manager.py` — `cooldown_seconds` type drifts between `int` and `float`.
- `services/scraper.py` — `ProxySpec` is redefined after import as a typing alias.
- Several API / app / service functions still lack return annotations.

### Lint errors (`ruff check .` — 54 errors)
- `api/v1/endpoints.py` — 14x `B904` (exception-chain omissions) and one unused `re` import.
- `src/domain/{enums,interfaces,schemas}/__init__.py` — `F403` wildcard re-exports.
- `services/gateway.py` — `F841` unused `last_exception`, `EM102` f-string in exception.
- `adapters/providers/gemini.py` — `EM101`/`EM102` string-literal exceptions.
- Several test files — `E402` import-order violations arising from `sys.path` manipulation before importing from `src/` (per `tests/AGENTS.md` convention).

### Version drift
- `pyproject.toml` is at `1.3.0`, but `src/app.py::create_app` still passes `version="1.0.0"` to the FastAPI constructor, so the OpenAPI spec reports the older value.

---
*Last Updated: 2026-04-15*
