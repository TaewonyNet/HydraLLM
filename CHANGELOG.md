# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-03-27

### Added
- **OpenCode 스타일 세션 관리** (`session_manager.py` 전면 재작성)
  - 메시지 레벨 개별 저장 (JSON blob → 개별 레코드)
  - git root hash 기반 `project_id` → `session_id` 자동 생성
  - Compaction boundary: 토큰 overflow 시 오래된 메시지 요약 → 경계 마커 삽입
  - `load_context()`: 마지막 summary 이후 메시지만 로드
  - `is_overflow()` / `compact()`: 토큰 임계값 초과 감지 및 자동 compaction
  - 기존 JSON blob 세션의 자동 마이그레이션
- `POST /v1/admin/sessions/new` — 서버 세션 생성 API
- `GET /v1/admin/sessions/{id}` — 세션 상세 정보 API
- `config.py`: `session_compact_threshold` (6000), `session_recent_window` (4) 설정
- 프롬프트에 `web_search` 키워드 또는 사이트명 포함 시 auto_web_fetch 플래그와 무관하게 web fetch/검색 트리거
- `site_keywords`에 github, reddit, stackoverflow 등 추가

### Changed
- `gateway.py`: 세션 흐름 전면 개편 — 서버 DB 기반 컨텍스트 로드 + 새 메시지만 추가 + 응답 후 overflow 체크
- `gateway.py`: `_merge_messages()`, `_compress_session_history()` 제거 — session_manager의 compaction으로 대체
- `static/index.html`: 서버 세션 기반으로 전면 수정
  - 페이지 로드 시 서버에서 session_id 발급, localStorage에 저장
  - 전체 chatMessages 대신 새 메시지 + session_id만 전송
  - 세션 목록/전환/삭제/새 세션 생성 UI 추가
  - 실시간 세션 통계 (메시지 수, 토큰 추정치) 표시

## [1.1.0] - 2026-03-27

### Added
- `TierType` Enum (`src/domain/enums.py`) — 문자열 기반 tier 관리를 타입 안전한 Enum으로 교체
- Playwright 브라우저 lifecycle 관리 (`scraper.py`) — startup/shutdown으로 브라우저 재사용
- DuckDB async 래핑 (`session_manager.py`) — `asyncio.to_thread()`로 이벤트 루프 블로킹 방지
- `CHANGELOG.md` — 변경 이력 추적 파일

### Changed
- Dockerfile: `curl` 패키지 추가 (healthcheck 실패 수정), `playwright install chromium` 단계 추가
- `pyproject.toml`: author 이메일 필드 수정
- `main.py`: conditional import를 top-level import로 변경
- `SPEC.md`: 라우팅 전략을 현재 코드의 2-tier(8192) 기준으로 문서 동기화
- `AGENTS.md`, `CLAUDE.md`, `README.md`: 코드 변경사항 반영

### Removed
- `setup.py` — `pyproject.toml`과 중복되므로 제거

## [1.0.0] - 2026-03-26

### Added
- 프로젝트명 HydraLLM으로 명명
- Auto URL detection + web_fetch 자동 감지
- LLMLingua-2 세션 압축 (GPT-like 세션 유지)
- UI: Auto Web Fetch / Context Compression 토글 추가
- UI: RAW API RESULT — Request/Response 탭 분리
- OpenClaw Responses API 호환 (SSE 포맷 포함)
- Cerebras `context_length_exceeded` 에러 시 키 격리 방지
- 2-tier 라우팅 (Groq < 8192 / Gemini >= 8192)
- DuckDuckGo 검색 의도 자동 감지
- WebScraper (Scrapling + Playwright 폴백)
- ContextCompressor (LLMLingua-2)
- DuckDB 기반 세션 영속화
- 다중 API 키 랜덤 순환 및 self-healing

### Initial Release
- Clean Architecture 4계층 (Domain → Services → Adapters → API)
- OpenAI 호환 API (`/v1/chat/completions`, `/v1/models`, `/v1/responses`)
- Gemini, Groq, Cerebras 프로바이더 지원
- Ollama, OpenCode, OpenClaw 로컬 에이전트 통합
- 웹 UI 대시보드 (`/ui`)
