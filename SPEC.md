# Project Specification: HydraLLM (Context-Aware Multi-LLM Gateway)

- **Version:** 1.3.0
- **Runtime:** Python 3.10+ (FastAPI)
- **Architecture:** Clean Architecture (Domain → Service → Adapter → API)

---

## 1. Overview, Purpose & Philosophy

### 1.1 What HydraLLM Is

**HydraLLM** is a **locally-run orchestration gateway** that unifies heterogeneous LLM resources — cloud free/low-cost tiers (Gemini, Groq, Cerebras) and local agents (Ollama, OpenCode, OpenClaw) — behind a single OpenAI-compatible endpoint (`POST /v1/chat/completions`).

"Local" describes where the gateway *runs* and where it can fully fall back to, **not** where the models live. Any OpenAI-compatible client (Claude Code, Cursor, OpenClaw, Continue) connects by changing only its `base_url`.

### 1.2 Purpose (two layers, stated explicitly)

- **Research / educational framing (primary intent).** HydraLLM is an open research project that *demonstrates* techniques — API orchestration, dynamic resource management / load optimization, context-aware routing across heterogeneous providers, and real-time context enrichment. It is **not** a production-SLA product, and responsibility for each provider's Terms of Service and rate limits rests entirely with the user (see `DISCLAIMER.md`).
- **Practical behaviour (the form that demonstration takes).** A single highly-available endpoint that lets clients use a pool of free/low-cost keys as if it were one premium endpoint, degrading gracefully to local models when cloud resources are exhausted.

### 1.3 Non-Goals (explicit scope boundaries)

- Not a production-SLA / billing / cost-tracking platform.
- Not a fine-tuning or model-training system.
- Not distributed — single-node only (Redis-backed key/breaker sync is roadmap, see `IMPROVEMENTS.md`).

### 1.4 Design Philosophy

The codebase shape follows from these principles (each is a *target*; where the current implementation diverges it is tracked as a defect, not a redefinition of intent):

1. **One socket.** Every provider hides behind the OpenAI schema; drop-in `base_url` swap.
2. **Availability first, graceful degradation.** CircuitBreaker → `PROVIDER_PRIORITY` fallback → final local fallback (Ollama). Never hard-fail while any resource remains.
3. **Context-aware right-sizing.** Route to the cheapest/fastest *adequate* model for the request context, not always the most capable one.
4. **Local sovereignty & self-sufficiency.** Runs locally; can degrade entirely to local agents with no hard cloud dependency.
5. **Self-healing & autonomy.** Background key re-probing (honouring cooldowns), dynamic model discovery, web-keyword learning, scraper auto-restart.
6. **Continuity across transitions.** Session context survives provider/agent/model switches via SQLite (WAL).
7. **Real-time grounding.** Web enrichment overcomes training-cutoff staleness; the "Web context injected" log makes application observable.
8. **Strict boundaries.** Unidirectional Clean Architecture, thin orchestrator + collaborators, async-first.
9. **Observability & transparency.** `routing_reason` constants, comm logs, metrics, unified `/ui` dashboard.
10. **Research framing, user-borne compliance.** Explicitly experimental; multi-key rotation's ToS sensitivity is disclosed.

---

## 2. System Architecture

### 2.1 Directory Structure

