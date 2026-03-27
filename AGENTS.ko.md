[English](AGENTS.md) | [한국어](AGENTS.ko.md)

# 🕵️ AGENTS.md — 개발자 및 에이전트 가이드

이 문서는 **HydraLLM**에서 작업하는 개발자와 AI 에이전트를 위한 기술 지침을 제공합니다.

---

## 🚀 주요 명령어

### 설정 및 실행
```bash
pip install -e .      # 의존성 설치 및 패키지 등록
python main.py        # 서버 실행 (기본 8000 포트)
```

### 품질 및 검증
```bash
pytest                # 전체 테스트 스위트 실행 (33개 테스트 통과)
mypy src/             # 엄격한 타입 체크 (Python 3.10+)
ruff check .          # 린팅 및 스타일 체크
ruff format .         # 자동 포맷팅
python scripts/analyze_logs.py  # gateway.log를 분석하여 에러 및 페치 실패 보고
```

## 🛠 프로젝트 아키텍처

HydraLLM은 비즈니스 로직과 인프라를 분리하기 위해 **Clean Architecture** 패턴을 따릅니다.

### 핵심 구성 요소
- **`src/app.py`**: FastAPI 엔트리 포인트. 모든 서비스와 어댑터를 초기화하고, UI를 위한 정적 파일을 마운트하며, 모델 발견 및 키 복구와 같은 백그라운드 작업을 위한 애플리케이션 수명 주기를 정의합니다.
- **`src/services/analyzer.py`**: **의사 결정자**. 각 요청의 컨텍스트(토큰 수, 멀티모달 필요성, 검색 의도)를 분석하고 가장 적절한 제공자 또는 에이전트로 라우팅합니다.
- **`src/services/key_manager.py`**: **금고**. 제공자당 여러 API 키를 관리합니다. 랜덤 순환, 에러 기반 티어 강등(`FREE` vs `PREMIUM` 감지), 실패한 키의 자동 복구를 구현합니다.
- **`src/services/session_manager.py`**: **메모리**. 로컬 지속성을 위해 **DuckDB**를 사용합니다. 대화 기록과 시스템 설정(온보딩 상태 등)을 저장하여 애플리케이션 재시작 후에도 연속성을 보장합니다.
- **`src/services/scraper.py`**: **눈**. 고급 웹 스크래핑을 위해 **Scrapling**(v0.4.2+)과 **Playwright**를 사용합니다. 안티봇 조치를 우회하여 URL에서 콘텐츠를 가져오거나 DuckDuckGo 검색을 수행합니다.
- **`src/services/compressor.py`**: **두뇌**. **LLMLingua-2**를 통합하여 긴 세션 기록이나 대량의 스크래핑된 콘텐츠를 지능적으로 압축하여, 중요한 컨텍스트를 보존하면서 토큰을 절약합니다.
- **`src/adapters/providers/`**: **커넥터**. 다양한 LLM 제공자(Gemini, Groq, Cerebras) 및 로컬 실행 엔진(Ollama, OpenCode, OpenClaw)에 대한 특화된 로직을 포함합니다.
- **`src/api/v1/endpoints.py`**: **인터페이스**. 표준 OpenAI 호환 엔드포인트를 정의합니다. 또한 특화된 OpenClaw `POST /v1/responses` API와 온보딩 및 상태 모니터링을 위한 내부 관리자 엔드포인트를 처리합니다.

## 📝 기술 가이드라인

### 1. 타입 안전성
모든 함수와 메서드는 완전한 타입 힌트를 포함해야 합니다. 엄격하게 Python 3.10+ 구문을 사용합니다(예: `Optional[str]` 대신 `str | None`). 이는 CI/CD에서 `mypy`에 의해 강제됩니다.

### 2. 엄격한 호환성
- **모델 ID**: 내부 라우팅에서 다른 모델을 사용하더라도 응답에는 항상 요청된 정확한 `model` ID를 반환해야 합니다.
- **스트리밍 (SSE)**: 응답 스트림은 OpenAI SSE 형식을 따라야 합니다. 첫 번째 청크에는 `role: "assistant"`가 포함되어야 하며, 마지막 청크에는 사용 가능한 경우 `usage` 통계가 포함되어야 합니다.
- **OpenClaw**: `POST /v1/responses`에서 `input` 필드 지원을 유지하고 이를 `messages`로 올바르게 매핑해야 합니다.

### 3. 오류 복원력
- 예외를 절대 억제하지 마세요. 스택 트레이스를 보존하기 위해 `raise ... from e`를 사용하세요.
- 풍부한 에러 메시지는 디버깅에 도움이 됩니다: `[Provider: {name}] [Model: {id}] - Error: {msg}`.
- 429/Quota 에러에는 `ResourceExhaustedError`를 사용하여 자동 키 순환 또는 제공자 폴백을 트리거하세요.

### 4. 코드 스타일
- **PEP 8** 컨벤션을 따릅니다.
- 린팅과 포맷팅 모두에 **Ruff**를 사용합니다.
- 비즈니스 로직은 `services/`에, 제공자별 구현 세부 사항은 `adapters/`에 위치합니다.

## 📡 에이전트를 위한 중요 컨텍스트

### 서브 모델 발견
HydraLLM은 시작 시 CLI 명령을 실행하여 로컬 에이전트(예: `opencode`)로부터 모델을 동적으로 발견합니다. 로컬 에이전트의 모델 목록을 하드코딩하지 마세요.

### 티어 프로빙 및 복구
키는 시작 시 사전에 프로빙됩니다. "미니 생성"을 사용하여 티어를 정확하게 감지합니다. 프리미엄 키가 할당량 제한에 도달하면 일시적으로 `FREE`로 강등되며, 60초마다 백그라운드 복구 작업에 의해 재평가됩니다.

### 자동 웹 페치 및 검색
- **자동 감지**: `enable_auto_web_fetch`가 True인 경우 사용자 프롬프트의 URL은 `WebScraper`를 통해 자동으로 가져와집니다.
- **검색 의도**: 일반적인 키워드(예: "오늘의 뉴스", "최신 업데이트")는 자동으로 DuckDuckGo 검색을 트리거합니다.
- **모드**: `WebScraper`는 `standard`(정제됨), `simple`(텍스트 전용), `network_only`(빠름) 모드를 지원합니다.

### 컨텍스트 압축
세션이 `max_tokens_fast_model` 임계값을 초과하면, `ContextCompressor`는 컨텍스트 오버플로를 방지하기 위해 LLMLingua-2를 사용하여 대화 기록을 자동으로 요약합니다.

---
*계속해서 바위를 굴립시다. 안정성과 성능이 우리의 제1목표입니다.*
