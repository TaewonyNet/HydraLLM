# HydraLLM 기술 명세서 (SPEC)

## 1. 개요 · 목적 · 철학

### 1.1 정의
HydraLLM은 **로컬 실행형 오케스트레이션 게이트웨이**로, 이기종 LLM 리소스 — 클라우드 무료·저가 티어(Gemini/Groq/Cerebras)와 로컬 에이전트(Ollama/OpenCode/OpenClaw) — 를 단일 OpenAI 호환 엔드포인트 뒤에서 통합합니다. "로컬"은 게이트웨이가 *실행되고 최종 폴백하는 위치*를 뜻하며, 모델이 로컬이라는 의미가 아닙니다. OpenAI 호환 클라이언트(Claude Code/Cursor/OpenClaw/Continue)는 `base_url`만 바꿔 연결합니다.

### 1.2 목적 (두 층위)
- **연구·교육 프레이밍 (1차 의도)**: API 오케스트레이션, 동적 리소스 관리·부하 최적화, 이기종 공급자 간 컨텍스트 인지 라우팅, 실시간 컨텍스트 보강 기법의 **실증**. 프로덕션 SLA 제품이 아니며, 각 공급자의 ToS·속도 제한 준수 책임은 사용자에게 있습니다(`DISCLAIMER.ko.md`).
- **실용 동작**: 무료·저가 키 풀을 하나의 고가용 엔드포인트처럼 사용하게 하고, 클라우드 소진 시 로컬 모델로 우아하게 저하.

### 1.3 Non-Goals
프로덕션 SLA·과금 플랫폼 아님 / 파인튜닝·학습 시스템 아님 / 분산·멀티노드 아님(Redis 동기화는 로드맵, `IMPROVEMENTS.ko.md`).

### 1.4 설계 철학
원칙은 모두 *지향*이며, 현 구현이 어긋나는 부분은 의도의 재정의가 아니라 결함으로 추적합니다:
1. **단일 소켓** — 모든 공급자를 OpenAI 규격 뒤에 통합.
2. **가용성 우선·우아한 저하** — 서킷브레이커 → `PROVIDER_PRIORITY` 폴백 → 최종 로컬 폴백(Ollama).
3. **컨텍스트 인지 라이트사이징** — 맥락에 *충분한* 가장 싸고 빠른 모델 선택.
4. **로컬 주권·자기충족** — 로컬 실행, 로컬 에이전트로 완전 대체 가능.
5. **자가 치유·자율 운영** — 쿨다운을 존중하는 키 재프로브, 모델 디스커버리, 키워드 학습, 스크래퍼 자동 재시작.
6. **전환 간 연속성** — 공급자/에이전트/모델 전환에도 SQLite(WAL) 세션 보존.
7. **실시간 지식 보강** — 웹 보강으로 컷오프 한계 보완, "Web context injected" 로그로 가시화.
8. **엄격한 경계** — 단방향 Clean Architecture, 얇은 오케스트레이터 + 협업자, async-first.
9. **투명성·관측가능성** — `routing_reason` 상수, comm 로그, 메트릭, `/ui` 대시보드.
10. **연구 프레이밍·사용자 귀속 컴플라이언스** — 명시적 실험 목적, 멀티키 로테이션 ToS 민감성 고지.

## 2. 시스템 아키텍처

레이어 규약: 단방향 의존성 `domain/ ← services/ ← adapters/ ← api/`. 안쪽 레이어는
바깥을 import 하지 않는다. `Gateway` 는 얇은 오케스트레이터이며 무거운 로직은 협업자
(`ResilienceManager`/`WebEnrichmentCoordinator`/`SessionOrchestrator`)에 둔다.
디렉터리 구조 상세는 `README.ko.md` 및 영문 `SPEC.md` §2.1 참조.

### 2.1 데이터 플로우

`Gateway.process_request` 는 아래 순서를 **정확히 이 차례로** 수행한다. 특히
**라우팅이 웹 보강보다 먼저** 결정되므로, 라우팅 토큰 수는 사용자 질의+세션 히스토리
기준이며 주입될 웹 데이터는 포함하지 않는다.

