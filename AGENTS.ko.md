# HydraLLM 지식 베이스

> 본 문서가 한국어 공식 문서입니다. 영문판([AGENTS.md](AGENTS.md))은 참고용으로 유지됩니다.

## 시스템 개요
- **스택**: Python 3.10+, FastAPI, Uvicorn, SQLite(WAL), Playwright + Scrapling, LLMLingua-2(선택).
- **목표**: 컨텍스트 인지 라우팅, 다중 공급자 페일오버, 실시간 웹 보강을 결합해 토큰당 LLM 성능을 극대화.
- **아키텍처**: 레이어 간 엄격한 분리의 Clean Architecture (Domain 다음 Services 다음 Adapters 다음 API). 도메인 레이어는 순수하며 모든 외부 I/O는 adapters/services에서 처리.

## 참조 경로
| 기능 | 경로 | 역할 |
|------|------|------|
| **서버 진입점** | `main.py` | `--debug` / `--port` 플래그를 지원하는 Uvicorn 런처. |
| **앱 팩토리** | `src/app.py` | FastAPI 앱 생성, 서비스를 `app.state`에 배선, `/ui` SPA 마운트, lifespan(디스커버리와 키 복구 태스크) 관리. |
| **요청 처리** | `src/api/v1/endpoints.py` | 채팅/모델/관리자/온보딩 HTTP 엔드포인트 (스트리밍 지원). |
| **의존성 주입** | `src/api/v1/dependencies.py` | `request.app.state`를 통해 `get_gateway`, `get_admin_service`, `get_key_manager` 제공. |
| **라우팅 로직** | `src/services/analyzer.py::ContextAnalyzer` | 토큰 수, 멀티모달, 웹 의도, 명시적 힌트에서 공급자/모델을 결정. |
| **오케스트레이션** | `src/services/gateway.py::Gateway` | analyze 다음 web enrich 다음 execute 다음 session persist 흐름 조율, `CircuitBreaker` 풀과 재시도 루프 소유. |
| **키 관리** | `src/services/key_manager.py::KeyManager` | 공급자별 풀, 랜덤 활성 키 선택, 쿼터/거부 쿨다운, 헬스 복구. |
| **웹 페칭** | `src/services/scraper.py::WebScraper` + `services/web_context_service.py` | Playwright/Scrapling 페처와 SSRF 안전 URL 보강. |
| **영속화** | `src/services/session_manager.py::SessionManager` | 세션, 메시지, 파트, 설정, 로그, 스크래핑 캐시의 SQLite WAL 저장소. |
| **압축** | `src/services/compressor.py::ContextCompressor` | LLMLingua-2 프롬프트 프루닝 (선택 extra). |
| **메트릭** | `src/services/metrics_service.py::MetricsService` | 요청별 사용량/레이턴시/상태 기록. |
| **관측** | `src/services/observability.py::Observability` | 라우팅/보강/실행 단계별 트레이스 기록. |
| **어댑터** | `src/adapters/providers/` | `gemini.py`, `openai_compat.py`(Groq + Ollama), `cerebras.py`, `local_cli.py`. |
| **설정** | `src/core/config.py::Settings` | `.env`를 읽는 `pydantic-settings` 로더. |
| **예외** | `src/core/exceptions.py` | `ErrorCategory`에 기반한 `BaseAppError` 계층. |
| **도메인** | `src/domain/enums/logic.py`, `src/domain/interfaces/logic.py`, `src/domain/models.py` | 열거형(`ProviderType`, `AgentType`, `ModelType`, `RoutingReason`), 추상 인터페이스, Pydantic DTO(`ChatRequest`, `ChatResponse`, `RoutingDecision`, `MessagePart`). |

## 요청 라이프사이클 (Chat)

