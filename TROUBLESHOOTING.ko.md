# HydraLLM 트러블슈팅 가이드

> 본 문서가 한국어 공식 문서입니다. 영문판([TROUBLESHOOTING.md](TROUBLESHOOTING.md))은 참고용으로 유지됩니다.

이 문서는 해결된 문제와 현재 열린 이슈를 기록하여 AI 에이전트와 개발자가 로그와 테스트 출력으로부터 HydraLLM을 진단할 수 있도록 돕습니다.

---

## 로그 기반 진단 워크플로

"문제 발생" 보고를 받으면 다음 순서로 진단합니다.

1. **세션/요청 ID 식별** — `gateway.log`에서 세션 ID 또는 `src/core/logging.py`가 주입하는 `[req_…]` 컨텍스트 프리픽스를 grep.
2. **라이프사이클 추적** — 동일한 `req_…`를 공유하는 모든 로그를 검색해 전체 흐름(`Routing decision → web_enrichment → provider execution → (fallback) → response`)을 확인.
3. **Usage 엔벨로프 점검** — `response.usage.gateway_provider`, `gateway_key_index`, `gateway_model`, `routing_reason`으로 요청을 처리한 리소스를 정확히 기록.
4. **메트릭 교차 확인** — `MetricsService.record_request`가 모든 요청의 상태, 레이턴시, 토큰 수를 기록. 관리자 엔드포인트를 통해 `gateway_sessions.sqlite`를 조회.

---

## 해결된 이슈와 패턴

### 1. TypeError: cannot unpack non-iterable NoneType object
- **증상**: 500 Internal Server Error, `_process_with_retries` 언패킹 중 `gateway.py`에서 `TypeError`.
- **원인**: 재시도 루프가 값 반환이나 예외 raise 없이 종료(대개 조용히 실패한 폴백)되어 암묵적으로 `None`을 반환.
- **수정**: 재시도 루프 바깥에서 `raise last_exception`이 보장되고, 모든 폴백 경로가 유효한 `(ChatResponse, list[dict])` 튜플을 반환하도록 함.

### 2. 404 Model Not Found (로컬 에이전트)
- **증상**: `Agent: ollama (Model: llama3) - Error: 404 Not Found`.
- **원인**: 로컬 머신에 존재하지 않는 모델명(예: `llama3`)이 어댑터 기본값에 하드코딩.
- **수정**: 실행 시 `adapter.discover_models()`를 호출하고, 일반 힌트(`ollama`, `auto`)를 디스커버리된 첫 모델에 매핑.

### 3. 429 Quota Exceeded (Gemini/Groq)
- **증상**: 하나 이상의 공급자에서 `Rate limit exceeded: 429`.
- **원인**: 특정 API 키에서 무료 티어 한도 도달.
- **수정**:
  - **엄격한 복구** — `KeyManager`는 프로브가 실제 성공할 때까지 429 실패 키를 failed 풀에 유지.
  - **공급자 폴백** — 공급자 A의 모든 키가 실패하면 재시도 루프가 공급자 B로 전환.
  - **로컬 폴백** — 모든 외부 공급자가 실패하면 `_final_fallback`이 Ollama로 라우팅.

### 4. UI 로드 문제 (대시보드/상태 갱신 안됨)
- **증상**: UI가 "조회 중..."을 표시하거나 대시보드 통계 fetch 실패.
- **원인**: 정적 HTML의 상대 API 경로가 프록시 뒤에서 잘못된 origin을 가리킴.
- **수정**: `static/index.html`과 `static/admin.html`이 절대 URL(`http://localhost:8000/v1/...`)을 사용.

### 5. Gemini 검색 실패 (400 Bad Request)
- **증상**: Gemini 요청이 `400 google_search_retrieval is not supported`로 실패.
- **원인**: Google GenAI SDK가 도구명을 `google_search_retrieval`에서 `google_search`로 변경.
- **수정**: `src/adapters/providers/gemini.py`가 `tools = [{"google_search": {}}]`로 선언.

### 6. Cerebras 공급자 실패
- **증상**: `Unexpected error: name 'CerebrasAdapter' is not defined`.
- **원인**: `gateway.py`의 import 누락과 `app.py`의 초기화 누락.
- **수정**: 어댑터 import를 `gateway.py`에 추가하고 `create_app`에 `cerebras_keys` 배선.

