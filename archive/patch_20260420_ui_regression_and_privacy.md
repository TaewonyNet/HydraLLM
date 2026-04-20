# UI/Admin Regression + Privacy Hardening — 2026-04-20

`free_agent` 에서 다음 증상을 동시에 보고 받아 수정했습니다.

1. UI 상 최근 웹 스크래핑 내역이 비어 보임.
2. LLM 메시지 전송 시 서버가 500 반환.
3. 로그 초기화 UI 가 동작하지 않음 (엔드포인트는 있었으나 버튼 없음).
4. 세션 내보내기/가져오기 흐름 변경 요청 (외부 세션 ID 로 이어쓰기 + 첫 진입 시 기존 세션 복원).
5. 로컬 에이전트 설치 상태가 항상 '미설치' 로 표기.
6. 리소스 가용성 사이드바가 비어 보임.
7. `key_manager.get_key_status()` 응답에 API 키 원문이 포함 (개인정보 노출).

모두 원인과 fix 를 기록합니다.

## 1. 루트 원인

| # | 증상 | 원인 |
|---|------|------|
| A | LLM 전송 500 | `gateway.py:341` 이 `self.analyzer.provider_priority` 로 public 속성 접근하는데 `ContextAnalyzer` 는 `_provider_priority` private 으로만 노출. `AttributeError` → 500. |
| B | 스크래핑 내역 비어 있음 | `admin_service.get_stats()` 가 `recent_scraping` 을 별도 키로만 담음. `index.html` dashboard 는 `scraping.recent` 를 기대. 키 불일치. |
| C | 리소스 가용성 사이드바 비어 있음 | `index.html refreshSidebarStatus` 가 `data.status.providers` 에서 providers 를 꺼내지만 실제 응답은 `{status:"healthy", providers:{...}}` 구조이므로 항상 빈 객체. |
| D | 로컬 에이전트 상태 미설치 | `InstallerService` 는 존재하고 DI 까지 주입되었으나 `/v1/admin/install/status`, `/v1/admin/install/{tool}` 엔드포인트 자체가 `endpoints.py` 에 없음. UI 호출이 모두 404. |
| E | 설정/온보딩/통신로그/세션 복원 실패 | UI 가 호출하는 `PUT /admin/settings`, `POST /admin/onboarding`, `GET/DELETE /admin/comm-logs`, `GET /admin/sessions/{id}/messages` 가 모두 미구현. |
| F | 로그 초기화 UI 부재 | `/v1/admin/logs/clear` 는 있었으나 플레이그라운드 UI(`index.html`) 에 버튼이 없었음 (admin.html 에만 있었음). |
| G | API 키 원문 노출 | `key_manager.get_key_status()` 의 `usage` 맵이 `{<원본키>: count}` 형태로 직렬화되어 `/admin/status` 응답에 그대로 나감. 8자 마스킹은 `keys[]` 에만 적용되어 있었음. |

## 2. 적용한 수정

### 2.1 개인정보 (key_manager.py)
- `get_key_status()` 의 `usage` 맵을 인덱스 기반(`{"0": n, "1": n, ...}`)으로 직렬화. 원본 키는 API 응답에 절대 포함하지 않음.

### 2.2 analyzer.py
- `provider_priority` 공용 속성 노출 (`self.provider_priority = list(self._provider_priority)`).
- gateway 의 fallback 순회 루프가 다시 정상 동작 → 500 해결.

### 2.3 endpoints.py 보강
새로 추가한 라우트:
- `PUT /admin/settings` (locale / debug_comm_log 저장)
- `POST /admin/onboarding` (enabled_models 저장 + onboarding_completed=True)
- `GET /admin/sessions/{session_id}/messages` (세션 복원)
- `POST /admin/sessions/import` (외부 세션 ID 검증 + 메시지 반환)
- `GET /admin/install/status`, `POST /admin/install/{tool}` (로컬 에이전트 설치 상태/설치)
- `GET /admin/comm-logs`, `DELETE /admin/comm-logs` (디버그 통신 로그)

### 2.4 admin_service.py
- `get_stats()` 의 `scraping` 블록에 `recent` 필드도 포함 (UI 양쪽 호환).
- `update_settings`, `save_onboarding`, `get_session_messages`, `get_session_info` 추가.
- `get_onboarding_status()` 가 `available_models` 까지 반환해 UI 가 모델 선택지를 렌더.
- `AdminService._gateway` 를 `app.py` 에서 주입 (가용 모델 조회용).

