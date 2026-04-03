[한국어](AGENTS.ko.md) | [English](AGENTS.md)

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
- **테스트 스위트**: `pytest` (61개 테스트 통과)
- **정적 분석**: `mypy src/`, `ruff check .`
- **로그 분석**: `python scripts/analyze_logs.py`
- **트러블슈팅**: [TROUBLESHOOTING.ko.md](TROUBLESHOOTING.ko.md) 참고
- **향후 개선 사항**: [IMPROVEMENTS.ko.md](IMPROVEMENTS.ko.md) 참고
- **아키텍처 리뷰**: [ARCH_REVIEW.ko.md](ARCH_REVIEW.ko.md)에서 전문가 분석 확인
- **개발 현황**: [STATUS.ko.md](STATUS.ko.md)에서 완료된 기능 및 로드맵 확인

## 🛠 프로젝트 아키텍처

HydraLLM은 비즈니스 로직과 인프라를 분리하기 위해 **Clean Architecture** 패턴을 따릅니다.

### 핵심 구성 요소
- **`src/app.py`**: FastAPI 엔트리 포인트. 모든 서비스와 어댑터를 초기화하고, UI를 위한 정적 파일을 마운트하며, 모델 발견 및 키 복구와 같은 백그라운드 작업을 위한 애플리케이션 수명 주기를 정의합니다.
- **`src/services/gateway.py`**: **오케스트레이터**. 전체 요청 생애주기를 조율합니다. 강화를 위해 `WebContextService`를, 지속성을 위해 `SessionOrchestrator`를 활용합니다. 제공자별 **서킷 브레이커(Circuit Breaker)**를 구현합니다.
- **`src/services/analyzer.py`**: **의사 결정자**. 각 요청의 컨텍스트(토큰 수, 멀티모달 필요성, 검색 의도)를 분석하고 가장 적합한 제공자 또는 에이전트로 라우팅합니다.
- **`src/services/web_context_service.py`**: **강화 도구**. 웹 인텐트 감지, `WebScraper`를 통한 URL 페칭, 모델 주입 전 **프롬프트 최적화**(요약)를 처리합니다.
- **`src/services/key_manager.py`**: **금고**. 제공자당 여러 API 키를 관리합니다. 타입 안전한 티어 관리를 위해 `TierType` Enum을 사용합니다. 랜덤 순환 및 에러 기반 격리를 구현합니다.
- **`src/services/session_manager.py`**: **데이터베이스 레이어**. 고동시성 로컬 지속성을 위해 **SQLite (WAL 모드)**를 사용합니다. `usage_metrics`, `system_logs`, `provider_health`를 관리합니다.
- **`src/services/metrics_service.py`**: **감사 도구**. 관리 대시보드를 위해 모델 및 엔드포인트별 실시간 사용 데이터(토큰, 지연 시간, 에러)를 집계합니다.
- **`src/services/scraper.py`**: **눈**. 헤드리스 브라우저 렌더링을 위해 **Scrapling**과 **Playwright**를 사용합니다. URL에서 콘텐츠를 가져오거나 SSRF 보호 기능이 있는 DuckDuckGo 검색을 수행합니다.
- **`src/services/compressor.py`**: **두뇌**. 긴 세션 기록이나 대량의 스크래핑된 콘텐츠를 지능적으로 압축하기 위해 **LLMLingua-2**를 통합합니다.

## 📝 기술 가이드라인

### 1. 타입 안전성
모든 함수와 메서드는 완전한 타입 힌트를 포함해야 합니다. 엄격하게 Python 3.10+ 구문을 사용합니다(예: `Optional[str]` 대신 `str | None`). 이는 CI/CD에서 `mypy`에 의해 강제됩니다.

### 2. 엄격한 호환성
- **모델 ID**: 내부 라우팅에서 다른 모델을 사용하더라도 응답에는 항상 요청된 정확한 `model` ID를 반환해야 합니다.
- **스트리밍 (SSE)**: 응답 스트림은 OpenAI SSE 형식을 따라야 합니다. 첫 번째 청크에는 `role: "assistant"`가 포함되어야 하며, 마지막 청크에는 `gateway_provider` 및 `gateway_key_index`와 함께 `usage` 통계가 포함되어야 합니다.

### 3. 오류 복원력 및 카테고리화
- `src.core.exceptions`의 특정 예외 클래스를 사용하세요.
- 모든 에러는 `RESOURCE_EXHAUSTED`, `VALIDATION_ERROR`, `SERVICE_UNAVAILABLE` 등으로 카테고리화되어야 합니다.
- 예외를 절대 억제하지 마세요. 스택 트레이스를 보존하기 위해 `raise ... from e`를 사용하세요.

### 4. 코드 스타일
- **PEP 8** 컨벤션을 따릅니다. 린팅과 포맷팅 모두에 **Ruff**를 사용합니다.
- 비즈니스 로직은 `services/`에, 제공자별 구현 세부 사항은 `adapters/`에 위치합니다.

## 📡 에이전트를 위한 중요 컨텍스트

### 동적 모델 탐색 프로토콜 (Dynamic Discovery Protocol)
HydraLLM은 모델명을 코드 내에 하드코딩하는 것을 금지합니다. 모든 가용 모델은 반드시 다음 절차를 통해 관리되어야 합니다:
1. **런타임 탐색**: 서버 시작 시 및 실행 중에 `adapter.discover_models()`를 호출하여 실제 설치된 모델(로컬) 또는 제공자가 지원하는 모델(클라우드) 목록을 실시간으로 가져옵니다.
2. **동적 타겟팅**: `Analyzer`는 하드코딩된 맵 대신 `register_model()`을 통해 동적으로 구축된 인메모리 레지스트리를 참조하여 요청을 라우팅합니다.
3. **지능형 매핑**: 사용자가 제네릭한 이름(예: `ollama`, `auto`)으로 요청할 경우, 시스템은 탐색된 목록 중 가장 적합한 모델을 자동으로 선택하여 연결합니다.
4. **추가 모델 반영**: 새로운 로컬 모델이나 제공자 모델이 추가된 경우, 코드를 수정하지 않고 `/v1/admin/refresh-models` API를 호출하여 즉시 시스템에 반영합니다.

### 티어 프로빙 및 복구
키는 시작 시 사전에 프로빙됩니다. 티어를 감지하기 위해 "미니 생성"을 사용합니다. 키가 할당량 제한(429)에 도달하면 격리되며, 60초마다 백그라운드 복구 작업에 의해 재평가됩니다.

### 자동 웹 페치 및 검색
- **자동 감지**: `enable_auto_web_fetch`가 True인 경우 사용자 프롬프트의 URL은 자동으로 가져와집니다.
- **메타데이터 스트리핑**: 쿼리는 검색 엔진으로 전송되기 전 신뢰할 수 없는 메타데이터(예: OpenClaw JSON 블록)가 제거됩니다.
- **최적화**: 컨텍스트 윈도우를 효율적으로 사용하기 위해 2000자 이상의 페칭된 콘텐츠는 자동으로 요약됩니다.

### 관리 및 관측성
- **대시보드**: 실시간 통계를 보려면 `http://localhost:8000/ui/admin`에 접속하세요.
- **트레이싱**: 모든 요청은 고유한 `request_id`를 가집니다. `gateway.log`에서 특정 요청의 생애주기를 추적하려면 `python scripts/analyze_logs.py`를 사용하세요.

---
*계속해서 바위를 굴립시다. 안정성과 성능이 우리의 제1목표입니다.*
