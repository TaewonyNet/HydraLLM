# Project Specification: Context-Aware HydraLLM

- **Version:** 1.0.0
- **Runtime:** Python 3.10+ (FastAPI)
- **Architecture:** Clean Architecture (Domain → Service → Adapter → API)

---

## 1. 개요

OpenAI API 규격(`POST /v1/chat/completions`)을 완벽하게 구현하는 **고가용성 로컬 LLM 게이트웨이**다.

Gemini, Groq, Cerebras 등 무료 티어 API를 통합 관리하며, 요청 컨텍스트(이미지 유무, 토큰 길이)에 따라 최적 모델로 자동 라우팅하고, Multi-Key Random Rotation으로 Rate Limit(429)을 무력화한다.

**OpenClaw, Claude Code, Cursor, Continue 등 OpenAI 호환 클라이언트를 `base_url` 변경만으로 연결할 수 있다.**

### 핵심 목표

1. 무료 티어 API 키의 쿼터를 병렬 순환으로 극대화한다.
2. 컨텍스트 분석 기반으로 적합한 모델을 자동 선택한다.
3. DuckDB 세션 저장으로 에이전트 전환 시에도 대화 맥락을 유지한다.
4. 로컬 CLI 에이전트(Ollama, OpenCode, OpenClaw)를 동일한 엔드포인트로 통합한다.

---

## 2. 시스템 아키텍처

### 2.1 디렉토리 구조

```
src/
├── app.py                      # FastAPI 앱 팩토리 + lifespan (모델 디스커버리, 키 프로빙, 백그라운드 복구)
├── core/
│   ├── config.py               # Pydantic Settings — 환경 변수 중앙 관리
│   ├── exceptions.py           # 커스텀 예외 (ResourceExhaustedError, RateLimitError 등)
│   └── logging.py
├── domain/
│   ├── enums.py                # ProviderType, AgentType, ModelType, RoutingReason, RoutingStrategy
│   ├── models.py               # ChatRequest, ChatResponse, ChatMessage, RoutingDecision (Pydantic v2)
│   ├── schemas.py              # API 응답 DTO (ModelInfo, ModelListResponse, ProviderStatus 등)
│   └── interfaces.py           # ABC 정의 (ILLMProvider, IContextAnalyzer, IKeyManager, IRouter)
├── services/
│   ├── analyzer.py             # 컨텍스트 분석 → 라우팅 결정
│   ├── key_manager.py          # 키 풀, 랜덤 순환, 실패 격리, probe 기반 복구
│   ├── gateway.py              # 오케스트레이션, URL 자동 감지, 세션 압축, 재시도 루프, 폴백
│   ├── scraper.py              # Playwright 기반 웹 스크래핑 (URL fetch, DuckDuckGo 검색)
│   ├── compressor.py           # LLMLingua-2 기반 프롬프트/세션 압축
│   └── session_manager.py      # DuckDB 기반 세션 영구 저장 + 시스템 설정 영속화
├── adapters/
│   └── providers/
│       ├── gemini.py           # Google GenAI (포맷 변환, 멀티모달)
│       ├── openai_compat.py    # Groq, Cerebras, Ollama (openai SDK base_url 교체)
│       └── local_cli.py        # OpenCode, OpenClaw (subprocess)
└── api/
    └── v1/
        ├── endpoints.py        # 모든 라우트 정의 (chat, models, admin, responses, completions)
        └── dependencies.py     # app.state에서 Gateway/KeyManager 추출 (FastAPI DI)
tests/
├── conftest.py                 # 프로젝트 루트 탐색 및 sys.path 설정
├── api/
│   └── test_api.py
├── unit/
│   ├── test_analyzer.py
│   ├── test_key_manager.py
│   └── test_gemini_adapter.py
└── integration/
    └── test_integration.py
```

### 2.2 데이터 흐름