```
src/
├── app.py                      # FastAPI App Factory + Lifespan (Discovery, Probing, Recovery)
├── core/
│   ├── config.py               # Pydantic Settings — Centralized environment management
│   ├── exceptions.py           # Custom Exceptions (ResourceExhaustedError, RateLimitError, etc.)
│   └── logging.py              # Configuration for local and console logging
├── domain/                     # Pure, framework-free (nested `logic.py` modules)
│   ├── enums/logic.py          # ProviderType, AgentType, ModelType, TierType, RoutingReason, PartType
│   ├── models.py               # ChatRequest, ChatResponse, ChatMessage, RoutingDecision (Pydantic v2)
│   ├── schemas/logic.py        # API Response DTOs (ModelInfo, ModelListResponse, ProviderStatus)
│   └── interfaces/logic.py     # ABCs: ILLMProvider, IContextAnalyzer, IKeyManager, IRouter, ISessionManager, IContextManager
├── services/                   # Gateway is a thin orchestrator; heavy logic lives in collaborators
│   ├── gateway.py              # Orchestration, URL auto-detection, retry-loop wiring
│   ├── resilience_manager.py   # Per-request retry loop, circuit-breaker checks, final_fallback→Ollama
│   ├── agent_executor.py       # Local-agent (Ollama/OpenCode/OpenClaw) execution + chat-model resolution; injected into ResilienceManager
│   ├── web_coordinator.py      # Web enrichment call, "Web context injected" log, messages.insert(-1)
│   ├── session_orchestrator.py # Session load/persist + compression handoff (exposed as gateway.sessions)
│   ├── analyzer.py             # Context analysis → Routing decision
│   ├── key_manager.py          # Key pools, random rotation, category-based cooldowns, probe recovery
│   ├── circuit_breaker.py      # Per-provider breaker (5 failures → open 60s)
│   ├── scraper.py              # Playwright-based web scraping (URL fetch, search, SSRF protection)
│   ├── web_context_service.py  # Web enrichment orchestration + scraping_metrics
│   ├── intent_classifier.py    # Web-intent classification (substring → embedding fallback)
│   ├── keyword_store.py        # Per-language web keyword persistence (data/web_keywords.{ko,en}.json)
│   ├── context_manager.py      # Multimodal context offload (used by Gemini adapter)
│   ├── compressor.py           # LLMLingua-2 based prompt/session compression
│   ├── session_manager.py      # SQLite WAL-based persistent session storage + settings (compaction delegated)
│   ├── memory_optimizer.py     # Compaction V2 (selective pruning + structured summary + dedup); SessionManager.compact delegates here
│   ├── admin_service.py        # Admin operations (onboarding, model refresh, settings)
│   ├── installer.py            # Local CLI agent installation (opencode/openclaw)
│   ├── comm_logger.py          # Provider request/response debug buffer (/admin/comm-logs)
│   ├── metrics_service.py      # Runtime metrics
│   └── observability.py        # Step-level observability (record_step)
├── adapters/
│   └── providers/
│       ├── gemini.py           # Google GenAI (format conversion, multimodal)
│       ├── openai_compat.py    # Groq + Ollama/local OpenAI-compatible endpoints (base_url override)
│       ├── cerebras.py         # Dedicated Cerebras adapter (CerebrasAdapter)
│       └── local_cli.py        # OpenCode, OpenClaw (subprocess-based integration)
├── api/
│   └── v1/
│       ├── endpoints.py        # Route definitions (chat, models, admin)
│       └── dependencies.py     # FastAPI Dependency Injection (services from app.state)
├── i18n/                       # Locale-based message translation (t(), set_locale)
└── utils/
    └── ulid.py                 # stdlib-only ULID / session-id generation
tests/
├── conftest.py                 # Project root discovery and sys.path setup
├── api/                        # API endpoint tests
├── unit/                       # Component logic tests
└── integration/                # Full request flow tests
```

### 2.2 Data Flow

`Gateway.process_request` orchestrates the steps below **in this exact order**. Note
that **routing is decided before web enrichment** — the routing token count reflects the
user query + session history, *not* the (potentially large) injected web reference data.

```
Client ──► POST /v1/chat/completions (or /v1/responses)
  │
  ▼  api/v1/endpoints.py  (admin routes gated by verify_admin_auth)
  ▼  Gateway.process_request
  │
  1. Normalize input            prompt → messages; reject empty
  2. SessionOrchestrator        load_history() + save_user_message()  ── SQLite WAL
  3. Compose prompt             prepend [SYSTEM CONTEXT] date/truth msg; merge+dedup history
  4. _get_available_tiers()     per provider: CircuitBreaker.is_available() → tier set
                                (OPEN breaker ⇒ empty set ⇒ provider excluded from routing)
  5. Analyzer.analyze()         ──► RoutingDecision(provider|agent, model_name, reason)
                                    [token count here is PRE-enrichment]
  6. WebEnrichmentCoordinator.enrich()
                                if web intent/URL: scrape → messages.insert(-1, web block)
                                emits "Web context injected: N chars ..." (canonical log)
  7. ResilienceManager.execute_with_resilience()   ── retry loop, see §6
        ├─ CircuitBreaker.is_available(provider)?  no → skip (strict ⇒ raise)
        ├─ KeyManager.get_next_key(provider)       random from active pool
        ├─ Adapter.generate()                      Gemini / OpenAI-compat / Cerebras / Local CLI
        ├─ success → CircuitBreaker.report_success + KeyManager.report_success → break
        └─ failure → CircuitBreaker.report_failure + KeyManager.report_failure(error)
                     ├─ retry (max 3) within provider
                     ├─ keys exhausted → fall through PROVIDER_PRIORITY chain
                     └─ all providers exhausted → final_fallback → Ollama (local)
  8. Persist response to session; return (streaming: chat.completion.chunk + [DONE])
```