### 2.5 static/index.html UI
- 리소스 가용성: `data.providers` 로 직접 접근.
- 스크래핑 내역: 서버가 `scraping.recent` 를 채우므로 기존 코드 그대로 동작.
- 세션 사이드바: '내보내기' 제거 → 세션 ID 입력 + '이어쓰기' 버튼.
- 세션 전환/초기 진입 시 `restoreSessionMessages()` 가 DB 에서 이력을 받아와 재렌더.
- 시스템 로그 뷰에 '로그 초기화' 버튼 추가.

### 2.6 .gitignore
- 런타임 산출물(`gateway.log`, `gateway_sessions.sqlite`, `startup_error_debug.log`, `test_out.log`) 추가. 세션 DB 와 로그는 사용자 대화/키 조각을 포함할 수 있으므로 신규 체크인 방지.
- 기존에 이미 tracking 중인 `gateway_sessions.sqlite` 는 사용자 판단에 맡기고 삭제/유지 결정은 별도 커밋에서 처리.

## 3. 추가로 확인한 개인정보 상황

- 원본 API 키 패턴(`AIzaSy*`, `gsk_*`, `csk-*`) 은 `.env` 외 소스/정적 자산/문서 어디에도 하드코딩되지 않음.
- 이메일(`twkang@tidesquare.com`) 도 소스 트리에 하드코딩 없음.
- `/admin/status` 응답 키 원문 누출 경로 차단 완료(2.1).

## 4. 운영자 수동 검증 체크리스트

게이트웨이 재기동(`python3 main.py`) 후 브라우저에서 `/ui` 접속.

- [ ] 대시보드에 '최근 웹 스크래핑 내역' 표가 데이터와 함께 표시.
- [ ] `?` 버튼 → '리소스 가용성' 사이드바에 provider 목록 렌더.
- [ ] 플레이그라운드에서 "2+2는?" 전송 → 200 응답, 500 없음.
- [ ] 플레이그라운드 재진입 시 이전 대화 자동 복원.
- [ ] '세션 ID 로 이어쓰기' 입력 후 해당 세션 이력 렌더.
- [ ] 시스템 로그 탭 상단 '로그 초기화' 버튼 작동.
- [ ] 설정 탭 → '로컬 에이전트 설치' 에 opencode/openclaw 실제 설치 여부 표시.
- [ ] `/admin/status` JSON 에 원본 API 키 문자열이 등장하지 않음(마스킹된 8자만).

## 5. 관련 파일

- `src/services/key_manager.py`
- `src/services/analyzer.py`
- `src/services/admin_service.py`
- `src/api/v1/endpoints.py`
- `src/app.py`
- `static/index.html`
- `static/i18n/ko.json`, `static/i18n/en.json`
- `.gitignore`

## 6. 이차 회귀 — 웹 fetch 동작 불능 (동일 일자 후속)

재기동 후 `"레터박스드에서 최근 영화 알려줘"`, `"로이터 오늘 뉴스"` 등 시계열 질의에서 다음 두 가지가 재발견됨.

### 6.1 증상
- 게이트웨이가 500 으로 깨짐: `'ContextAnalyzer' object has no attribute 'get_default_model_for_provider'` (`gateway.log` 2026-04-20 14:34:23).
- 스크래퍼가 DDG HTML 을 받는데도(`Fetched (202) .../html/?q=...`) 후속 `Scraping URL` 로그가 하나도 찍히지 않음 → `web_text` 가 비어 LLM 이 2024 년 환각 응답을 반환.

### 6.2 원인
- G-1: `_get_default_model_for_provider` 는 private 이지만 `gateway.py:358` fallback 루프가 공용 API 로 호출. 2.2 에서 `provider_priority` 만 public 으로 노출하고 이 메소드는 빠졌던 누락.
- G-2: `scraper.search_and_scrape` 가 DuckDuckGo 결과 HTML 을 단일 셀렉터 `a.result__a::attr(href)` 로만 파싱. DDG 가 레이아웃을 변경한 뒤에는 0 매치 → `top_links` 비어 `no_search_results` 반환 → `_process_search` 가 `None, None` 로 폐기. Bing 폴백 없음.