```
Client
  │  POST /v1/chat/completions
  ▼
API Layer (endpoints.py)
  │
  ▼
Gateway Service (gateway.py)
  │
  ├─► SessionManager  ──── DuckDB (load history by session_id, 중복 제거 병합)
  │
  ├─► Analyzer Service  →  RoutingDecision (provider, model_name, reason)
  │                         [루프 진입 전 1회만 실행]
  │
  └─► Retry Loop (max 3)
        │
        ├─► Key Manager  →  API Key (랜덤 순환)
        │
        ├─► Adapter (gemini / openai_compat / local_cli)
        │       │
        │       └─► External API / subprocess
        │
        ├── 성공 → Response → Session 저장 → Client
        │
        └── 실패
              │
              ├─► Key Manager: report_failure(key) → failed 풀로 이동
              └─► 해당 프로바이더 키 소진 시 다른 클라우드 프로바이더로 폴백
                   (ProviderType enum 순서: Gemini → Groq → Cerebras)
```

---

## 3. 핵심 모듈 명세

### 3.1 Domain Layer

#### `enums.py`

```python
class ProviderType(Enum):
    GEMINI   = "gemini"
    GROQ     = "groq"
    CEREBRAS = "cerebras"

class AgentType(Enum):
    OLLAMA   = "ollama"
    OPENCODE = "opencode"
    OPENCLAW = "openclaw"

class RoutingReason(Enum):
    TOKEN_COUNT      = "token_count"
    IMAGE_PRESENT    = "image_present"
    MODEL_HINT       = "model_hint"
    KEY_AVAILABILITY = "key_availability"
    RATE_LIMIT       = "rate_limit"
```

> `ModelType` enum에 30+ 모델 변형이 정의되어 있다 (Gemini 1.5~3.1, Groq Llama/DeepSeek, Cerebras 등).
> 티어는 별도 enum 없이 문자열(`"free"`, `"premium"`, `"standard"`, `"unknown"`)로 관리된다.

#### `models.py`

```python
class ChatMessage(BaseModel):
    role: str
    content: str | dict[str, Any] | list[Any]

class ChatRequest(BaseModel):
    model: str | None
    messages: list[ChatMessage] | None
    prompt: str | None           # 레거시 Completion 호환
    session_id: str | None       # DuckDB 세션 연동
    has_search: bool | None
    stream: bool | None
    web_fetch: str | None        # 명시적 URL fetch
    auto_web_fetch: bool | None  # URL 자동 감지 (None=서버 기본값)
    compress_context: bool | None # LLMLingua-2 압축 (None=서버 기본값)
    # estimate_token_count(), has_images() 메서드 제공

class ChatResponse(BaseModel):
    id: str
    model: str
    choices: list[ChatMessageChoice]
    usage: dict[str, Any] | None

class RoutingDecision(BaseModel):
    provider: ProviderType | None
    agent: AgentType | None
    model_name: str
    reason: str
    confidence: float | None
```

#### `interfaces.py`

```python
class ILLMProvider(ABC):
    async def generate(self, request: ChatRequest, api_key: str) -> ChatResponse
    def get_supported_models(self) -> list[ModelType]
    def is_multimodal(self) -> bool
    def get_max_tokens(self) -> int
    async def discover_models(self) -> list[dict[str, Any]]
    async def probe_key(self, api_key: str) -> dict[str, Any]

class IContextAnalyzer(ABC):
    async def analyze(self, request: ChatRequest, available_tiers: dict | None) -> RoutingDecision
    def get_supported_models_info(self) -> list[dict[str, Any]]
    def get_all_discovered_models_info(self) -> list[dict[str, Any]]
    def register_model(self, model_name: str, provider: ProviderType | Any, metadata: dict | None) -> None

class IKeyManager(ABC):
    async def get_next_key(self, provider: ProviderType, min_tier: str = "free") -> str
    async def report_success(self, provider: ProviderType, api_key: str) -> None
    async def report_failure(self, provider: ProviderType, api_key: str, error: Exception) -> None
    def get_key_status(self) -> dict[ProviderType, dict[str, Any]]

class IRouter(ABC):
    async def route_request(self, request: ChatRequest) -> ChatResponse
    async def get_status(self) -> dict[str, Any]
    def get_supported_models(self) -> list[dict[str, Any]]
    def get_all_models(self) -> list[dict[str, Any]]
```

---

### 3.2 Service Layer

#### `analyzer.py` — Context Analyzer

**역할**: 요청 특성을 분석하여 라우팅 전략을 결정한다.

**라우팅 로직** (우선순위 순서):

