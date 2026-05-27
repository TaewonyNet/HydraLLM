# HydraLLM: 지능형 오케스트레이터

> 본 문서가 한국어 공식 문서입니다. 영문판([README.md](README.md))은 참고용으로 유지됩니다.

**HydraLLM**은 여러 LLM 리소스를 효율적으로 활용하도록 설계된 컨텍스트 인지 게이트웨이입니다. Gemini / Groq / Cerebras 전반에 걸쳐 요청을 라우팅하며, 공급자별 회로 차단기, 랜덤 키 로테이션(쿼터 인지 쿨다운 포함), 실시간 웹 보강 기능을 제공하고, OpenAI 호환 API를 엄격한 Clean Architecture(Domain 다음 Services 다음 Adapters 다음 API) 위에서 구성합니다.

- **버전**: `1.3.0` (`pyproject.toml`)
- **Python**: `3.10+`
- **실행 진입점**: `python main.py`
- **통합 UI**: `http://localhost:8000/ui`
- **OpenAI 호환 엔드포인트**: `POST /v1/chat/completions`

## 프로젝트 구조

```text
.
├── main.py                       # Uvicorn 실행 진입점 (--debug, --port 지원)
├── src/
│   ├── app.py                    # FastAPI 팩토리, lifespan, 정적 UI 마운트
│   ├── adapters/providers/       # gemini, openai_compat (Groq/Ollama), cerebras, local_cli
│   ├── api/v1/                   # endpoints.py, dependencies.py
│   ├── core/                     # config, container, exceptions, logging
│   ├── domain/                   # enums, interfaces, schemas, models
│   ├── services/                 # analyzer, gateway, key_manager, session_manager,
│   │                             # scraper, compressor, web_context_service,
│   │                             # admin_service, metrics_service, observability,
│   │                             # session_orchestrator, context_manager
│   └── utils/                    # ulid 헬퍼
├── tests/
│   ├── unit/                     # analyzer, key_manager, adapters, ulid, stability
│   ├── integration/              # gateway failover, auto-models, provider validation
│   └── api/                      # FastAPI endpoint contract 테스트
├── static/                       # 통합 SPA (Playground + Dashboard)
├── scripts/                      # validate_flow.py (엔드 투 엔드 라우팅 검증기)
├── pyproject.toml                # Poetry, ruff, mypy, pytest 설정
└── .env                          # 공급자 키와 런타임 설정 (gitignore 처리됨)
```

## 핵심 기능

1. **지능형 라우팅** — `services/analyzer.py::ContextAnalyzer`가 토큰 수, 멀티모달 여부, 탐지된 웹 의도, 명시적 모델 힌트(`provider/model`), 사용 가능한 키 티어를 기반으로 공급자와 모델을 선택합니다.
2. **회로 차단기와 클라우드 페일오버** — `services/gateway.py`가 모든 공급자 호출을 `CircuitBreaker`(실패 5회 임계값, 60초 복구)로 감싸며, `PROVIDER_PRIORITY` 체인(Gemini → Groq → Cerebras)을 따라 재시도합니다.
3. **최종 로컬 폴백** — 모든 클라우드 공급자가 소진되면 Gateway가 `OpenAICompatAdapter`로 `OLLAMA_BASE_URL`의 Ollama에 라우팅합니다.
4. **쿨다운 기반 키 로테이션** — `services/key_manager.py::KeyManager`가 공급자별 풀을 유지하며, 활성 키 집합에서 랜덤하게 선택하고, 쿼터(1시간) 또는 거부/403(24시간) 오류에 대해 더 긴 쿨다운을 적용합니다.
5. **웹 보강** — `services/web_context_service.py`와 `services/scraper.py::WebScraper`(Playwright + Scrapling)가 명시적 URL을 가져오거나 웹 의도 감지 시 스크래핑을 수행하며, 24시간 SQLite 캐시를 사용합니다. 게이트웨이가 웹 컨텍스트 블록을 `request.messages[-2]` 위치에 주입하면 stdout 에 `Web context injected: N chars (session=...)` INFO 로그를 함께 남겨, SQLite 이벤트 스토어를 열어보지 않고도 보강 적용 여부를 즉시 확인할 수 있습니다.
6. **컨텍스트 압축** — `services/compressor.py::ContextCompressor`가 LLMLingua-2(선택적 `compression` extra)를 사용해 긴 히스토리를 축소합니다.
7. **세션 영속화** — `services/session_manager.py::SessionManager`가 SQLite(WAL)에 메시지와 파트를 저장하고, 포킹 및 compaction 임계값을 지원하며, 런타임 설정을 보관합니다.
8. **통합 관리 UI** — `/ui`에서 playground, dashboard, 키 상태, 모델 카탈로그를 하나의 SPA로 제공하며, 모든 fetch 호출은 프록시 안정성을 위해 절대 URL을 사용합니다.
9. **OpenAI API 호환** — 스트리밍 SSE(`chat.completion.chunk` + `[DONE]`) 포함 `/v1/chat/completions`.
10. **웹 의도 키워드 점진 학습** — `services/keyword_store.py::KeywordStore`가 언어별(`ko`, `en`) 키워드를 JSON 파일(`data/web_keywords.{lang}.json`)로 영속화하고, `services/intent_classifier.py::IntentClassifier`가 임베딩 비교에 앞서 부분 문자열 매치를 수행합니다. `scripts/validate_flow.py`가 false negative 질의를 자동으로 `/v1/admin/intent/keywords/learn` 에 기록해 사전을 키워 나갑니다.

