# HydraLLM 개발 현황

> 본 문서가 한국어 공식 문서입니다. 영문판([STATUS.md](STATUS.md))은 참고용으로 유지됩니다.

이 문서는 HydraLLM 프로젝트의 현재 기술 성숙도, 완료된 기능, 그리고 남아 있는 작업을 요약합니다. 문서 갱신은 실제 코드 검증과 함께 수행됩니다.

- **버전**: `1.3.0` (`pyproject.toml`)
- **Python 목표 버전**: `3.10+`
- **최근 검증일**: `2026-04-15`

---

## 완료 사항

### 1. 핵심 아키텍처와 라우팅
- [x] **Clean Architecture** — `domain/`, `services/`, `adapters/`, `api/` 레이어가 엄격히 분리됨 (`AGENTS.ko.md` 참조).
- [x] **지능형 컨텍스트 라우팅** — `ContextAnalyzer`가 토큰 수, 멀티모달 여부, 탐지된 웹 의도, 명시적 힌트, 티어 가용성을 근거로 공급자와 모델을 선택.
- [x] **공급자 통합** — Google Gemini(native GenAI SDK), Groq(OpenAI 호환 어댑터), Cerebras를 일급 지원.
- [x] **로컬 에이전트 래핑** — Ollama(`OpenAICompatAdapter`), OpenCode/OpenClaw(`LocalCLIAdapter`)를 런타임 모델 디스커버리와 함께 지원.

### 2. 회복탄력성과 안정성
- [x] **회로 차단기** — 모든 클라우드 공급자 호출을 공급자별 `CircuitBreaker`(실패 5회 임계, 60초 복구)로 감쌈.
- [x] **다중 공급자 페일오버** — `_execute_with_full_resilience`가 결정된 공급자를 먼저 시도한 뒤 `PROVIDER_PRIORITY`를 따라 성공 또는 소진까지 탐색.
- [x] **최종 로컬 폴백** — `_final_fallback`이 소진된 트래픽을 `OpenAICompatAdapter`를 통해 Ollama로 라우팅.
- [x] **키 복구 루프** — `Gateway.recover_failed_keys`가 60초 주기의 백그라운드 태스크로 실패 키를 재프로브.
- [x] **쿼터/거부 쿨다운** — `KeyManager.report_failure`가 403(24시간), 쿼터(1시간), 일반(5분) 쿨다운을 구분.

### 3. 데이터와 세션 관리
- [x] **SQLite WAL 영속화** — `SessionManager`가 세션, 메시지, 파트, 시스템 로그, 사용량, 스크래핑 캐시, 런타임 설정을 저장.
- [x] **세션 Compaction** — `SessionOrchestrator`와 `ContextCompressor`(LLMLingua-2)가 `session_compact_threshold` 이상의 히스토리를 축소.
- [x] **세션 포킹** — `SessionManager.fork_session`이 특정 메시지에서 기존 대화를 분기.
- [x] **런타임 설정 저장소** — 온보딩 상태와 모델 허용 목록을 기동 시 SQLite에서 로드.

### 4. 정보 검색과 보강
- [x] **웹 컨텍스트 서비스** — `WebContextService.enrich_request`가 명시적 URL과 웹 의도 쿼리를 가져와 `WebFetchPartData` / `WebSearchPartData`를 메시지 파트에 주입.
- [x] **Scrapling + Playwright 스크래퍼** — `WebScraper`가 SSRF 안전한 URL 처리와 함께 헤드리스 브라우징을 수행.
- [x] **웹 캐시** — 24시간 SQLite 기반 캐시, 대시보드에서 메타데이터 확인 가능.
- [x] **메타데이터 제거** — 검색 쿼리는 업스트림 에이전트의 신뢰할 수 없는 메타데이터를 디스패치 전에 제거.