```
Client ──► POST /v1/chat/completions
  ▼ api/v1/endpoints.py  (admin 라우트는 verify_admin_auth 게이트)
  ▼ Gateway.process_request
  1. 입력 정규화            prompt → messages, 빈 입력 거부
  2. SessionOrchestrator    load_history() + save_user_message()  ── SQLite WAL
  3. 프롬프트 조립          [SYSTEM CONTEXT] 날짜/진실성 system 메시지 선두 삽입, 히스토리 병합·중복제거
  4. _get_available_tiers() 공급자별 CircuitBreaker.is_available() → 티어 집합 (OPEN ⇒ 제외)
  5. Analyzer.analyze()     ──► RoutingDecision (토큰 수는 보강 이전 값)
  6. WebEnrichmentCoordinator.enrich()  웹 의도/URL 이면 스크랩 → messages.insert(-1, 웹 블록)
                                        "Web context injected: N chars ..." 정본 로그
  7. ResilienceManager.execute_with_resilience()   ── 재시도 루프(§6)
        서킷 확인 → KeyManager.get_next_key → Adapter.generate
        성공 → report_success → 반환 / 실패 → report_failure → 재시도/폴백/final_fallback(Ollama)
  8. 세션 영속화 후 반환 (스트리밍: chat.completion.chunk + [DONE])
```

---

## 3. 핵심 모듈 명세 (도메인 계약)

### 3.1 enums (`domain/enums/logic.py`)
`ProviderType`(gemini/groq/cerebras), `AgentType`(ollama/opencode/openclaw),
`ModelType`(디스커버리로 채워짐, 하드코딩 금지), `TierType`(FREE/STANDARD/PREMIUM/EXPERIMENTAL/UNKNOWN),
`RoutingReason`, `PartType`.

### 3.2 DTO (`domain/schemas/logic.py`)
`ProviderStatus`, `AgentStatus`, `ModelCapabilities`, `ModelInfo`, `ModelListResponse` (Pydantic v2).

### 3.3 ABC (`domain/interfaces/logic.py`)
어댑터·서비스는 아래 ABC 를 구현하며, 안쪽 레이어는 ABC 에만 의존한다.
- `ILLMProvider`: `generate`, `get_supported_models`, `get_max_tokens`, `is_multimodal`, `discover_models`, `probe_key`.
- `IContextAnalyzer`: `analyze`, `get_supported_models_info`, `get_all_discovered_models_info`, `register_model`.
- `IKeyManager`: `get_next_key(provider, min_tier)`, `report_success`, `report_failure(provider, key, error)`, `get_key_status`.
- `IRouter`: `route_request`, `get_status`, `get_supported_models`, `get_all_models`.
- `ISessionManager`: 세션 로드/영속, 설정, 공급자 헬스, 웹 캐시/스크래핑 메트릭 접근(SQLite).
- `IContextManager`: 멀티모달 컨텍스트 오프로드/캐싱 — Gemini 어댑터가 이 ABC 에 의존(구체 클래스 결합 제거).

---

## 4. API 명세

모든 라우트는 `/v1` 하위에 마운트된다. **공개 2개** + **admin 25개**이며, admin 라우트는 전부 `verify_admin_auth` 게이트.

### 4.1 공개 라우트
- **`POST /v1/chat/completions`** — OpenAI 호환. `ChatRequest` 는 표준에 `session_id`,
  `has_search`/`auto_web_fetch`/`web_fetch`(웹 보강), `compress_context`(압축), `prompt`(레거시),
  `stream` 을 추가. `model="auto"`(또는 `PROVIDER/auto`)는 라우팅(§5). `stream=true` 면
  `chat.completion.chunk` SSE + `data: [DONE]`.
- **`GET /v1/models`** — 디스커버리된 모델 카탈로그(`multimodal`/`has_search` 능력, 가상 `PROVIDER/auto` 포함).

> **미구현**: `POST /v1/responses`(OpenClaw alias)는 초기 초안에만 언급됐고 `endpoints.py` 에
> 존재하지 않으며 현재 범위 밖이다. OpenClaw `openai-responses` 모드가 필요하면 향후 기능으로 구현한다.

### 4.2 Admin 라우트 (25, 전부 게이트)
관측(stats/dashboard/status/logs/comm-logs), 모델·키(refresh-models/probe/keys),
세션(sessions CRUD/cleanup/messages/import), 설정·온보딩(settings/onboarding),
설치(install/status, install/{tool} — opencode/openclaw 를 `curl | bash` 실행, §10 참조),
웹 인텐트 키워드(keywords GET/POST, keywords/learn). 모두 `/v1/admin/*`.

### 4.3 인증·에러
- **Admin 인증**: `X-Admin-Key` 헤더를 `ADMIN_API_KEY` 와 대조. 키 미설정 시 `--debug`/`DEBUG=true`
  가 아니면 **fail-closed(503)**, --debug 에서만 개방(host 가 `0.0.0.0` 바인드라 기본 개방은 위험).