---

## 3. Core Module Specifications

### 3.1 Domain Layer

#### `enums/logic.py`

- `ProviderType`: gemini, groq, cerebras.
- `AgentType`: ollama, opencode, openclaw.
- `ModelType`: discovered model identifiers per provider (do not hardcode; extended via discovery).
- `RoutingReason`: token_count, image_present, model_hint, key_availability, rate_limit.
- `PartType`: multimodal message part types (text, image, …).

> Web-search intent is carried by `RoutingStrategy.SEARCH_REQUIRED`, not by `RoutingReason`.

> Tiers are managed via `TierType` Enum: `FREE`, `STANDARD`, `PREMIUM`, `EXPERIMENTAL`, `UNKNOWN`.

#### `models.py`

- `ChatRequest`: Extends OpenAI standard with `session_id`, `has_search`, `web_fetch`, `compress_context`.
- `ChatResponse`: OpenAI compatible structure.
- `RoutingDecision`: Internal model for analyzer results.

#### `schemas/logic.py` — API Response DTOs (Pydantic v2)

`ProviderStatus`, `AgentStatus`, `ModelCapabilities`, `ModelInfo`, `ModelListResponse`.

#### `interfaces/logic.py` — ABC contracts

Six abstract base classes. Adapters/services implement these; inner layers depend on
the ABCs, never on concrete outer classes.

- `ILLMProvider`: `generate`, `get_supported_models`, `get_max_tokens`, `is_multimodal`, `discover_models`, `probe_key`.
- `IContextAnalyzer`: `analyze`, `get_supported_models_info`, `get_all_discovered_models_info`, `register_model`.
- `IKeyManager`: `get_next_key(provider, min_tier)`, `report_success`, `report_failure(provider, key, error)`, `get_key_status`.
- `IRouter`: `route_request`, `get_status`, `get_supported_models`, `get_all_models`.
- `ISessionManager`: session load/persist, settings, provider-health, and web-cache/scraping-metrics accessors (SQLite-backed).
- `IContextManager`: multimodal context offload/caching (`should_offload`, `prepare_temp_file`, `get_cached_file`, `cache_file`, …).

> The Gemini adapter depends on the `IContextManager` ABC; `services.context_manager.ContextManager`
> is the concrete implementation. All adapters depend only on domain ABCs, never on concrete
> service classes (unidirectional dependency is fully observed).

---

## 4. API Specification

All routes are mounted under `/v1` via `api/v1/endpoints.py`. There are **2 public** routes
and **25 admin** routes; every admin route depends on `verify_admin_auth` (see §4.3).

### 4.1 Public routes

**`POST /v1/chat/completions`** — OpenAI-compatible chat. `ChatRequest` extends the OpenAI
body with: `session_id` (local persistence), `has_search` / `auto_web_fetch` / `web_fetch`
(web enrichment controls), `compress_context` (LLMLingua compaction), `prompt` (legacy
single-string input), `stream`. `model="auto"` (or `PROVIDER/auto`) triggers routing (§5).
When `stream=true`, the response is emitted as `chat.completion.chunk` SSE events
terminated by `data: [DONE]`.

**`GET /v1/models`** — dynamically discovered model catalogue (`ModelListResponse`); includes
per-model `multimodal` / `has_search` capabilities and virtual `PROVIDER/auto` entries.

