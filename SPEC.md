[English](SPEC.md) | [한국어](SPEC.ko.md)

# Project Specification: HydraLLM (Context-Aware Multi-LLM Gateway)

- **Version:** 1.0.0
- **Runtime:** Python 3.10+ (FastAPI)
- **Architecture:** Clean Architecture (Domain → Service → Adapter → API)

---

## 1. Overview

**HydraLLM** is a high-availability local LLM gateway that implements the OpenAI API specification (`POST /v1/chat/completions`).

It integrates and manages free tier APIs from Gemini, Groq, and Cerebras. It automatically routes requests to the optimal model based on request context (image presence, token length) and defeats rate limits (429) using multi-key random rotation.

**OpenAI-compatible clients such as OpenClaw, Claude Code, Cursor, and Continue can be connected by simply changing the `base_url`.**

### Core Goals

1. Maximize free tier API key quotas through parallel rotation.
2. Automatically select suitable models based on context analysis.
3. Maintain conversation context across agent transitions using DuckDB session storage.
4. Integrate local CLI agents (Ollama, OpenCode, OpenClaw) into the same endpoint.

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
├── domain/
│   ├── enums.py                # ProviderType, AgentType, ModelType, RoutingReason
│   ├── models.py               # ChatRequest, ChatResponse, ChatMessage, RoutingDecision (Pydantic v2)
│   ├── schemas.py              # API Response DTOs (ModelInfo, ModelListResponse, ProviderStatus)
│   └── interfaces.py           # ABC Definitions (ILLMProvider, IContextAnalyzer, IKeyManager, IRouter)
├── services/
│   ├── analyzer.py             # Context analysis → Routing decision
│   ├── key_manager.py          # Key pools, random rotation, isolation, probe-based recovery
│   ├── gateway.py              # Orchestration, URL auto-detection, session management, retry loop
│   ├── scraper.py              # Playwright-based web scraping (URL fetch, search)
│   ├── compressor.py           # LLMLingua-2 based prompt/session compression
│   └── session_manager.py      # DuckDB-based persistent session storage + settings persistence
├── adapters/
│   └── providers/
│       ├── gemini.py           # Google GenAI (format conversion, multimodal)
│       ├── openai_compat.py    # Groq, Cerebras, Ollama (standard OpenAI SDK wrapper)
│       └── local_cli.py        # OpenCode, OpenClaw (subprocess-based integration)
└── api/
    └── v1/
        ├── endpoints.py        # Route definitions (chat, models, admin, responses)
        └── dependencies.py     # FastAPI Dependency Injection (Gateway/KeyManager from app.state)
tests/
├── conftest.py                 # Project root discovery and sys.path setup
├── api/                        # API endpoint tests
├── unit/                       # Component logic tests
└── integration/                # Full request flow tests
```

### 2.2 Data Flow

```
Client
  │  POST /v1/chat/completions (or /v1/responses)
  ▼
API Layer (endpoints.py)
  │
  ▼
Gateway Service (gateway.py)
  │
  ├─► SessionManager  ──── DuckDB (load history, deduplicate, merge)
  │
  ├─► URL Auto-detection ─► WebScraper (scrapes URLs found in user prompt)
  │
  ├─► Analyzer Service  ──► RoutingDecision (provider, model_name, reason)
  │
  └─► Retry Loop (max 3)
        │
        ├─► Key Manager  ──► API Key (Randomly selected from active pool)
        │
        ├─► Adapter (Gemini / OpenAI Compat / Local CLI)
        │       │
        │       └─► External API Call / Subprocess Execution
        │
        ├── Success ──► Response ──► Save to Session ──► Client
        │
        └── Failure
              │
              ├─► Key Manager: report_failure(key) ──► Move to failed pool
              └─► Provider Fallback: Switch to another provider if keys exhausted
```

---

## 3. Core Module Specifications

### 3.1 Domain Layer

#### `enums.py`

- `ProviderType`: gemini, groq, cerebras.
- `AgentType`: ollama, opencode, openclaw.
- `RoutingReason`: token_count, image_present, model_hint, search_required.

> Tiers are managed as strings: `"free"`, `"standard"`, `"premium"`, `"experimental"`, `"unknown"`.

#### `models.py`

- `ChatRequest`: Extends OpenAI standard with `session_id`, `has_search`, `web_fetch`, `compress_context`.
- `ChatResponse`: OpenAI compatible structure.
- `RoutingDecision`: Internal model for analyzer results.

#### `interfaces.py`

Defines the contracts for all providers and services.
- `ILLMProvider`: `generate`, `discover_models`, `probe_key`.
- `IKeyManager`: `get_next_key`, `report_success`, `report_failure`.
- `IRouter`: `route_request`, `get_status`.

---

## 4. API Specification

### `POST /v1/chat/completions`

OpenAI Chat Completion API.
- Support `model="auto"` for intelligent routing.
- Support `session_id` for local persistence.

### `POST /v1/responses` (OpenClaw Alias)

Dedicated alias for OpenClaw's `openai-responses` mode.
- Automatic mapping of `input` → `messages`.
- Immediate `response.created` event to prevent timeouts.
- Strict SSE standard compliance.

### `GET /v1/models`

Dynamic list of all discovered models.
- Includes `has_search` and `multimodal` capabilities.
- Includes virtual `auto` models for each provider.

---

## 5. Routing Strategy

**Context Analyzer** determines the model based on:

1. **Explicit Model Hint**: If the model string is recognized (e.g. "gpt-4o"), it maps to a pre-defined high-quality equivalent.
2. **Provider Auto**: e.g., `GEMINI/auto` routes within Gemini using current key tiers.
3. **Multimodal**: Priortizes Gemini Vision if images are detected.
4. **Token Count**:
   - < 1,500: Groq (Llama 3.3 70B) for speed.
   - < 5,000: Cerebras (Llama 3.1 70B) for high-speed fallback.
   - \> 5,000: Gemini for high context window.
5. **Tier Awareness**: Pro models are only selected if `premium` keys are available.

---

## 6. Key Management

- **Random Rotation**: Uniform distribution of load across the key pool.
- **Auto-Downgrading**: If a key returns `limit: 0` (Gemini free tier restriction), it's immediately marked as `free` in metadata.
- **Self-Healing**: A background task re-probes failed keys every 60 seconds.

---

## 7. License

This project is licensed under the **MIT License**.