### 7. WebScraper vs 네이티브 Google Search
- **동작**: `WebScraper`가 안티봇 사이트(예: Brunch)에서 실패하면 Gemini의 네이티브 `google_search`로 폴백.
- **이유**: `WebContextService`가 URL 페칭에 실패하고 `has_search`가 true이면 LLM이 내장 도구를 사용해 의도를 충족.
- **탐지 방법**: `gateway.log`에서 `Scrapling failed` 다음 `Sending request to Gemini with tools`를 grep.

### 8. URL 감지 실패 (정규식 이슈)
- **증상**: 한국어 텍스트가 뒤따르는 URL(예: `https://brunch.co.kr/@id/123 사이트 요약`)이 웹 페칭 대상으로 감지되지 않음.
- **원인**: `web_context_service.py`의 원래 `_URL_PATTERN` 정규식이 너무 엄격하여 URL 뒤 추가 문자가 있으면 부분 매치 반환.
- **수정**: 정규식을 `r"https?://[^\s()<>]+"`로 단순화하여 공백이나 유효하지 않은 구분자까지 전체 URL 문자열을 캡처.

### 9. 스트리밍 엔드포인트의 IndentationError
- **증상**: 테스트 수집 또는 서버 기동 중 `src/api/v1/endpoints.py`에서 `IndentationError: unexpected indent`.
- **원인**: `_handle_chat_completion`의 스트리밍 `AsyncIterator` 내부에 중복 코드 블록과 잘못된 들여쓰기.
- **수정**: `generate_stream`이 단일 `try/except` 쌍과 올바른 `yield` / `return` 들여쓰기를 갖도록 정리.

### 10. 명시적 URL에 대한 WebScraper 우회
- **증상**: `auto_web_fetch`가 `False`일 때 사용자 쿼리의 URL이 페칭되지 않음.
- **원인**: `web_context_service.py::enrich_request`가 `auto_web_fetch`와 의도 감지가 모두 false이면 `urls_to_fetch`에 항목이 있어도 조기 반환.
- **수정**: URL 추출이 조기 반환 검사보다 먼저 실행되며, 검사에 `urls_to_fetch` 존재 여부가 포함되도록 수정.

---

## 열린 이슈 (2026-04-15 검증 스냅샷)

다음 항목은 현재 `pytest` / `mypy` / `ruff` 실행에서 탐지됩니다. 일관된 기준선을 유지하기 위해 의도적으로 마스킹하지 않고 추적합니다.

### 11. `POST /v1/admin/keys`가 422 반환 (통합 테스트 실패)
- **재현**: `pytest tests/integration/test_integration.py::TestIntegration::test_admin_keys_endpoint`
- **증상**: `assert 422 == 200` — 엔드포인트가 JSON으로 전송된 `{"provider": "gemini", "keys": ["test-key-1"]}`을 거절.
- **원인**: `src/api/v1/endpoints.py::add_keys`는 `async def add_keys(provider: str, keys: list[str] = Body(...), ...)`로 선언됨. `keys`에만 `Body(...)` 어노테이션이 있기 때문에 FastAPI가 `provider`를 쿼리 파라미터에 바인딩. 테스트가 두 필드 모두 JSON 바디 내부로 전송하므로 FastAPI는 `provider` 누락을 보고하고 `keys`는 잘못 임베드됨.
- **권장 수정안** (미적용): 두 필드를 단일 Pydantic 페이로드 모델로 감싸거나, `provider: str = Body(...)` 또는 `Body(..., embed=True)`를 일관되게 사용.

