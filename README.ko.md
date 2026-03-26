# 🚀 Multi-LLM Gateway: 지능형 키 순환 및 컨텍스트 라우팅 시스템

**Multi-LLM Gateway**는 여러 LLM 제공자(Gemini, Groq, Cerebras)를 하나로 통합하여 표준화된 OpenAI 호환 인터페이스를 제공하는 고성능 API 게이트웨이입니다. **지능형 키 순환**, **티어 기반 라우팅**, **로컬 세션 영구 저장** 기능을 통해 무료 티어 쿼터를 극대화하고 안정적인 AI 서비스를 구축할 수 있도록 돕습니다.

---

## ✨ 핵심 기능

- **지능형 무료 티어 최적화**: 
  - 여러 개의 무료 API 키를 등록하여 부하를 효율적으로 분산하고 쿼터를 극대화합니다.
  - **자동 강등 (Auto-Downgrading)**: 유료 모델 호출 시 권한 부족(`limit: 0`) 에러가 발생하면 실시간으로 해당 키를 격리하고 티어를 재분류합니다.
  - **무작위 순환 (Random Rotation)**: 키 풀 내에서 랜덤하게 키를 선택하여 특정 계정의 쿼터 소진을 최소화합니다.
  - **자가 치유 (Self-Healing)**: 실패한 키를 백그라운드 태스크가 매분 재검증하여 자동으로 복구합니다.
- **고급 커스텀 웹 스크래퍼 및 검색**:
  - **Playwright** 기반의 자체 스크래핑 엔진을 통해 무료 티어 사용자도 실시간 웹 정보를 활용할 수 있습니다.
  - 3가지 모드 지원: `standard` (정제된 마크다운), `simple` (raw 텍스트 추출), `network_only` (최속 모드, CSS/JS 차단).
  - 요청 시 `web_fetch` 필드를 통해 외부 URL 데이터를 자동으로 가져와 컨텍스트에 포함합니다.
- **Context-Aware 스마트 라우팅**:
  - 입력 토큰 수, 이미지 포함 여부(멀티모달), 검색 필요성 등을 분석하여 최적의 모델을 자동 선택합니다.
  - **키 권한 인지 로직**: 유료 키가 없는 경우 고용량 무료 모델(Gemini Flash 등)로 자동 폴백하여 서비스 중단을 방지합니다.
  - **임계값 최적화**: 속도 중심(Groq < 1.5k), 균형 중심(Cerebras < 5k), 용량 중심(Gemini > 5k)으로 라우팅됩니다.
- **로컬 세션 영구 저장 (Stateless but Contextual)**:
  - **DuckDB**를 활용하여 대화 기록을 서버 로컬에 안전하게 저장합니다.
  - **에이전트 간 연속성**: Gemini에서 시작한 대화를 Groq나 로컬 에이전트로 변경하더라도 전체 대화 맥락이 완벽하게 유지됩니다.
- **완벽한 OpenAI 및 OpenClaw 호환성**:
  - `/v1/chat/completions`, `/v1/models`, `/v1/completions` 등 표준 엔드포인트를 완벽히 지원합니다.
  - **OpenClaw 특화 지원**: `/v1/responses` 별칭 경로와 `input`, `max_output_tokens`, `prompt` 필드 자동 매핑을 지원합니다.
  - **표준 스트리밍 (SSE)**: OpenAI 서버 전송 이벤트 표준을 준수하여 실시간 응답을 제공합니다.
- **동적 모델 발견 및 가상 모델**:
  - 서버 시작 시 제공자 및 로컬 에이전트로부터 최신 모델 목록을 자동으로 가져옵니다.
  - `auto`, `gemini/auto`, `groq/auto` 등 특정 제공자에 특화된 지능형 가상 모델을 지원합니다.
- **고급 디버깅 웹 UI**:
  - 마크다운 렌더링, 코드 문법 강조, 대화형 온보딩 마법사가 포함된 대시보드(`/ui`)를 제공합니다.
- **로컬 에이전트 통합**:
  - **Ollama**, **OpenCode**, **OpenClaw** 등 로컬 CLI 엔진을 `subprocess` 방식으로 직접 호출하여 API로 통합합니다.

---

## 🛠 시작하기

### 1. 설치

```bash
git clone https://github.com/TaewonyNet/agent-playground.git
cd free_agent
pip install -e .
```

### 2. 환경 설정 (`.env`)

```env
PORT=8000
LOG_LEVEL=INFO

# 제공자별 API 키 (쉼표로 구분)
GEMINI_KEYS=key1,key2
GROQ_KEYS=gsk_1,gsk_2

# 기본 모델 설정
DEFAULT_FREE_MODEL=gemini-flash-latest
DEFAULT_PREMIUM_MODEL=gemini-pro-latest
```

### 3. 서버 실행

```bash
python main.py
```

---

## 📡 주요 API 엔드포인트

- `POST /v1/chat/completions`: 표준 채팅 API. `model="auto"` 시 스마트 라우팅 작동.
- `POST /v1/responses`: OpenClaw 및 레거시 클라이언트를 위한 호환 엔드포인트.
- `GET /v1/models`: 가용한 전체 모델 목록 및 상세 기능(🌐 검색, 🖼️ 멀티모달), 티어 정보 조회.
- `GET /v1/admin/status`: 제공자 건강 상태 및 개별 API 키 티어 실시간 모니터링.
- `GET /v1/admin/sessions`: 로컬에 저장된 영구 세션 목록 관리.

---

## 🏗 아키텍처

확장성을 극대화하기 위해 **Clean Architecture** 원칙을 준수합니다.
- **Domain**: 핵심 데이터 모델 및 인터페이스 정의 (`src/domain`).
- **Services**: 라우팅 로직, 키 관리, 세션 관리 등 핵심 비즈니스 로직 (`src/services`).
- **Adapters**: 클라우드 API 및 로컬 CLI 엔진용 연결 어댑터 (`src/adapters`).
- **API**: FastAPI 기반의 엔드포인트 및 의존성 주입 (`src/api`).

---

## 📄 라이선스

본 프로젝트는 **MIT License**에 따라 배포됩니다.