1. **특정 모델 지정**: `_model_mapping`에 매칭되면 해당 모델의 프로바이더로 직접 라우팅
   - `gpt-4`, `gpt-4o` → `GEMINI` (gemini-1.5-pro로 매핑)
   - `gpt-3.5-turbo`, `gpt-4o-mini` → `GEMINI` (gemini-1.5-flash로 매핑)
2. **프로바이더 지정**: `gemini/auto`, `groq/auto` 등 → 해당 프로바이더 내 자동 선택
3. **이미지 확인**: `messages` 내 `image_url` 존재 → `GEMINI` (멀티모달)
4. **토큰 수 기반 2단계 라우팅** (`settings.MAX_TOKENS_FAST_MODEL` 기준):
   - 토큰 < `MAX_TOKENS_FAST_MODEL` (8192) → `GROQ` (Llama-3.3-70b, 빠른 추론)
   - 토큰 >= `MAX_TOKENS_FAST_MODEL` → `GEMINI` (장문 컨텍스트)
   - Cerebras는 GROQ와 동일한 context limit(8192)이므로 fallback 용도로만 사용

> ⚠️ **중요**: 임계값은 반드시 `settings.MAX_TOKENS_FAST_MODEL`에서 읽어야 한다. 코드에 직접 수치를 하드코딩하지 않는다.

**출력**: `RoutingDecision(provider, agent, model_name, reason, confidence)`

---

#### `key_manager.py` — Key Rotation

**역할**: API 키의 생명주기 관리 및 부하 분산.

**데이터 구조**:
```python
# 키를 상태별 분리 풀로 관리 (KeyEntity 등 별도 dataclass 없음)
_key_pools: dict[ProviderType, list[str]]          # 전체 키
_active_keys: dict[ProviderType, list[str]]         # 사용 가능한 키
_failed_keys: dict[ProviderType, list[str]]         # 실패한 키
_key_usage: dict[ProviderType, dict[str, int]]      # 키별 사용 횟수
_key_metadata: dict[ProviderType, dict[str, dict]]  # 키별 메타데이터 (tier, last_probed 등)
```

**알고리즘**:
- **랜덤 순환**: Active 풀에서 `random.choice()`로 반환 (균등 분산 목적).
- **실패 격리**: API 에러 시 `report_failure()`로 failed 풀로 이동, active 풀에서 제거.
- **Probe 기반 복구**: `app.py`의 백그라운드 태스크가 60초 간격으로 failed 키를 실제 API 호출로 재검증(`probe_key`). 성공 시 active 풀로 복구.
- **티어 자동 감지**: `probe_key()` 결과로 `"free"` / `"premium"` 티어를 자동 분류. 코드에 하드코딩 금지.
- **모든 키 소진 시**: `ResourceExhaustedError` 발생 → gateway가 다른 클라우드 프로바이더로 폴백.

---

#### `gateway.py` — Gateway Orchestrator

**역할**: Analyzer, KeyManager, Adapter, SessionManager를 조율하고 재시도 로직을 수행한다.

```python
async def process_request(request: ChatRequest) -> ChatResponse:
    # 1. 세션 기록 로드 (session_id 있을 경우, 중복 메시지 제거 병합)
    if request.session_id:
        history = session_manager.get_history(request.session_id)
        request.messages = merge_messages(history, request.messages)

    # 2. 세션 히스토리 압축 (enable_context_compression=True + 메시지 4개 초과 + 토큰 초과)
    #    LLMLingua-2로 오래된 메시지를 압축하여 GPT처럼 긴 세션 유지
    if do_compress and len(messages) > 4 and estimated_tokens > MAX_TOKENS_FAST_MODEL:
        messages = _compress_session_history(messages)

    # 3. URL 자동 감지 (enable_auto_web_fetch=True)
    #    프롬프트에 https:// URL이 있으면 scraper로 자동 fetch → system 메시지로 주입
    urls = re.findall(r"https?://...", content_text)
    for url in urls:
        content = await scraper.scrape_url(url)
        if do_compress:
            content = compressor.compress(content)
        # system 메시지로 맨 앞에 삽입

    # 4. 라우팅 결정 — 루프 진입 전 1회만 실행
    decision = await analyzer.analyze(request, available_tiers)

    # 5. 재시도 루프 (키만 교체)
    for attempt in range(max_retries):
        try:
            key = await key_manager.get_next_key(decision.provider, min_tier)
            response = await adapter.generate(request, key)
            return response
        except ResourceExhaustedError:
            # 다른 클라우드 프로바이더로 폴백
        except Exception:
            # context_length_exceeded → 키 실패 처리 안 함, 즉시 폴백
            # 그 외 → report_failure로 키 격리
```