> **Not implemented:** a `POST /v1/responses` OpenClaw alias (`input`→`messages`, immediate
> `response.created`) was described in earlier drafts but **does not exist** in `endpoints.py`
> and is **out of current scope**. It is recorded here only so the spec is not mistaken for
> claiming it; implement it as a future feature if OpenClaw `openai-responses` mode is needed.

### 4.2 Admin routes (25, all gated)

Grouped by concern (all under `/v1/admin`):

- **Observability**: `GET stats`, `GET dashboard`, `GET status`, `GET logs`, `POST logs/clear`, `GET comm-logs`, `DELETE comm-logs`.
- **Models/keys**: `POST refresh-models`, `POST probe`, `POST keys`.
- **Sessions**: `GET sessions`, `POST sessions/new`, `DELETE sessions/{id}`, `POST sessions/cleanup`, `GET sessions/{id}/messages`, `POST sessions/import`.
- **Settings/onboarding**: `GET/PUT settings`, `GET/POST onboarding`.
- **Install** (local agents): `GET install/status`, `POST install/{tool}` (runs `curl … | bash` for opencode/openclaw — keep behind auth, see §10).
- **Web-intent keywords**: `GET keywords`, `POST keywords` (`{lang, keywords[]}`), `POST keywords/learn` (`{query}`) — routed via `get_keyword_store` / `get_intent_classifier`.

### 4.3 Authentication & errors

- **Admin auth**: `X-Admin-Key` header checked against `ADMIN_API_KEY`. If the key is unset,
  access is **fail-closed (503)** unless running with `--debug` / `DEBUG=true`, in which case
  admin is open for local development (host binds `0.0.0.0`, so open-by-default is unsafe).
- **Error contract**: failures surface as HTTP error responses; internal exception detail
  MUST be masked in client-facing 5xx responses (full detail goes to server logs only) to
  avoid leaking provider endpoints / SDK internals.

---

## 5. Routing Strategy

`Analyzer.analyze` returns a `RoutingDecision` by evaluating the rules below as an ordered
**decision procedure — first match wins**. The token count used is measured on the user
query + session history (**pre-web-enrichment**, see §2.2); injected web data does not
change the routing tier.

```
1. Explicit provider/model hint
   ├─ "provider/model" or a recognized model id  → that provider; strict (no cross-provider fallback)
   └─ recognized cross-vendor alias (e.g. "gpt-4o") → mapped to a pre-defined equivalent
2. Provider-scoped auto  ("GEMINI/auto", "GROQ/auto", "CEREBRAS/auto")
   → stay within that provider; pick a model by its current key tiers
3. Multimodal  (images present)
   → Gemini Vision   (if no Gemini key is available → falls through to step 5 token routing)
4. Web intent  (has_search / detected real-time query)
   → influences **enrichment** (web context is scraped and injected, ≤6000 chars) but does
     **NOT** force a provider. The enriched request routes by token size like any other →
     usually the fast tier (de-funnels Gemini; the injected context fits the fast window).
5. Token count  (single canonical threshold = settings.max_tokens_fast_model, default 8192)
   ├─ tokens <  threshold → FAST TIER: Groq ↔ Cerebras, **round-robin** across available
   │                        fast-tier providers (load distribution, not Groq-always)
   └─ tokens >= threshold → Gemini (large context window)
6. Tier awareness  (applied within the above)
   → premium/Pro models are selected ONLY when PREMIUM-tier keys exist in the pool;
     otherwise the free/standard model for that provider is used.
```

**Invariants (enforced; violations are defects):**
- The threshold is `settings.max_tokens_fast_model` **everywhere** — both the `auto`
  promotion path and `_determine_strategy`. No separate hardcoded cut-off (e.g. 7000).
- **Provider-keyword precedence**: when a model id contains a provider keyword
  (`cerebras`, `groq`), that keyword wins over generic family substrings (`llama`,
  `deepseek`); `ollama` (which contains the substring `llama`) is matched first of all.
- A provider whose CircuitBreaker is OPEN, or whose key pool is empty, is excluded from
  candidate selection (its tier set is empty in step 5/6).