## API 표면

모든 엔드포인트는 `src/api/v1/endpoints.py`를 통해 `/v1`에 마운트됩니다.

| 메서드 | 경로 | 목적 |
|--------|------|------|
| `POST` | `/v1/chat/completions` | 기본 채팅 진입점 (스트리밍 지원) |
| `GET`  | `/v1/models` | 발견된 모든 모델 목록 |
| `GET`  | `/v1/admin/sessions` | 영속화된 세션 목록 |
| `POST` | `/v1/admin/sessions/new` | 새 세션 생성 |
| `DELETE` | `/v1/admin/sessions/{session_id}` | 세션 삭제 |
| `GET`  | `/v1/admin/logs?limit=50` | 최근 시스템 로그 |
| `GET`  | `/v1/admin/stats` | 집계된 사용량과 헬스 통계 |
| `GET`  | `/v1/admin/dashboard` | UI용 통계와 최근 로그 |
| `GET`  | `/v1/admin/status` | 실시간 공급자/에이전트 상태 |
| `POST` | `/v1/admin/refresh-models` | 공급자 모델 디스커버리 재실행 |
| `POST` | `/v1/admin/probe` | 모든 키 헬스 프로브 |
| `POST` | `/v1/admin/keys` | 런타임 키 추가 (알려진 문제 참고) |
| `GET`  | `/v1/admin/onboarding` | 온보딩 상태와 사용 가능 모델 |
| `POST` | `/v1/admin/onboarding` | 온보딩 선택 저장 |
| `GET`  | `/v1/admin/intent/keywords` | 언어별 웹 의도 키워드 목록 |
| `POST` | `/v1/admin/intent/keywords` | `{lang,keywords[]}` 수동 키워드 등록 |
| `POST` | `/v1/admin/intent/keywords/learn` | `{query}` false negative 질의에서 키워드 학습(LLM 추출 + 정규식 폴백) |

루트 및 UI 경로:

| 메서드 | 경로 | 목적 |
|--------|------|------|
| `GET` | `/` | 서비스 배너 (`/docs`, `/openapi.json`, `/ui` 링크 제공) |
| `GET` | `/ui` | 통합 관리 SPA (`static/index.html`) |
| `GET` | `/ui/static/*` | 정적 자산 |

## 설치

### 1. Python 가상환경 준비 (권장)

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

> ⚠️ **pydantic v2 필수**: 이 프로젝트는 `pydantic>=2.5` 와 `pydantic-settings>=2.1` 를 요구합니다. 전역 `~/.local` 에 pydantic v1 이 남아 있으면 `ModuleNotFoundError: No module named 'pydantic._internal'` 이 발생하므로 반드시 가상환경을 사용하거나 `pip install --upgrade 'pydantic>=2.5'` 로 업그레이드하세요.

### 2. 의존성 설치

```bash
# (A) pip (PEP 517 / pyproject.toml)
pip install .                       # 런타임만
pip install '.[dev]'                # + pytest / pytest-asyncio / pytest-cov / mypy / ruff
pip install '.[dev,compression]'    # + llmlingua (컨텍스트 압축)
pip install '.[compression]'        # 압축 기능만

# (B) Poetry
poetry install                   # 런타임 + dev (group.dev.dependencies 기본 포함)
poetry install -E compression    # + 컨텍스트 압축
```

