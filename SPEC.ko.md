[English](SPEC.md) | [한국어](SPEC.ko.md)

# 프로젝트 명세서: HydraLLM (컨텍스트 인지형 멀티 LLM 게이트웨이)

- **버전:** 1.0.0
- **런타임:** Python 3.10+ (FastAPI)
- **아키텍처:** 클린 아키텍처 (Domain → Service → Adapter → API)

---

## 1. 개요

**HydraLLM**은 OpenAI API 규격(`POST /v1/chat/completions`)을 구현하는 고가용성 로컬 LLM 게이트웨이입니다.

Gemini, Groq, Cerebras 등 다양한 무료 티어 API를 통합 관리하며, 요청 컨텍스트(이미지 유무, 토큰 길이)에 따라 최적의 모델로 자동 라우팅합니다. 또한 Multi-Key Random Rotation 기법을 사용하여 레이트 리밋(429)을 효과적으로 회피합니다.

**OpenClaw, Claude Code, Cursor, Continue와 같은 OpenAI 호환 클라이언트를 `base_url` 변경만으로 즉시 연결할 수 있습니다.**

### 핵심 목표

1. 병렬 키 순환을 통해 무료 티어 API 키의 할당량(Quota)을 극대화한다.
2. 컨텍스트 분석을 기반으로 최적의 모델을 자동 선택한다.
3. DuckDB 세션 저장소를 통해 에이전트 전환 시에도 대화 맥락을 유지한다.
4. 로컬 CLI 에이전트(Ollama, OpenCode, OpenClaw)를 단일 엔드포인트로 통합한다.

---

## 2. 시스템 아키텍처

### 2.1 디렉토리 구조

```
src/
├── app.py                      # FastAPI 앱 팩토리 + lifespan (모델 발견, 프로빙, 복구)
├── core/
│   ├── config.py               # Pydantic Settings — 환경 변수 및 설정 중앙 관리
│   ├── exceptions.py           # 커스텀 예외 정의
│   └── logging.py              # 로컬 파일 및 콘솔 로깅 설정
├── domain/
│   ├── enums.py                # 제공자, 에이전트, 모델 타입 등 열거형
│   ├── models.py               # Pydantic 기반 데이터 모델 (ChatRequest 등)
│   ├── schemas.py              # API 응답용 DTO
│   └── interfaces.py           # 인터페이스(ABC) 정의
├── services/
│   ├── analyzer.py             # 컨텍스트 분석 및 라우팅 결정 로직
│   ├── key_manager.py          # 키 풀 관리, 랜덤 순환, 티어 감지 및 복구
│   ├── gateway.py              # 오케스트레이션, URL 자동 감지, 세션 관리, 재시도 루프
│   ├── scraper.py              # Playwright 기반 웹 스크래핑 및 검색
│   ├── compressor.py           # LLMLingua-2 기반 컨텍스트 압축
│   └── session_manager.py      # DuckDB 기반 세션 및 설정 영구 저장
├── adapters/
│   └── providers/
│       ├── gemini.py           # Google Gemini 연동 어댑터
│       ├── openai_compat.py    # Groq, Cerebras, Ollama 연동 어댑터
│       └── local_cli.py        # 로컬 CLI(OpenCode, OpenClaw) 연동 어댑터
└── api/
    └── v1/
        ├── endpoints.py        # API 라우트 정의
        └── dependencies.py     # 의존성 주입 정의
```

### 2.2 데이터 흐름

```
클라이언트
  │  POST /v1/chat/completions (또는 /v1/responses)
  ▼
API 레이어 (endpoints.py)
  │
  ▼
게이트웨이 서비스 (gateway.py)
  │
  ├─► SessionManager  ──── DuckDB (이력 로드, 중복 제거 병합)
  │
  ├─► URL 자동 감지  ────► WebScraper (프롬프트 내 URL 수집)
  │
  ├─► Analyzer Service  ──► RoutingDecision (제공자, 모델명 결정)
  │
  └─► 재시도 루프 (최대 3회)
        │
        ├─► Key Manager  ──► API 키 (활성 풀에서 무작위 선택)
        │
        ├─► Adapter (Gemini / OpenAI / Local CLI)
        │       │
        │       └─► 외부 API 호출 / 서브프로세스 실행
        │
        ├── 성공 ──► 응답 ──► 세션 저장 ──► 클라이언트 반환
        │
        └── 실패
              │
              ├─► Key Manager: 실패 보고 ──► 실패 풀로 격리
              └─► 제공자 폴백: 현재 제공자 키 소진 시 타 제공자로 전환
```

---

## 3. API 명세 요약

### `POST /v1/chat/completions`
표준 OpenAI 채팅 API. `model="auto"` 사용 시 지능형 라우팅이 작동하며, `session_id`를 통해 서버 측 세션을 유지할 수 있습니다.

### `POST /v1/responses` (OpenClaw 전용)
OpenClaw의 `openai-responses` 모드와 완벽 호환되는 엔드포인트입니다. `input` 필드를 자동으로 처리하며, 타임아웃 방지를 위해 즉각적인 이벤트를 전송합니다.

### `GET /v1/models`
동적으로 수집된 전체 모델 목록을 반환합니다. 검색 지원(`has_search`) 및 멀티모달 여부를 포함합니다.

---

## 4. 라우팅 전략

**Context Analyzer**는 다음 기준에 따라 모델을 선택합니다:

1. **명시적 모델 지정**: 요청된 모델명이 특정 제공자 모델과 매칭되면 우선 처리.
2. **제공자 자동 선택**: `GEMINI/auto` 등 특정 제공자 내에서 최적 모델 선택.
3. **멀티모달**: 이미지 포함 시 Gemini Vision 모델 우선 배정.
4. **토큰 수 기반**:
   - 1,500 토큰 미만: Groq (Llama 3.3 70B) - 최고 속도.
   - 5,000 토큰 미만: Cerebras (Llama 3.1 70B) - 고속 폴백.
   - 5,000 토큰 이상: Gemini - 대규모 컨텍스트 전용.
5. **티어 인지**: 유료 모델은 `premium` 권한이 있는 키가 존재할 때만 선택됩니다.

---

## 5. 키 관리 및 보안

- **랜덤 순환**: 키 풀 내 부하를 균등하게 분산하여 계정 차단을 방지합니다.
- **자동 강등**: Gemini `limit: 0` 에러 발생 시 해당 키를 즉시 `free` 티어로 재분류하여 장애를 예방합니다.
- **자가 치유**: 백그라운드 태스크가 60초마다 실패한 키를 재검증하여 자동 복구합니다.

---

## 6. 라이선스
본 프로젝트는 **MIT License**에 따라 배포됩니다.
