# HydraLLM 기술 명세서 (SPEC)

## 1. 개요
HydraLLM은 Clean Architecture를 준수하는 고가용성 LLM 게이트웨이입니다. 다중 공급자 간 지능형 라우팅, 서킷 브레이커 기반 장애 격리, 실시간 웹 보강 기능을 제공하며, OpenAI API 규격을 완벽히 지원합니다.

## 2. 핵심 컴포넌트 명세
### 2.1 Gateway (`src/services/gateway.py`)
- **오케스트레이션**: 세션 로드 -> 컨텍스트 분석 -> 웹 보강 -> 회복탄력적 실행 -> 영속화 흐름 조율.
- **기술적 라우팅 사유**: 모든 응답에 의사결정 근거를 명확한 상수로 포함합니다.
    - `USER_HINT`: 사용자 명시적 지정
    - `TOKEN_OPTIMIZED`: 토큰 수 기반 최적화
    - `WEB_INTENT_SEARCH`: 웹 검색 인텐트 분석 결과
    - `MULTIMODAL_ANALYSIS`: 이미지 처리 필요성 감지
    - `KEY_AVAILABILITY`: 키 상태 및 페일오버 결과

### 2.2 Context Analyzer (`src/services/analyzer.py`)
- **라우팅 알고리즘**: 토큰 길이, 멀티모달 여부, 웹 검색 의도를 분석하여 최적의 `RoutingDecision` 생성.
- **동적 가용성 필터링**: `KeyManager`의 실시간 상태를 반영하여 활성 키가 없는 공급자의 모델은 선택지에서 제외합니다.

### 2.3 Key Manager (`src/services/key_manager.py`)
- **키 로테이션**: 활성 키 풀 내 랜덤 선택 및 사용량 추적.
- **장애 관리**: 오류 유형별 차등 쿨다운 적용.
    - `403 Forbidden`: 24시간 (키 단위 영구 장애 대응)
    - `429 Rate Limit / Quota`: 1시간
    - 기타 통신 오류: 5분

### 2.4 Web Context Service (`src/services/web_context_service.py`)
- **데이터 보강**: Playwright/Scrapling 기반 실시간 정보 수집 및 프롬프트 주입.
- **성능 추적**: `scraping_metrics` 테이블을 통해 성공률, 수집 글자 수, 지연시간 기록.

## 3. 회복탄력성 및 복구 패턴 (Troubleshooting Insights)
- **Concurrency Guard**: `KeyManager` 및 `SessionManager` 쓰기 작업에 `asyncio.Lock` 및 `threading.Lock`을 적용하여 고부하 상황에서의 데이터 무결성을 보장합니다.
- **Self-Healing Scraper**: 브라우저 인스턴스 충돌 감지 시 자동 재시작 메커니즘을 작동합니다.
- **Unpacking Guard**: 보강 데이터 처리 시 발생할 수 있는 `NoneType` 오류를 방지하기 위해 엄격한 반환 타입(`tuple[list, str | None]`)과 가드를 준수합니다.

---
*최종 업데이트: 2026-04-20 (버전 1.3.0 기준)*