> `[tool.poetry.extras]` 에 `dev` extra 가 선언되어 있으므로, Poetry 가 없어도 `pip install '.[dev]'` 한 줄로 테스트·린트·타입체크 도구까지 전부 `pyproject.toml` 기준으로 설치됩니다. 셸에 따라 대괄호 이스케이프가 필요하므로 **따옴표 필수**.

### 3. Playwright 브라우저 바이너리 설치

웹 스크래퍼(`services/scraper.py`) 가 Chromium 을 사용하므로 최초 1회 다운로드가 필요합니다.

```bash
python -m playwright install chromium
```

### 4. 환경 변수 설정

```bash
cp .env.example .env
# .env 를 열어 GEMINI_KEYS, GROQ_KEYS, CEREBRAS_KEYS 등 필요한 값을 채웁니다.
```

### 5. 동작 확인

```bash
python main.py           # 기본 포트 8000 에서 기동
curl http://127.0.0.1:8000/   # {"status":"online", ...}
```

## 명령어

```bash
# 서버 실행 (기본 포트 8000)
python main.py
python main.py --debug --port 8001

# 테스트 (현재 baseline: 106 passed)
pytest                    # 전체 스위트
pytest -m unit            # 단위 테스트만
pytest -m integration     # 통합 테스트만
pytest tests/unit/test_analyzer.py::test_auto_routing   # 특정 테스트

# 코드 품질
ruff check .
ruff check --fix .
mypy src/

# 재현형 격리 전수 테스트 (임시 디렉터리로 복제 → 새 venv → pip install .[dev] → pytest)
#   .env 가 없으면 .env.example 로 자동 fallback.
EXPECTED_TESTS=106 scripts/isolated_test.sh --clean           # Linux / macOS / Git Bash
powershell -ExecutionPolicy Bypass -File scripts/isolated_test.ps1   # 한글 Windows (cp949 안전)
```

### 한글 Windows(cp949) 노트

- `src/core/logging.py` 의 `RotatingFileHandler` 는 `encoding="utf-8"` 로 고정되며, `sys.stdout.reconfigure` 를 통해 콘솔 출력도 UTF-8 로 재설정합니다.
- `src/services/session_manager.py::_get_project_id` 는 `subprocess.run(..., encoding="utf-8", errors="replace")` 를 사용해 git root 경로에 한글이 있어도 디코딩 실패 없이 동작합니다.
- `scripts/isolated_test.sh` 는 실행 전에 `PYTHONUTF8=1 / PYTHONIOENCODING=utf-8 / LC_ALL=C.UTF-8 / LANG=C.UTF-8` 를 export 하고 Git Bash 환경에서는 `chcp.com 65001` 을 선제 호출합니다. 또한 `rsync` 미설치 시 `cp -a` 로 자동 fallback 하고, venv activate 경로를 `.venv/Scripts/activate`(Windows) vs `.venv/bin/activate`(POSIX) 로 자동 선택합니다.
- PowerShell 스크립트는 `chcp 65001` + `[Console]::OutputEncoding=UTF8` + `$env:PYTHONUTF8="1"` 설정과 `robocopy` 기반 복제를 사용하며, pytest 로그를 PS 5.1 의 `Tee-Object` 대신 `[System.IO.File]::WriteAllLines(..., UTF8Encoding(false))` 로 직접 UTF-8(BOM 없음) 저장합니다. 네이티브 한글 Windows 는 **PowerShell 스크립트를 우선 사용**하세요.

## 설정

설정은 `pydantic-settings`를 통해 `.env`에서 로드됩니다(`src/core/config.py::Settings`).
주요 변수:

- **키(쉼표로 구분된 풀)** — `GEMINI_KEYS`, `GROQ_KEYS`, `CEREBRAS_KEYS`
- **우선순위** — `PROVIDER_PRIORITY=gemini,groq,cerebras,ollama,opencode,openclaw`
- **라우팅 기본값** — `DEFAULT_FREE_MODEL`, `DEFAULT_PREMIUM_MODEL`, `MAX_TOKENS_FAST_MODEL`
- **로컬 에이전트** — `OLLAMA_BASE_URL`, `OPENCODE_BASE_URL`, `OPENCLAW_BASE_URL`
- **기능 플래그** — `ENABLE_CONTEXT_COMPRESSION`, `ENABLE_AUTO_WEB_FETCH`, `WEB_CACHE_TTL_HOURS`
- **관리자** — `ADMIN_API_KEY` (선택; 미설정 시 관리자 인증 비활성화)
- **웹 의도 키워드 저장소** — `DATA_DIR`(기본 `data/`), `KEYWORD_EXTRACTION_MODEL`(Ollama 소형 LLM명; 미지정 시 정규식 폴백만 사용)

전체 예시는 `.env.example`을 참고하세요. `.env`는 `.gitignore`에 포함되어 저장소에 커밋되지 않습니다.

### 시크릿 취급 주의

- `.env`는 공급자 키를 **평문**으로 저장하며 **로컬 파일**입니다. `.gitignore`에 이미 포함되어 있어 원격 저장소로는 푸시되지 않습니다.
- 다만 "로컬 평문"은 안전의 보증이 아닙니다. 디스크 위의 다른 시크릿과 동일하게 취급하세요. 파일 권한을 `chmod 600 .env`로 제한하고, 파일 원본을 공유하거나 채팅/AI 어시스턴트/이슈 트래커/스크린샷/페어 프로그래밍 도구에 붙여 넣지 마세요. 키가 머신 밖으로 한 번이라도 나가면 (LLM 세션 트랜스크립트 포함) 탈취된 것으로 간주해야 합니다.
- 실수로 키를 읽거나 붙여넣거나 로그에 남기거나 커밋한 경우 즉시 공급자 콘솔(Gemini / Groq / Cerebras)에서 **폐기 후 재발급**하고 `.env` 값을 교체한 뒤 서버를 재시작하세요.
- 다수 개발자가 사용하는 환경이라면 OS 키체인, Vault, 1Password CLI, 클라우드 KMS 등 정식 시크릿 매니저에서 프로세스 시작 시점에 주입하세요. 동기화되는 dotfile(`~/.config`, 클라우드 백업 등)에 시크릿을 넣지 마세요.

## 알려진 이슈 (2026-04-27 검증)

### 테스트 결과 요약

- **전수 pytest**: **106 passed / 0 failed** — 소스 환경과 격리 환경(`scripts/isolated_test.sh --clean`) 모두에서 동일하게 통과합니다. `EXPECTED_TESTS=106` 으로 기대치를 갱신하세요.
- **mypy**: `mypy src/` 0 errors. Pydantic v2 mypy 플러그인 활성화 상태 유지.
- **ruff (`src/`)**: 0 errors. `tests/` 의 `E402` import-order 위반은 `sys.path` 조작 규약 때문이며 의도적으로 유지됩니다.
- **버전**: `pyproject.toml` 과 `src/app.py` 모두 `1.3.0`.
- **네트워크 의존 통합 테스트**: `test_auto_models_functionality` 는 최종 로컬 폴백이 Ollama 의 임베딩 전용 모델(`bge-m3:latest` 등)을 chat 으로 선택하면 `400 does not support chat` 로 실패할 수 있습니다 (`TROUBLESHOOTING.ko.md` 12 절 참조). 클라우드 키가 모두 살아 있을 때는 영향 없음.

### 웹 컨텍스트와 키 소진의 분리

모든 클라우드 풀이 쿨다운(403/forbidden=24h, quota=1h)에 들어가면 게이트웨이는 즉시 Ollama 로컬 폴백 경로로 전환됩니다. 이때도 웹 검색/스크래핑 결과는 `request.messages[-2]` 위치에 정상적으로 주입되며, `gateway.log` 에 다음과 같은 한 줄이 남습니다.

```
... services.gateway - INFO - Web context injected: 8049 chars into request.messages[-2] (session=...)
```

이 로그가 보이는데도 응답 품질이 낮다면 원인은 **웹 보강 실패가 아니라 폴백 모델의 용량 한계**입니다. 즉시 조치는 `POST /v1/admin/probe` 로 키 재검증을 시도하거나, `POST /v1/admin/keys` 로 유효한 신규 키를 런타임 주입하거나, Ollama 측에 더 큰 모델을 pull 해 두는 방향이 됩니다.

---
*마지막 업데이트: 2026-04-27*