`TierType` values: `FREE`, `STANDARD`, `PREMIUM`, `EXPERIMENTAL`, `UNKNOWN`.

### 5.1 Decisions are optimistic; key-absence is resolved at execution

The Analyzer returns the **context-ideal** provider and **never selects a local agent on its
own**. It is availability-aware only in the *fast / None-preferred* path (small query with no
fast-tier key falls to an available provider, e.g. Gemini); with **no keys at all** it still
returns a cloud default (small → Groq, large → Gemini). A **large/`auto`** request always
prefers Gemini (large window) **even if Gemini has no key**. The real key-absence handling —
provider fallback and degradation to local — happens in `ResilienceManager` (§6.4), not here.

---

## 6. Resilience & Fallback

Owned by `ResilienceManager.execute_with_resilience` (the retry loop) and per-provider
`CircuitBreaker` instances. New retry/fallback logic belongs here, not in `Gateway`.

> **Agent execution** (local-agent chat-model resolution + adapter call, used by the agent
> path and `final_fallback`) is owned by `AgentExecutor` (`services/agent_executor.py`).
> `Gateway` constructs it and injects `agent_executor.process` into `ResilienceManager` as
> `process_with_agent`; ResilienceManager only *calls* that callable.

### 6.1 Strict vs. fallback routing

Strictness is decided **once, by the Analyzer, from the original hint** and carried on
`RoutingDecision.strict` — `ResilienceManager` consumes `decision.strict` and never
re-parses `request.model` (which `Gateway` overwrites with `decision.model_name` before
execution). A request is **strict** (no cross-provider fallback) when the hint is explicit:
contains `/` and names a provider/agent (`gemini/…`, `GEMINI/auto`, `local-agent/…`).
Strict requests raise on failure of the named target. All other requests (`auto`, aliases
like `gpt-4o`) are **fallback** requests and traverse the provider chain.

> **Design note (decision vs. recovery, intentional):** the Analyzer (pure, deterministic,
> I/O-free) decides the *primary* target; `ResilienceManager` owns *recovery* (retry,
> circuit checks, provider fallback, local final-fallback) at execution time — because
> key-exhaustion/circuit-open/provider-5xx are only known while running. This split is a
> feature, not redundant routing; do not collapse the two layers.

### 6.2 Retry loop

```
agent path (if decision.agent set):
    try the local agent first; on failure (non-strict) → log + continue to cloud chain

for provider in PROVIDER_PRIORITY (gemini, groq, cerebras, …):
    if not CircuitBreaker[provider].is_available():       # OPEN
        strict → raise ResourceExhaustedError ; else skip provider
    for attempt in range(max_retries = 3):
        key = KeyManager.get_next_key(provider)
        resp = Adapter.generate(request, key)
        success → CircuitBreaker.report_success + KeyManager.report_success → return
        on RateLimitError / ResourceExhaustedError:
            CircuitBreaker.report_failure + KeyManager.report_failure(error)
            strict → raise ; if active keys == 0 → break to next provider ; else retry
        on ServiceUnavailableError: same handling (short backoff)
        on other Exception: report_failure to KeyManager only if 403/auth-like; strict → raise

all providers exhausted → final_fallback():
    decision.agent = OLLAMA ; model = "" → AgentExecutor.process (local, never circuit-broken)
```

### 6.3 CircuitBreaker state machine (per provider)

Single-threaded asyncio; methods are synchronous and therefore atomic (no lock needed).

| State | Behaviour |
|-------|-----------|
| CLOSED | requests pass; `failure_count` resets on success |
| → OPEN | after `failure_threshold` (5) consecutive failures; blocks for `recovery_timeout` (60s) |
| OPEN → HALF_OPEN | after the timeout elapses, admits **exactly one** probe (thundering-herd guard) |
| HALF_OPEN + success | → CLOSED, counters reset |
| HALF_OPEN + failure | → OPEN immediately (does not wait for threshold) |
| HALF_OPEN stuck probe | a new probe is admitted after `recovery_timeout` (self-heal) |

### 6.4 Key-absence & degradation

How missing keys actually route (resolved here, not in the Analyzer — see §5.1):