**폴백 체인**: 클라우드 프로바이더 간 순차 전환 (`ProviderType` enum 정의 순서). 로컬 에이전트(Ollama 등)는 별도 경로(`_process_with_agent`)로 처리되며 자동 폴백 대상이 아님.

---

#### `session_manager.py` — Session Store

**역할**: DuckDB를 활용한 대화 기록 영구 저장 및 시스템 설정 관리.

```python
class SessionManager:
    """
    테이블: sessions(session_id TEXT, messages JSON, summary TEXT, updated_at TIMESTAMP)
    테이블: system_settings(key TEXT, value JSON)

    동기 메서드로 구현 (async 아님).
    """
    def get_history(self, session_id: str) -> list[ChatMessage]: ...
    def save_message(self, session_id: str, role: str, content: str) -> None: ...
    def clear_session(self, session_id: str) -> None: ...
    def get_all_sessions(self) -> list[dict]: ...
    def get_setting(self, key: str, default: Any = None) -> Any: ...
    def set_setting(self, key: str, value: Any) -> None: ...
```

---

### 3.3 Adapter Layer

#### `providers/openai_compat.py` — Groq, Cerebras, Ollama

- `openai.AsyncOpenAI(base_url=..., api_key=...)` 로 인스턴스화.
- Groq: `base_url="https://api.groq.com/openai/v1"`
- Cerebras: `base_url="https://api.cerebras.ai/v1"`
- Ollama: `base_url="http://localhost:11434/v1"` (HTTP 호출, subprocess 아님)

#### `providers/gemini.py` — Google Gemini

- OpenAI 포맷 → Google `contents` 포맷 변환 (시스템 프롬프트 분리 필수).
- Google `GenerateContentResponse` → OpenAI `ChatCompletion` 포맷 매핑.
- 멀티모달: base64 이미지 처리 포함.

#### `providers/local_cli.py` — 로컬 CLI 에이전트

- `OpenCode`, `OpenClaw`를 `subprocess`로 실행.
- 각 CLI 도구를 영구 서버 없이 호출하여 리소스를 절약.
- 표준 입출력(stdin/stdout)으로 OpenAI 호환 요청/응답을 교환.

> 참고: Ollama는 `local_cli.py`가 아닌 `openai_compat.py`를 통해 HTTP로 연결된다.

---

## 4. API 명세

### `POST /v1/chat/completions`

OpenAI Chat Completion API와 동일한 시그니처. 추가 파라미터:

```json
{
  "model": "auto",
  "messages": [{"role": "user", "content": "Hello!"}],
  "session_id": "optional-session-uuid",
  "has_search": false
}
```

응답의 `model` 필드에는 실제 처리한 모델명이 반환된다.

### `POST /v1/responses` (별칭)

OpenClaw 등 `/v1/responses` 엔드포인트를 사용하는 클라이언트를 위한 별칭.
- `input` 필드 → `messages` 자동 변환 (OpenAI Responses API 호환)
- `prompt` 필드 → `messages` 자동 변환 (레거시 Completion 호환)
- `max_output_tokens` → `max_tokens` 자동 매핑

### `POST /v1/completions` (별칭)

내부적으로 `/v1/responses`와 동일한 로직으로 처리된다.

### `GET /v1/models`

가용 모델 전체 목록 반환. 각 모델에 기능 플래그(`has_search`, `multimodal`) 포함.

### `GET /v1/admin/status`

```json
{
  "status": { "status": "healthy", "providers": {...}, "agents": {...} },
  "key_statistics": {
    "gemini": {"total": 3, "active": 2, "failed": 1, "keys": [...]},
    "groq":   {"total": 2, "active": 2, "failed": 0, "keys": [...]}
  }
}
```