### 6.3 수정
- `analyzer.py` — 공용 `get_default_model_for_provider(provider)` 래퍼 추가(기존 private 은 유지).
- `scraper.py` — DDG 셀렉터를 5 개 후보 순회(`a.result__a`, `a.result__url`, `div.result h2 a`, `div.results_links a`, `a[data-testid='result-title-a']`)로 보강, 0 매치 시 Bing (`li.b_algo h2 a` 등) 로 자동 폴백. 0 링크가 나올 때 `WARNING` 로 남겨 향후 재발을 즉시 관측 가능하게 함. `asyncio.gather(..., return_exceptions=True)` 로 개별 스크랩 실패가 전체 파이프라인을 폐기하지 않도록 변경.

### 6.4 검증 로직 확장 (`scripts/validate_flow.py`)
서버 기동과 무관하게 실행되는 **Preflight** 섹션 추가:
1. `preflight_interface_checks()` — `ContextAnalyzer` 가 gateway/web_context 가 호출하는 공용 API (`analyze`, `get_default_model_for_provider`, `provider_priority` 등 8 메소드 + 1 속성) 를 모두 제공하는지 정적 검사. 이번 유형의 사일런트 AttributeError 를 기동 전에 차단.
2. `preflight_scraper_smoke()` — `"오늘 뉴스"` 쿼리로 `WebScraper._search_links_duckduckgo` → 실패 시 Bing 폴백 경로를 돌려 1 개 이상 링크가 반환되는지 확인. DDG/Bing 셀렉터가 동시에 깨지면 즉시 경고.

두 검사 모두 `openclaw`/LLM 호출 이전에 실행되어 실패 시 `exit 2` 로 조기 종료한다.

### 6.5 관련 파일
- `src/services/analyzer.py` (공용 래퍼 추가)
- `src/services/scraper.py` (다중 셀렉터 + Bing 폴백)
- `scripts/validate_flow.py` (Preflight 추가)

## 7. 삼차 회귀 — Bing 폴백이 10 링크 뽑고도 0 으로 떨어짐

6.3 에서 Bing 폴백을 붙인 뒤에도 LLM 이 계속 환각을 내기에 재분석한 로그:

```
15:09:17,450 Bing selector matched (li.b_algo h2 a::attr(href)): 10 links
15:09:17,450 All search engines returned 0 links for '...'
```

### 7.1 원인
Bing 은 `li.b_algo h2 a` 의 `href` 를 실제 목적지 URL 이 아니라 클릭 추적 URL
(`https://www.bing.com/ck/a?!&&p=...&u=a1<base64(url)>&ntb=1`) 로 감싸서 돌려준다.
Bing 폴백 코드에 넣어둔 필터 `if "bing.com" in abs_link: continue` 가 10 개 전부를
내부 도메인으로 오인해 폐기 → 최종 0 링크. 이어서 web_text 가 비어 LLM 이 세션
히스토리의 과거 환각을 반복 재생산하는 구조.

### 7.2 수정
- `scraper.py` 모듈 상단에 `_unwrap_bing_redirect(url)` 헬퍼 추가.
  - `bing.com/ck/a?` 경로의 `u=a1<base64>` 파라미터를 `base64.urlsafe_b64decode`(실패 시 표준 `b64decode`) 로 풀어 실제 URL 복원.
  - 외부 URL 은 pass-through, bing 내부의 비-ck 페이지는 `None` 반환해 거른다.
- `_search_links_bing` 이 모든 raw 링크를 이 헬퍼로 먼저 디코딩한 뒤 외부 URL 만 모은다.
- 10 개 모두 drop 된 경우 `WARNING` (internal/decode_failed 카운트 포함) 으로 관측 가능하게 남긴다.

### 7.3 검증 강화
`scripts/validate_flow.py` Preflight 에 두 항목 추가:
1. `preflight_bing_decoder()` — ck/a 디코드·외부 pass-through·내부 rejection 3 케이스 정적 검사 (네트워크 불필요).
2. `preflight_scraper_smoke()` — 기존 스모크에 **"최종 반환이 검색엔진 내부 도메인이 아닌지"** 검증 추가. 10 링크 뽑고 전부 내부로 떨어지는 이 회귀를 즉시 감지.

### 7.4 세션 히스토리 오염 관련 (관찰)
web_text 가 복구되더라도, 세션에 이미 남아 있는 과거 환각 assistant 응답은 다음 턴에도 context 로 주입되어 모델이 시점을 유지하려 시도할 수 있다. 현재 시스템 프롬프트 `You MUST prioritize the provided [WEB REFERENCE DATA] over your internal knowledge` 가 이를 억제하지만, 증상이 심한 세션은 사용자가 '+ 새 세션' 으로 시작하면 깨끗하다. 구조 변경 없이 운영 가이드 수준으로만 기록해 둠.