### 12. Ollama 최종 폴백이 임베딩 모델을 선택 (통합 테스트 실패)
- **재현**: `pytest tests/integration/test_auto_models_functionality.py::test_auto_models_functionality`
- **증상**: `All primary paths failed. Triggering final local fallback.` 이후 `400 - "bge-m3:latest" does not support chat`.
- **원인**: `Gateway._final_fallback`이 `decision.model_name = ""`을 설정하고 `_process_with_agent`에 위임. 해당 메서드는 `OpenAICompatAdapter.discover_models()`가 반환한 첫 번째 모델을 선택(Ollama는 임베더 포함 모든 서비스 모델을 나열). `llama` 부분 문자열 매치가 없을 때는 첫 번째 모델이 선택되므로 `bge-m3:latest` 같은 임베딩 모델이 채팅용으로 뽑힐 수 있음. `OpenAICompatAdapter.discover_models`는 이름에 `embed`, `rerank`, `vision-adapter`가 포함된 모델만 필터링하는데 `bge-m3`는 해당 키워드를 포함하지 않으므로 필터를 통과.
- **권장 수정안** (미적용): Ollama가 디스커버리한 목록을 capability 기준으로 필터링(임베딩/알려진 비채팅 아키텍처 거절)하거나, 현재의 `llama`-only 휴리스틱 대신 설정 가능한 preferred-model 패턴을 노출.

### 13. mypy 공백 (`mypy src/` — 35건)
- **재현**: `mypy src/`
- **주요 오류**:
  - `services/admin_service.py:49` — `AdminService.delete_session`이 `session_manager.delete_session`을 호출하지만 구체 `SessionManager`에는 해당 메서드가 존재하지 않음. `ISessionManager`에는 `clear_session`만 정의. `DELETE /v1/admin/sessions/{id}` 호출 시 런타임 `AttributeError` 가능성.
  - `services/analyzer.py:104`, `adapters/providers/gemini.py:81` — Pydantic v2 플러그인 미설정으로 `RoutingDecision`, `ChatMessage`에 대한 "missing named argument" 허위 보고.
  - `services/gateway.py:211,214` — `model_dump`/`model_extra` 주변 `dict` vs `BaseModel` 유니온 내로잉.
  - `services/key_manager.py:117` — `cooldown_seconds`가 `int` 분기와 `timedelta.total_seconds()`(float) 분기 사이에서 드리프트.
  - `services/scraper.py:13-15` — `ProxySpec` 타이핑 별칭이 클래스로 재할당.
  - `api/v1/endpoints.py`, `app.py`, `services/context_manager.py`, `services/session_manager.py` — 일부 작은 헬퍼에 반환 어노테이션 누락.
- **권장 수정안** (미적용): `pyproject.toml`에 `pydantic.mypy` 플러그인 설치, `ISessionManager`에 `delete_session` 추가 또는 `SessionManager`에 구체 구현 추가, `key_manager` 숫자 타입 엄격화, 반환 어노테이션 추가.

### 14. ruff 린트 백로그 (`ruff check .` — 54건)
- **재현**: `ruff check .`
- **주요 범주**:
  - `B904` 14건 `api/v1/endpoints.py` — `except` 블록 내 `raise HTTPException(...)`에서 `raise ... from err` 사용 필요.
  - `F401` `api/v1/endpoints.py`의 미사용 `re` import.
  - `F403` `src/domain/{enums,interfaces,schemas}/__init__.py`의 와일드카드 재export.
  - `F841` `services/gateway.py`의 미사용 `last_exception`.
  - `EM101`/`EM102` `adapters/providers/gemini.py`와 `services/gateway.py`의 문자열 리터럴 예외.
  - `E402` `tests/AGENTS.ko.md` 규약에 따라 `sys.path`를 먼저 조작하고 `src/`에서 import하는 테스트 파일들. 테스트가 정식 패키지 레이아웃으로 이동하면 제거 가능.
- **권장 수정안** (미적용): `ruff check . --fix`가 네 건을 자동 처리. 나머지는 소규모 리팩터링 필요.

---

## 로그 최적화 팁
- 모든 로그 항목은 상관관계 분석을 위해 `[req_…]`를 포함해야 함. `src/core/logging.py`의 `setup_logging`이 `request_id_ctx`를 통해 주입.
- `settings.debug`가 활성화되면(`python main.py --debug`) `DEBUG` 레벨에서 전체 요청/응답 페이로드를 확인 가능.
- JSON 로그 포맷은 사람이 읽기 어려우므로 피하되, `scripts/analyze_logs.py`가 파싱할 수 있도록 필드 순서를 일관되게 유지.

---
*마지막 업데이트: 2026-04-15*