| Situation | Behaviour |
|-----------|-----------|
| Some providers lack keys | The decided provider's `get_next_key` fails → fall through the `PROVIDER_PRIORITY` chain to a provider that has keys. |
| **`auto` + no cloud keys at all** | Cloud attempts are **skipped entirely**; request goes **immediately to local Ollama** via `final_fallback` (`resilience_manager.py` early-exit). |
| All cloud providers exhausted mid-run | `final_fallback` → Ollama (local, never circuit-broken). |
| **strict** request (explicit `provider/model`) | **No fallback.** If the named provider has no key it raises `ResourceExhaustedError`; it does **not** degrade to local. |

Net effect: an `auto` request **never hard-fails** while any resource (including local Ollama)
remains; a **strict** request honours the user's explicit choice and fails fast instead.

---

## 7. Key Management

- **Random Rotation**: Uniform distribution of requests across the (eligible) key pool for balanced utilization.
- **Quota tagging**: On quota/billing errors a key is tagged in metadata (`is_quota_limit=True`) and moved to the failed pool with a 1h cooldown. Tier values are assigned at probe time (`probe_key`), **not** downgraded on failure.
- **Category-based Cooldowns**: On failure a key is moved to the failed pool with a `cooldown_until` derived from the error category:
  - `403 Forbidden`: 24 hours (persistent per-key fault).
  - `429 Rate Limit / Quota`: 1 hour.
  - Other transient errors: 5 minutes.
- **Self-Healing**: A background task polls every 60 seconds, but **only re-probes keys whose `cooldown_until` has elapsed**. The 60s interval is the polling cadence, not an override of the category cooldowns — a key in cooldown is skipped until its window expires. (Probing a 403-cooldown key every 60s would defeat the 24h backoff.)
- **Concurrency**: All mutations of the active/failed pools (`get_next_key`, `report_success`, `report_failure`, recovery) are guarded by the same `asyncio.Lock` to preserve pool integrity under load.

> **Note**: Multi-key rotation may be restricted by some providers' Terms of Service. Users are responsible for reviewing and complying with their provider's policies.

---

## 8. Web Enrichment

Grounds responses in real-time web data. Owned by `WebEnrichmentCoordinator`
(`web_coordinator.py`) over `WebContextService` (`web_context_service.py`),
`IntentClassifier`, `KeywordStore`, and `WebScraper`. Runs **after** routing (§2.2).

### 8.1 Trigger & fetch

`WebContextService.enrich_request(request)`:
1. `auto_web_fetch = request.auto_web_fetch ?? settings.enable_auto_web_fetch`.
2. Extract explicit URLs from the query via `_URL_PATTERN` (`https?://…`); append
   `request.web_fetch` if set.
3. If explicit URLs → scrape each. Otherwise, if `IntentClassifier.needs_web_search(query)`
   **or** `request.has_search` and no blocks yet → `search_and_scrape`.
4. Per-fetch cap `max_chars = 6000`. Results cached in SQLite for
   `settings.web_cache_ttl_hours`; every attempt is logged to `scraping_metrics`
   via `record_scraping(url, status, chars, latency)` (`status` ∈ cache_hit/success/failed/error).
5. **Extraction**: content is extracted to clean **markdown** via `trafilatura` when
   installed (denser signal per token, so the fast tier answers web queries well); falls
   back to BeautifulSoup text extraction when `trafilatura` is absent (no regression).
   Combined with step 4's cap, the injected web context comfortably fits the fast-tier
   window — which is why web intent no longer forces Gemini (§5 step 4).

### 8.2 Intent classification

`IntentClassifier.needs_web_search` evaluates in order: **substring match** against the
keyword store → **trivial/meta-query early-return** (`False`) → **embedding similarity**
(cosine vs positive/negative exemplars, model `bge-m3:latest`; the operative threshold is
`0.58`, set in `i18n/{en,ko}.json` and loaded by `initialize()` — the `0.65` hardcoded in
`IntentClassifier.__init__` is always overridden. Positive iff `max_pos ≥ threshold and avg_pos > avg_neg`).

### 8.3 Keyword store (incremental learning)