- **에러 계약**: 5xx 응답은 내부 예외 원문을 노출하지 않는다(상세는 서버 로그에만).

---

## 5. 라우팅 전략

`Analyzer.analyze` 는 아래 규칙을 **순서대로(first-match-wins)** 평가해 `RoutingDecision` 을
반환한다. 토큰 수는 **웹 보강 이전**(사용자 질의+히스토리) 기준이다(§2.1).

```
1. 명시적 provider/model 힌트  → 해당 공급자(strict, 교차 폴백 없음); 교차벤더 별칭은 매핑
2. provider-scoped auto       → 해당 공급자 내에서 현재 키 티어로 모델 선택
3. 멀티모달(이미지)            → Gemini Vision (Gemini 키 없으면 5번 토큰 라우팅으로 흘러감)
4. 웹 의도(has_search/실시간)  → 보강(스크랩·주입 ≤6000자)에만 영향, **공급자 강제 안 함**.
                                보강된 요청도 토큰 크기로 라우팅(대개 fast tier) → Gemini 쏠림 완화
5. 토큰 수(단일 임계값 = settings.max_tokens_fast_model, 기본 8192)
   ├─ 임계값 미만 → FAST TIER: Groq ↔ Cerebras **라운드로빈**(가용 fast 공급자 부하 분산)
   └─ 임계값 이상 → Gemini(대용량 컨텍스트)
6. 티어 인지                  → PREMIUM 키가 있을 때만 Pro/프리미엄 모델 선택
```

**불변식(위반은 결함)**: 임계값은 `settings.max_tokens_fast_model` 로 일원화(7000 등 별도
하드코딩 금지); provider 키워드(`cerebras`/`groq`)가 일반 family(`llama`/`deepseek`)보다 우선,
`ollama`(부분문자열 `llama` 포함)는 가장 먼저 매칭; 서킷 OPEN 또는 키풀 빈 공급자는 후보에서 제외.

### 5.1 결정은 낙관적 — 키 부재는 실행 단계에서 해소
Analyzer 는 **맥락상 이상적** 공급자를 반환하며 **로컬 에이전트를 스스로 고르지 않는다**. 가용성
인지는 *fast/None-preferred* 경로에서만 적용(작은 쿼리 + fast-tier 키 없음 → 가용한 Gemini). **키가
전무**해도 클라우드 default(작은→Groq, 대용량→Gemini)를 낸다. **대용량/`auto`** 는 Gemini 키가 없어도
항상 Gemini(대용량 윈도우) 선호. 실제 키 부재 처리(공급자 폴백·로컬 저하)는 `ResilienceManager`(§6.4)가 한다.

---

## 6. 회복탄력성과 폴백

`ResilienceManager.execute_with_resilience`(재시도 루프)와 공급자별 `CircuitBreaker` 가 소유한다.

> **에이전트 실행**(로컬 에이전트 채팅모델 해석 + 어댑터 호출, agent 경로·`final_fallback` 사용)은
> `AgentExecutor`(`services/agent_executor.py`)가 소유한다. `Gateway` 가 이를 생성해
> `ResilienceManager` 에 `process_with_agent` 로 주입하고, ResilienceManager 는 그 콜러블을 호출만 한다.

### 6.1 strict vs fallback
strict 여부는 **analyzer 가 원본 힌트로 한 번** 판정해 `RoutingDecision.strict` 에 싣는다.
ResilienceManager 는 `decision.strict` 만 소비하며, gateway 가 덮어쓴 `request.model` 을 재파싱하지 않는다(구조 결함 S1 해소).
명시적 힌트(`/` 포함 + provider/agent 지정: `gemini/...`, `GEMINI/auto`, `local-agent/...`)면 **strict**(교차 폴백 없음, 실패 시 raise).
순수 `auto`/별칭(`gpt-4o`)은 **fallback** 으로 공급자 체인을 순회한다.

> **설계 노트(결정 vs 복구, 의도)**: analyzer(순수·결정론적·I/O 없음)는 *1차 대상*을 결정하고,
> ResilienceManager 는 실행 시점의 *복구*(재시도·서킷·공급자 폴백·로컬 최종 폴백)를 담당한다.
> 키 소진·서킷 개방·5xx 는 실행해 봐야 알 수 있기 때문이며, 두 레이어 분리는 중복이 아니라 의도된 설계다(통합 금지).