### `GET /v1/admin/onboarding` / `POST /v1/admin/onboarding`

온보딩 상태 조회 및 완료. 모델 선택 설정을 DuckDB에 영속화.

### `GET /v1/admin/sessions` / `DELETE /v1/admin/sessions/{session_id}`

세션 목록 조회 및 삭제.

### `POST /v1/admin/keys`

런타임에 키를 추가/갱신한다.

```json
{"provider": "gemini", "keys": ["new_key_1", "new_key_2"]}
```

### `POST /v1/admin/probe`

모든 키를 즉시 재검증하고 Self-Healing을 트리거한다.

### `GET /ui`

내장 웹 대시보드. 마크다운 렌더링, 코드 하이라이팅, 세션 내보내기/가져오기 지원.

---

## 5. 테스트 전략

### 5.1 단위 테스트

**`test_analyzer.py`**
- 이미지 포함 요청 → `GEMINI` 반환
- 토큰 수 기반 2단계 라우팅 (GROQ / GEMINI). Cerebras는 동일 context limit으로 fallback 전용
- `model="gpt-4"` 요청 → GEMINI (gemini-1.5-pro로 매핑)

**`test_key_manager.py`**
- Active 키 중 랜덤 반환 (편향 없음 검증)
- `report_failure` 호출 시 즉시 active 풀에서 제거
- 모든 키 실패 시 `ResourceExhaustedError` 발생

**`test_gemini_adapter.py`**
- Gemini 어댑터 포맷 변환 검증

### 5.2 통합 테스트

**`test_integration.py`**
- 전체 요청 흐름 통합 테스트

### 5.3 API 테스트

**`test_api.py`**
- FastAPI 엔드포인트 통합 테스트

---

## 6. 개발 로드맵

| Phase | 내용 |
|---|---|
| Phase 1 | Domain 정의, key_manager, analyzer 구현 및 단위 테스트 |
| Phase 2 | openai_compat, gemini 어댑터 구현 및 실제 API 호출 테스트 |
| Phase 3 | FastAPI 앱 구성, gateway 서비스 연결, 엔드포인트 노출 |
| Phase 4 | session_manager.py(DuckDB), local_cli.py(subprocess) 구현 |
| Phase 5 | Web UI(/ui), Self-Healing 백그라운드 태스크, Dockerfile |

---

## 7. 설정

`.env` 파일로 관리. `src/core/config.py`의 `Settings(BaseSettings)`에서 로드.

```env
# 서버
PORT=8000
LOG_LEVEL=INFO

# 라우팅 임계값 — analyzer.py에서 이 값을 읽어 2단계 라우팅에 사용
# 토큰 < 이 값: GROQ, 이상: GEMINI (Cerebras도 8192 한도이므로 fallback 전용)
MAX_TOKENS_FAST_MODEL=8192

# Feature Flags
ENABLE_AUTO_WEB_FETCH=true       # 프롬프트 내 URL 자동 감지 및 fetch
ENABLE_CONTEXT_COMPRESSION=true  # LLMLingua-2로 세션 히스토리/웹 콘텐츠 압축

# API 키 (쉼표 구분, 여러 개 등록 가능)
GEMINI_KEYS=key1,key2,key3
GROQ_KEYS=gsk_1,gsk_2
CEREBRAS_KEYS=csk_1

# 기본 모델
DEFAULT_FREE_MODEL=gemini-flash-latest
DEFAULT_PREMIUM_MODEL=gemini-pro-latest

# 로컬 에이전트
OLLAMA_BASE_URL=http://localhost:11434/v1
OPENCODE_BASE_URL=http://localhost:8080/v1
OPENCLAW_BASE_URL=http://localhost:9000/v1

# 재시도
MAX_RETRIES=3
```

---

## 8. 클라이언트 연결 가이드

### OpenClaw

```
Settings → API Provider → Custom
API Base URL: http://localhost:8000/v1
API Key: any (게이트웨이가 관리)
```

### Claude Code / Cursor / Continue

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1
export OPENAI_API_KEY=any
```

### Python openai SDK

```python
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key="any",
    base_url="http://localhost:8000/v1"
)

response = await client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Hello!"}],
    extra_body={"session_id": "my-session-id"}
)
```