### 7.5 관련 파일
- `src/services/scraper.py` (`_unwrap_bing_redirect` 헬퍼 + `_search_links_bing` 디코드 통합)
- `scripts/validate_flow.py` (`preflight_bing_decoder` + 외부-도메인 단정 추가)

## 8. 과잉 웹 호출 감소

### 8.1 로그에서 관찰된 과잉 호출 패턴
`gateway.log` (2026-04-20) 에서 다음 비용성 문제가 드러남:

| 케이스 | 예 | 문제 |
|--------|-----|------|
| A. trivial 쿼리가 검색 발동 | `"hi"` → `Performing search for: hi` (line 219) | embedding classifier false positive |
| B. 동일 주제 연타 중복 스크래핑 | `로이터에서 최근 뉴스 / 로이터 오늘 뉴스 / 로이터 오늘 기준 뉴스 / 거짓말 말고 로이터 뉴스 지금 실시간 기준으로` — 4회 각각 검색 | search 결과 query-level 캐시 부재 |
| C. meta 쿼리가 그대로 검색어로 전달 | `"다시 검색해줘"` → 그 자체가 검색 쿼리 | refer-only/ack 표현 필터 부재 |

### 8.2 수정
- **trivial-query 가드 (`intent_classifier._is_trivial_query`)** — URL 없는 조건에서 토큰 2개 미만, 한/영 문자 4개 미만, 혹은 `hi`/`응`/`다시`/`더` 등 meta-only 토큰만으로 구성된 쿼리는 embedding classifier 이전에 `False` 로 조기 반환. keyword_store 가 먼저 보는 구조는 유지하므로 사용자가 `"검색해줘"` 같은 명시 키워드를 쓴 경우는 영향 없음.
- **search-cache (query-level) 도입 (`web_context_service._process_search`)** — 기존 URL 용 `web_content_cache` 테이블을 `search:<정규화키>` 로 재사용. hit 시 `cache_hit` 으로 기록하고 재스크랩 skip.
- **검색 키 정규화 (`WebContextService._normalize_search_key`)** — 소문자·구두점 정리에 더해 `알려줘`/`찾아줘`/`검색해줘` 접미사, `다시`/`거짓말 말고`/`please` 접두사, `지금`/`오늘`/`최근`/`실시간`/`기준으로` noise, `에서`/`으로`/`부터`/`은`/`는`/`이`/`가` 등 조사 접미를 제거. 위 4 개 로이터 변형이 모두 `로이터 뉴스` 로 통합되어 단일 캐시 엔트리로 hit.

### 8.3 검증 로직 확장
`validate_flow.py` Preflight 에 `preflight_web_cost_controls()` 추가:
- `IntentClassifier._is_trivial_query` 가 `hi`/`더`/`ok ok` 는 `True`, `로이터 오늘 뉴스 알려줘`/`Reuters news today`/URL 은 `False` 를 반환하는지 6 케이스 단정.
- `로이터` 변형 4 개가 모두 동일한 정규화 키로 귀결되는지 단정 (위 B 재발 즉시 감지).

### 8.4 적용 후 기대 효과
- 같은 세션에서 사실상 동일한 주제를 다른 문장으로 4~5 회 묻더라도 첫 회만 실제 스크래핑, 이후는 `search_cache` hit.
- `hi`/`ok`/`네` 같은 ack 입력은 intent_classifier 를 호출하지 않아 embedding 네트워크 비용 + scraper 호출이 모두 사라짐.
- `/admin/stats` scraping 지표의 `cache_hit` 비중 증가로 실효 관측 가능.

### 8.5 추후 과제(이번 범위 밖)
- 직전 턴 assistant 응답에 `web_context` part 가 있었는지 감지해 refer-only 후속 질문(`"더 자세히"`, `"다시"`) 이 들어오면 기존 web_text 재사용. 현재는 세션 메시지 구조 조회 비용 때문에 보류.
- 정규화 키가 너무 공격적일 때 별개 질의가 충돌할 위험 — 필요하면 TTL 을 짧게 가져가고 `cache_key` 를 `search:<로케일>:<키>` 로 분리.

### 8.6 관련 파일
- `src/services/intent_classifier.py` (`_is_trivial_query` 추가, `needs_web_search` early-return 배치)
- `src/services/web_context_service.py` (`_normalize_search_key`, `_process_search` 캐시 적용)
- `scripts/validate_flow.py` (`preflight_web_cost_controls` 추가)
