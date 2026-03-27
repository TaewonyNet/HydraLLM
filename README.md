[English](README.md) | [한국어](README.ko.md)

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
  - **Key-Aware Logic**: Intelligently falls back to high-capacity free models (like Gemini Flash) if no premium keys are available.
  - **Thresholds**: Optimized for speed (Groq < 1,500 tokens), balance (Cerebras < 5,000 tokens), and depth (Gemini > 5,000 tokens).
- **Advanced Custom Web Scraper & Search**:
  - Integrated **Scrapling** and **Playwright** based scraping for free tier users to bypass native search limits.
  - Features stealthy fetching with **browserforge** headers to minimize anti-bot detection.
  - Supports 3 modes: `standard` (clean structured text), `simple` (raw text), and `network_only` (ultra-fast).
  - Automatically fetches external content via the `web_fetch` field or URL auto-detection in prompts.
- **LLMLingua-2 Prompt Compression**:
  - (Optional) Compresses long conversation history or web content using **LLMLingua-2** to stay within context limits.
- **Persistent Local Sessions**:
  - Powered by **DuckDB**, the gateway maintains full conversation history locally for cross-agent continuity.
- **Strict OpenAI & OpenClaw Compatibility**:
  - Standard endpoints: `/v1/chat/completions`, `/v1/models`, and legacy `/v1/completions`.
  - **OpenClaw Support**: Includes `/v1/responses` alias and automatic mapping for `input`, `max_output_tokens`, and `prompt` fields.
  - **Strict Streaming (SSE)**: Standard-compliant real-time responses.
- **Local Agent Integration**:
  - Wraps **Ollama**, **OpenCode**, and **OpenClaw** CLI engines via direct subprocess invocation.

---

## 🕹️ Interactive Playground (Easy Access)

The easiest way to test HydraLLM is through the built-in Web UI.
- **URL**: `http://localhost:8000/ui`
- **Features**: Chat directly with your pooled models, monitor key status, and manage sessions without any coding.

---

## 🛠 Quick Start

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/TaewonyNet/HydraLLM.git
cd HydraLLM

# Install package and dependencies
pip install -e .

# Install Playwright browser binaries
playwright install chromium
```

### 2. Configuration (`.env`)

```env
PORT=8000
LOG_LEVEL=INFO
DEBUG=false

# Provider Keys (Comma separated)
GEMINI_KEYS=key1,key2
GROQ_KEYS=gsk_1,gsk_2
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

Built with **Clean Architecture** for maximum extensibility.
- **Domain**: core schemas and interfaces (`src/domain`).
- **Services**: Routing, Key Management, Session Persistence, and Scraping logic (`src/services`).
- **Adapters**: Connectors for cloud APIs and local CLI tools (`src/adapters`).
- **API**: FastAPI endpoints and dependency management (`src/api`).

---

## 📄 License

This project is licensed under the **MIT License**.