### 6.2 재시도 루프
```
agent 경로(decision.agent 설정 시): 로컬 에이전트 먼저 시도, 실패(non-strict) → 클라우드 체인으로
for provider in PROVIDER_PRIORITY:
    CircuitBreaker.is_available() 아니면 skip (strict ⇒ raise)
    for attempt in range(max_retries=3):
        key=get_next_key; resp=Adapter.generate
        성공 → CircuitBreaker.report_success + KeyManager.report_success → 반환
        RateLimit/ResourceExhausted/ServiceUnavailable → report_failure(서킷+키);
            strict→raise; 활성키 0 → 다음 공급자; else 재시도
모든 공급자 소진 → final_fallback → Ollama(로컬, 서킷 미적용)
```

### 6.3 CircuitBreaker 상태 기계 (공급자별)
단일 스레드 asyncio — 메서드는 동기(원자적)라 락 불필요.

| 상태 | 동작 |
|------|------|
| CLOSED | 통과; 성공 시 failure_count 리셋 |
| → OPEN | 연속 실패 5회(failure_threshold) 후; 60초(recovery_timeout) 차단 |
| OPEN → HALF_OPEN | 타임아웃 경과 후 **단 1건 프로브**만 허용(herd 방지) |
| HALF_OPEN+성공 | → CLOSED, 카운터 리셋 |
| HALF_OPEN+실패 | → 즉시 OPEN(임계값 대기 안 함) |
| 프로브 묶임 | recovery_timeout 후 새 프로브 허용(self-heal) |

### 6.4 키 부재와 우아한 저하
키가 없을 때 실제 라우팅(§5.1 의 결정이 아니라 여기서 해소):

| 상황 | 동작 |
|------|------|
| 일부 공급자만 키 없음 | 결정된 공급자의 `get_next_key` 실패 → `PROVIDER_PRIORITY` 체인으로 키 있는 공급자에 폴백 |
| **`auto` + 클라우드 키 전무** | 클라우드 시도를 통째로 건너뛰고 **즉시 로컬 Ollama** `final_fallback`(early-exit) |
| 실행 중 모든 클라우드 소진 | `final_fallback` → Ollama(로컬, 서킷 미적용) |
| **strict**(명시 `provider/model`) | **폴백 없음.** 해당 공급자 키 없으면 `ResourceExhaustedError` — 로컬 저하 안 함 |

요지: **`auto` 는 모든 자원(로컬 Ollama 포함)이 남아 있는 한 절대 하드 실패하지 않고**, **strict 는
사용자의 명시 선택을 존중해 빠르게 실패**한다.

---

## 7. 키 관리

- **랜덤 로테이션**: 활성 키 풀 내 균등 분산.
- **카테고리별 쿨다운**(실패 시 `cooldown_until` 기록): `403 Forbidden` 24h / `429 Quota` 1h / 기타 5m.
  분류는 예외 카테고리(`ErrorCategory`) 우선, 메시지 휴리스틱 보강.
- **자가 치유**: 60초 폴링이되 `cooldown_until` 경과 키만 재프로브(쿨다운 무시는 회귀). 60초는 폴링 주기지 무효화가 아님.
- **동시성**: 활성/실패 풀 변경(`get_next_key`/`report_success`/`report_failure`/복구)은 동일 `asyncio.Lock` 으로 보호.

> **주의**: 멀티키 로테이션은 일부 공급자 ToS 에 의해 제한될 수 있으며, 준수 책임은 사용자에게 있다.

---

## 8. 웹 보강

`WebEnrichmentCoordinator` → `WebContextService`(+`IntentClassifier`/`KeywordStore`/`WebScraper`). 라우팅 **이후** 실행(§2.1).

- **트리거/페치**: `auto_web_fetch ?? settings.enable_auto_web_fetch`; `_URL_PATTERN` 으로 URL 추출(+`web_fetch`);
  명시 URL 스크랩, 없으면 `needs_web_search` 또는 `has_search` 일 때 `search_and_scrape`. 페치당 `max_chars=6000`.
- **추출**: `trafilatura` 설치 시 본문을 깨끗한 **markdown** 으로 추출(토큰당 신호 밀도↑ → fast tier 도 웹 질의를 잘 답함),
  없으면 BeautifulSoup 텍스트 폴백(무회귀). 캡과 결합돼 주입 컨텍스트가 fast 윈도우에 충분히 들어가므로 웹 의도가 더 이상 Gemini 를 강제하지 않는다(§5).
  `web_cache_ttl_hours` SQLite 캐시 + `record_scraping` 메트릭(cache_hit/success/failed/error).