1. **요청 도착** — `POST /v1/chat/completions` 다음 `_handle_chat_completion`이 `request_id_ctx`에 `req_…` ID를 할당.
2. **세션 오케스트레이션** — `SessionOrchestrator.load_history`와 `save_user_message`가 이전 메시지를 로드하고 중복을 제거.
3. **라우팅** — `ContextAnalyzer.analyze`가 `RoutingDecision`(provider/agent/model/reason/confidence/web_search_required)을 생성.
4. **웹 보강** — `WebContextService.enrich_request`가 명시적 URL과 감지된 웹 의도 컨텐츠를 가져옴 (24시간 SQLite 캐시 포함).
5. **실행** — `Gateway._execute_with_full_resilience`가 결정된 공급자를 먼저 시도한 뒤 `PROVIDER_PRIORITY`를 따라 탐색하며, `KeyManager.get_next_key`를 사용하고 공급자별 `CircuitBreaker` 상태를 존중. 전부 소진 시 `_final_fallback`이 `_process_with_agent`를 통해 Ollama로 라우팅.
6. **후처리** — `gateway_provider`, `gateway_key_index`, `gateway_model`, `routing_reason`으로 usage 보강. 어시스턴트 턴은 `SessionOrchestrator.save_assistant_response`로 영속화. 메트릭 기록.
7. **응답** — 비스트리밍 요청은 `ChatResponse`를 반환. 스트리밍은 동일 결과를 OpenAI 스타일 SSE 청크로 감쌈.

## 규약
- **Clean Architecture 경계** — 도메인은 순수 유지. 서비스는 도메인과 어댑터 인터페이스에만 의존. 어댑터는 `ILLMProvider`를 구현. API는 DI를 통해 서비스에 의존.
- **비동기 우선 I/O** — 모든 네트워크/DB/서브프로세스 호출은 `async`여야 함. 서비스에서 `time.sleep`이나 블로킹 `requests` 호출 금지.
- **표준 SSE** — 스트리밍 청크는 `chat.completion.chunk` 형태를 따르며 `data: [DONE]\n\n`으로 종료.
- **예외 분류** — `src/core/exceptions.py`에서 타입드 예외를 raise, 게이트웨이가 카테고리를 HTTP 코드로 매핑.
- **app.state 기반 상태** — 서비스는 `src/app.py::create_app`에서 생성되고 FastAPI DI를 통해 접근. 모듈 수준 싱글턴 사용 금지.
- **라우팅 힌트** — `provider/model` 문자열은 지정된 공급자로 엄격하게 라우팅. `provider/auto`와 기본 `auto`/`default`는 분석기의 선택 로직을 트리거.

## 안티 패턴
- **하드코딩된 모델 리스트** — `register_model` / `discover_models`를 사용. 고정 모델 ID 가정 금지.
- **동기 호출** — 서비스 내부에서 `requests`, `time.sleep`, 블로킹 서브프로세스 사용 금지.
- **UI 내 상대 API URL** — `static/`의 모든 `fetch` 호출은 프록시 뒤에서도 SPA가 동작하도록 절대 `http://host:port/v1/...` 경로를 사용.
- **어댑터 세부 노출** — 어댑터는 벤더 에러를 `src.core.exceptions` 타입으로 변환한 후에 raise. 서비스는 벤더 SDK를 직접 import 금지.
- **인터페이스 우회** — `SessionManager`의 새 기능은 먼저 `ISessionManager`에 추가되어야 서비스(예: `AdminService`)가 추상에 의존 가능. 현재 `AdminService.delete_session`은 인터페이스에 없는 구체 메서드를 호출하고 있어 인터페이스 우선 원칙을 위반한 상태 (추적 중).

## 검증 도구
- **테스트** — `pytest`, 마커는 `unit`, `integration`, `slow` (`pyproject.toml` 참조). 경로는 `pythonpath = ["src"]`로 설정.
- **타입** — `mypy src/` (엄격 기본값: `disallow_untyped_defs`, `warn_return_any`, `strict_equality`).
- **린트** — `ruff check .`, 규칙 `E, F, I, N, W, UP, B, A, C4, T10, EM, ISC`.
- **알려진 갭** — 현재 실패 중인 검사는 `TROUBLESHOOTING.ko.md` 열한 번째 이하 섹션 참조.

---
*마지막 업데이트: 2026-04-15*
