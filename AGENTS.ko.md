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
pytest                # 전체 테스트 스위트 실행
mypy src/             # 엄격한 타입 체크 (Python 3.10+)
ruff check .          # 린팅 및 스타일 체크
ruff format .         # 자동 포맷팅
```

## 🛠 프로젝트 아키텍처

- **`src/app.py`**: 비차단형 백그라운드 초기화 기능이 포함된 FastAPI 엔트리 포인트.
- **`src/services/analyzer.py`**: 의사 결정자. 컨텍스트 분석 및 라우팅 휴리스틱을 구현합니다.
- **`src/services/key_manager.py`**: 금고. 랜덤 순환, 티어 감지 및 자가 치유를 담당합니다.
- **`src/services/session_manager.py`**: 메모리. 채팅 및 시스템 설정을 위한 DuckDB 기반 로컬 영구 저장소.
- **`src/services/scraper.py`**: 눈. 커스텀 검색 및 URL 페칭을 위한 Playwright 기반 웹 스크래핑.
- **`src/services/compressor.py`**: 두뇌. 세션 기록 및 웹 콘텐츠를 위한 LLMLingua-2 기반 프롬프트 압축.
- **`src/adapters/providers/`**: 커넥터. Gemini, Groq 및 로컬 CLI 서브프로세스를 위한 특정 로직.
- **`src/api/v1/endpoints.py`**: 인터페이스. API 라우팅 및 엄격한 OpenAI/OpenClaw 호환성을 처리합니다.

## 📝 기술 가이드라인

1. **타입 안전성**: 모든 함수는 완전한 타입 힌트를 사용해야 합니다. Python 3.10+의 `X | Y` 스타일을 권장합니다.
2. **엄격한 호환성**: 
   - 응답의 `model` ID는 항상 요청의 `model` ID와 일치해야 합니다.
   - 깨끗한 JSON 출력을 위해 `response_model_exclude_none=True`를 사용합니다.
   - 스트리밍은 엄격한 OpenAI SSE 형식을 따라야 합니다 (1번째 청크에 `role` 포함, 마지막 청크에 `usage` 포함).
3. **오류 복원력**:
   - 예외 체이닝을 위해 `raise ... from e`를 사용합니다.
   - 풍부한 에러 메시지를 사용합니다: `Model: X (Provider: Y) - Error: Z`.
4. **Clean Architecture**: 비즈니스 로직은 `services`에, 하드웨어/API 세부 사항은 `adapters`에 위치합니다.

## 📡 에이전트를 위한 중요 컨텍스트

- **서브 모델 발견**: 게이트웨이는 시작 시 `opencode models` 등을 실행합니다. 로컬 에이전트 모델 목록을 하드코딩하지 마세요.
- **티어 프로빙**: `FREE` vs `PREMIUM` 상태를 결정하기 위해 실제 미니 요청을 수행합니다. 런타임 상태 API를 신뢰하세요.
- **OpenClaw 통합**: `/v1/responses` 엔드포인트와 `input` 필드 매핑은 OpenClaw 호환성에 매우 중요합니다.
- **자동 웹 페치**: 프롬프트의 URL은 `WebScraper`를 통해 자동으로 감지되고 가져와집니다. `enable_auto_web_fetch` 또는 요청별 `auto_web_fetch` 필드로 제어됩니다.
- **세션 압축**: 긴 세션은 컨텍스트 제한 내에서 유지하기 위해 LLMLingua-2(`ContextCompressor`)를 사용하여 압축됩니다. `enable_context_compression` 또는 요청별 `compress_context` 필드로 제어됩니다.
- **웹 검색**: `has_search: True`를 통해 활성화됩니다. DuckDuckGo HTML 검색 + Playwright 스크래핑을 사용합니다.
- **토큰 라우팅**: 2단계 전략 — 속도를 위한 GROQ/Cerebras (< 8192 토큰), 깊은 컨텍스트 또는 멀티모달을 위한 GEMINI (>= 8192 토큰).

---
*계속해서 바위를 굴립시다. 안정성이 제1목표입니다.*