- **인텐트 분류**: 부분문자열 매치 → trivial/meta 쿼리 early-return → 임베딩(cosine, `bge-m3:latest`; 유효 임계값
  **0.58**(`i18n/{en,ko}.json`); `IntentClassifier.__init__` 의 0.65 하드코딩은 `initialize()` 가 항상 덮어씀.
  `max_pos ≥ threshold and avg_pos > avg_neg`).
- **키워드 스토어**: `data/web_keywords.{ko,en}.json`, 길이 2~60, 대소문자 무시 dedup, 언어당 FIFO 200.
  키워드는 admin 라우트로만 추가(소스 하드코딩 금지); `scripts/validate_flow.py` 가 false negative 를 자동 학습.
- **주입 불변식**: `web_text` 생성 시 `system` 메시지(`name="web_context"`)를 `messages.insert(-1)` 하고
  정본 stdout 로그 `Web context injected: N chars into request.messages[-2] (session=…)` 출력. 회귀 금지(테스트가 단언).

---

## 9. 세션·영속화

`SessionManager`(SQLite)와 `SessionOrchestrator`(`gateway.sessions`)가 소유한다.

- **연결**: `_get_conn()` 은 호출마다 새 연결 + `PRAGMA journal_mode=WAL`. per-task 연결로 `database is locked` 회피.
- **스키마**: `sessions`(`fork_point_message_id` 포킹 지원), `messages`, `parts`(type ∈ text/web_fetch/web_search/compaction/step_cost/retry).
- **compaction 경계**: `load_context`/`load_history` 는 **마지막 `compaction` 파트 이후**의 text/compaction 파트만 로드.
  긴 히스토리를 절단하면서 압축 요약을 새 베이스로 보존.
- **MemoryOptimizer**(`services/memory_optimizer.py`): compaction 을 소유하며 `SessionManager.compact` 가 위임
  (연결 팩토리+토큰 추정기 주입). 임계값(`session_compact_threshold`) 초과 시 — **Phase A** 선택적 pruning(오래된
  `web_fetch`→`[PRUNED]`, `retry` 삭제, 최근 `session_recent_window` 보호) → **Phase B** `ContextCompressor`(LLMLingua-2)
  구조화 요약(요약 입력은 `(role, text[:500])` 단위 dedup) → `compaction` 마커 기록.
- **연속성**: 컨텍스트는 `session_id` 기준이라 공급자/에이전트/모델 전환에도 히스토리 보존(`test_gateway_model_switch_continuity.py` 가드).
- **cp949 안전**: `_get_project_id` 는 git subprocess 를 `encoding="utf-8"` 로 실행.

---

## 10. 비기능 요구

- **Async-first**: 모든 I/O(HTTP/DB/subprocess)는 async. services/adapters 에서 `time.sleep`/`requests` 등 블로킹 금지.
- **보안 모델**:
  - *SSRF*: `WebScraper` 가 호스트 resolve 후 내부/예약 IP 를 속성 검사로 차단(`is_private/loopback/link_local/reserved/multicast/unspecified` + IPv4-mapped 정규화 + CGNAT). **리다이렉트 재검증**(best-effort): 두 fetch 경로 모두 리다이렉트 후 최종 URL(`StealthyFetcher.response.url`, Playwright `page.url`)을 재검사하고, Playwright 경로는 `context.route` 가드로 내부 주소 document 네비게이션을 abort. **잔여**: 단일 resolve 창 내 DNS-rebinding 과 StealthyFetcher 의 최종 URL 재검사 이전 사전 요청은 남으며, 완전 차단은 IP 핀닝이 필요하나 브라우저 기반 fetcher 에서는 불가.
  - *Admin 인증*: 미설정 키는 fail-closed(503), `--debug` 에서만 개방.
  - *시크릿*: API 키는 전체 노출 금지(`[:8]…` 절단); 5xx 에 내부 예외 원문 비노출.
- **인코딩**: 로그 핸들러/`sys.stdout` 에 UTF-8 강제(cp949 안전, `core/logging.py`).
- **ToS**: 멀티키 로테이션은 일부 공급자 ToS 제한 대상일 수 있으며 준수 책임은 사용자에게 있다(`DISCLAIMER.ko.md`). ToS 고지는 사용자 동의 없이 제거 금지.

---

## 11. 라이선스
**MIT License**.

---
*최종 업데이트: 2026-06-20 (버전 1.3.0 기준)*