### 5. 모니터링과 관리
- [x] **통합 관리 UI** — `/ui`에서 playground, dashboard, 공급자 상태, 모델 카탈로그를 하나의 SPA로 제공, 모든 fetch는 절대 URL 사용.
- [x] **메트릭 서비스** — `MetricsService.record_request`가 요청별 토큰, 레이턴시, 상태, 엔드포인트, 공급자를 영속화.
- [x] **관측 트레이싱** — `Observability`가 `req_…` ID별로 라우팅/보강/LLM 단계를 기록.
- [x] **관리자 API 표면** — 세션 CRUD, 로그, 통계, 대시보드, 공급자 상태, 프로브, 모델 리프레시, 온보딩, 런타임 키 주입.
- [x] **로그 분석 유틸리티** — `scripts/analyze_logs.py`가 `gateway.log`에 대해 자동 이슈 진단 수행.
- [x] **요청 ID 트레이싱** — 모든 로그 라인에 `[req_…]` 컨텍스트를 포함하여 상관관계 분석.

### 6. API와 호환성
- [x] **OpenAI 호환 채팅 엔드포인트** — 스트리밍 SSE(`chat.completion.chunk` + `[DONE]`) 포함 `POST /v1/chat/completions`.
- [x] **모델 디스커버리 엔드포인트** — `GET /v1/models`이 가상 `auto`/`<provider>/auto` 포함 모든 등록 모델을 반환.
- [x] **FastAPI lifespan 디스커버리** — 기동 시 초기 공급자 모델 디스커버리와 키 프로브가 백그라운드 태스크로 실행.

---

## 남은 이슈 (2026-04-15 검증 스냅샷)

이 항목들은 명시적으로 추적되고 있으며 마스킹되지 않습니다. 상세 내용은 `TROUBLESHOOTING.ko.md` 열한 번째 이하 섹션을 참고하세요.

### 테스트 결과 (`pytest` — 2 실패 / 70 통과)
- unit + api 테스트는 63개 전부 통과.
- `tests/integration/test_integration.py::TestIntegration::test_admin_keys_endpoint` — `POST /v1/admin/keys`가 422를 반환. 엔드포인트 시그니처가 `provider`를 쿼리 파라미터로 해석하는데 테스트는 JSON 바디로 전송하기 때문.
- `tests/integration/test_auto_models_functionality.py::test_auto_models_functionality` — 로컬 Ollama 폴백이 첫 번째 디스커버리 모델을 선택할 때 임베딩 전용 모델(예: `bge-m3:latest`)이 선택되면 Ollama가 `400 does not support chat`로 거절.

### 타입 오류 (`mypy src/` — 35건 / 10개 파일)
- `services/admin_service.py`가 `session_manager.delete_session`을 호출하지만 `SessionManager`에는 해당 메서드가 존재하지 않으며, `ISessionManager`에는 `clear_session`만 정의되어 있음. `DELETE /v1/admin/sessions/{id}` 호출 시 런타임 실패 위험.
- `services/analyzer.py` / `adapters/providers/gemini.py` — Pydantic v2 mypy 플러그인 미설정에 따른 `RoutingDecision`, `ChatMessage`의 "missing named argument" 허위 보고.
- `services/gateway.py` — `msg.model_extra`와 `dict | BaseModel` 파트 주위의 타입 내로잉 공백.
- `services/key_manager.py` — `cooldown_seconds`가 `int`와 `float` 사이에서 드리프트.
- `services/scraper.py` — `ProxySpec`이 import 후 클래스로 재정의됨.
- 여러 API/app/서비스 함수에 반환 어노테이션 누락.

### 린트 오류 (`ruff check .` — 54건)
- `api/v1/endpoints.py` — 14건의 `B904`(예외 체이닝 누락)와 한 건의 미사용 `re` import.
- `src/domain/{enums,interfaces,schemas}/__init__.py` — `F403` 와일드카드 재export.
- `services/gateway.py` — `F841` 미사용 `last_exception`, `EM102` f-string 예외.
- `adapters/providers/gemini.py` — `EM101`/`EM102` 문자열 리터럴 예외.
- 일부 테스트 파일 — `sys.path` 조작 이후 `src` import로 인한 `E402`. 테스트가 정식 패키지 레이아웃으로 이동하면 제거 가능.

### 버전 드리프트
- `pyproject.toml`은 `1.3.0`이지만 `src/app.py::create_app`이 여전히 `FastAPI(version="1.0.0")`을 생성하여 OpenAPI 스펙에는 구 버전이 노출됨.

---
*마지막 업데이트: 2026-04-15*
