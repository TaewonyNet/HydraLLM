# 🚀 HydraLLM: Smart Key Rotation & Context Routing

**HydraLLM** is a high-performance, unified LLM API gateway that consolidates multiple providers (Gemini, Groq, Cerebras) into a single OpenAI-compatible interface. It features **intelligent key rotation**, **tier-aware routing**, and **local session persistence**, making it the ultimate tool for maximizing free tier quotas and building resilient AI applications.

---

## ✨ Key Features

- **Intelligent Free Tier Optimization**: 
  - Manage multiple free API keys to distribute load and maximize quotas.
  - **Auto-Downgrading**: Real-time detection of `limit: 0` or quota errors to automatically isolate and re-tier keys.
  - **Random Rotation**: Distributes load across your key pool to minimize rate limiting hits.
  - **Self-Healing**: Background task re-probes failed keys every minute to restore service.
- **Context-Aware Smart Routing**:
  - Automatically selects the best model based on token count, multimodal needs, or search requirements.
  - **Tier-Aware Logic**: Intelligently falls back to high-capacity free models (like Gemini Flash) if no premium keys are available.
  - **Thresholds**: Optimized for speed (Groq < 8192 tokens) and depth (Gemini >= 8192 tokens). Cerebras shares the same context limit as Groq and serves as a fallback provider.
- **Strict OpenAI & OpenClaw Compatibility**:
  - Standard endpoints: `/v1/chat/completions`, `/v1/models`, and legacy `/v1/completions`.
  - **OpenClaw Support**: Includes `/v1/responses` alias and automatic mapping for `input`, `max_output_tokens`, and `prompt` fields.
  - **Strict Streaming (SSE)**: Standard-compliant Server-Sent Events for real-time responses.
- **Automatic URL Detection & Web Fetch**:
  - Detects URLs in prompts and automatically fetches web content using **Playwright** (enabled by default, toggle via `auto_web_fetch`).
  - Supports 3 scrape modes: `standard` (clean markdown), `simple` (raw text), and `network_only` (fastest).
  - Integrated DuckDuckGo search via `has_search: true`.
- **LLMLingua-2 Session Compression**:
  - Compresses long conversation history using **LLMLingua-2** to maintain GPT-like sessions within context limits.
  - Older messages are intelligently compressed while keeping recent context intact.
  - Both features default ON, can be toggled per-request (`auto_web_fetch`, `compress_context`) or globally via config.
- **Persistent Local Sessions**:
  - Powered by **DuckDB**, the gateway maintains full conversation history locally.
  - **Cross-Agent Continuity**: Seamlessly switch between different providers (e.g., start with Gemini, move to Groq) while maintaining context.
- **Dynamic Model Discovery**:
  - Automatically fetches the latest model lists from providers and local agents on startup.
  - Support for **virtual models** like `auto`, `gemini/auto`, `groq/auto` for provider-specific intelligent routing.
- **Advanced Debugging Web UI**:
  - Built-in dashboard at `/ui` with Markdown rendering, syntax highlighting, and an interactive onboarding wizard.
  - **Raw API Inspector**: Request/Response tab showing actual gateway input (before/after processing) and raw API output.
  - Toggle controls for Auto Web Fetch, Context Compression, and Web Search.
- **Local Agent Integration**:
  - Wraps **Ollama**, **OpenCode**, and **OpenClaw** CLI engines into the unified API via direct subprocess invocation.

---

## 🛠 Quick Start

### 1. Installation

```bash
git clone https://github.com/TaewonyNet/agent-playground.git
cd free_agent
pip install -e .
```

### 2. Configuration (`.env`)

```env
PORT=8000
LOG_LEVEL=INFO

# Provider Keys (Comma separated)
GEMINI_KEYS=key1,key2
GROQ_KEYS=gsk_1,gsk_2

# Optional Tier Overrides
FREE_MODELS=flash,lite,8b
PREMIUM_MODELS=pro,ultra,70b

# Default Models for 'auto' routing
DEFAULT_FREE_MODEL=gemini-flash-latest
DEFAULT_PREMIUM_MODEL=gemini-pro-latest

# Feature Flags (all default True)
ENABLE_AUTO_WEB_FETCH=true
ENABLE_CONTEXT_COMPRESSION=true
```

### 3. Run

```bash
python main.py
```

---

## 📡 API Highlights

- `POST /v1/chat/completions`: Standard Chat API. Use `model="auto"` for smart routing.
- `POST /v1/responses`: Enhanced alias for OpenClaw and legacy clients.
- `GET /v1/models`: List available models with capabilities (🌐 Search, 🖼️ Multimodal) and tiers.
- `GET /v1/admin/status`: Real-time health monitoring of providers and individual keys.
- `GET /v1/admin/sessions`: List and manage local persistent sessions.

---

## 🏗 Architecture

The project follows **Clean Architecture** principles:
- **Domain**: core schemas and interfaces (`src/domain`).
- **Services**: Routing, Key Management, and Session Management logic (`src/services`).
- **Adapters**: Connectors for cloud APIs and local CLI tools (`src/adapters`).
- **API**: FastAPI endpoints and dependency management (`src/api`).

---

## 📄 License

This project is licensed under the **MIT License**.