`KeywordStore` persists per-language lists to `data/web_keywords.{ko,en}.json`: entries are
length 2–60, case-insensitively de-duplicated, FIFO-capped at 200/lang. Keywords are added
**only** through the admin routes (§4.2); `scripts/validate_flow.py` auto-feeds false
negatives to `…/keywords/learn`. Do not hardcode keywords in source.

### 8.4 Injection invariant

When `web_text` is produced, the coordinator inserts a `system` message
(`name="web_context"`, wrapped in `--- REAL-TIME WEB CONTEXT START/END ---`) at
`messages.insert(-1, …)` and emits the **canonical** stdout log
`Web context injected: N chars into request.messages[-2] (session=…)`. This log is the
authoritative signal that enrichment was applied; tests assert it and it MUST NOT regress.

---

## 9. Session & Persistence

Owned by `SessionManager` (SQLite) and `SessionOrchestrator` (`gateway.sessions`).

- **Connections**: `_get_conn()` opens a **fresh connection per call** with
  `PRAGMA journal_mode=WAL`. Per-task connections (not a shared singleton) prevent
  `database is locked` under concurrent reads/writes.
- **Schema**: `sessions` (with `fork_point_message_id` for branching), `messages`, and
  `parts` (`type` ∈ `text`, `web_fetch`, `web_search`, `compaction`, `step_cost`, `retry`).
- **Compaction boundary**: `load_context` / `load_history` return only the messages **after
  the most recent `compaction` part** (loading `text`/`compaction` parts past that point).
  This truncates long histories while preserving the compacted summary as the new base.
- **MemoryOptimizer** (`services/memory_optimizer.py`): compaction is owned by `MemoryOptimizer`;
  `SessionManager.compact` delegates to it (injecting a connection factory + token estimator).
  Compaction V2 runs while over `session_compact_threshold`: **Phase A** selective pruning
  (old `web_fetch`→`[PRUNED]`, delete `retry`, protect the recent `session_recent_window`) →
  **Phase B** structured summary via `ContextCompressor` (LLMLingua-2), whose input is
  de-duplicated by `(role, text[:500])` to cut redundant cost → writes the `compaction` marker.
- **Continuity**: because context is keyed by `session_id` and not by provider, a session's
  history survives provider/agent/model switches (regression-guarded by
  `test_gateway_model_switch_continuity.py`).
- **cp949 safety**: `_get_project_id` runs git via subprocess with `encoding="utf-8"`.

---

## 10. Non-Functional Requirements

- **Async-first**: every I/O path (HTTP, DB, subprocess) is `async`. `time.sleep`,
  `requests`, and other blocking calls are prohibited in services/adapters
  (`asyncio.sleep`, `httpx.AsyncClient`, `asyncio.subprocess` instead). Single-node asyncio.
- **Security model**:
  - *SSRF*: `WebScraper` resolves the host and blocks any internal/reserved IP via property
    check (`is_private/loopback/link_local/reserved/multicast/unspecified` + IPv4-mapped
    normalization + CGNAT). **Redirect re-validation** (best-effort): the final URL after
    redirects is re-checked on both fetch paths (`StealthyFetcher.response.url`, Playwright
    `page.url`), and the Playwright path additionally aborts document/navigation requests that
    resolve to internal addresses via a `context.route` guard. **Residual**: DNS-rebinding
    within a single resolution window, and StealthyFetcher's pre-flight request before
    final-URL re-check, remain — full mitigation needs IP-pinning, which is infeasible with
    the browser-based fetchers in use.
  - *Admin auth*: `X-Admin-Key` vs `ADMIN_API_KEY`; **fail-closed (503)** when the key is
    unset unless `--debug`/`DEBUG=true`.
  - *Secret handling*: API keys are never logged or returned in full (truncated to `[:8]…`);
    client-facing 5xx responses must not leak internal exception detail.
- **Encoding**: UTF-8 is forced on log handlers and `sys.stdout` (`core/logging.py`) for
  Korean-Windows (cp949) safety.
- **Terms of Service**: multi-key rotation may be restricted by some providers' ToS;
  compliance is the user's responsibility (`DISCLAIMER.md`). The ToS notice must not be
  removed without user consent.

---

## 11. License

This project is licensed under the **MIT License**.
