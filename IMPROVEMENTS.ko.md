# HydraLLM 개선 사항 및 트러블슈팅 이력

## 1. 해결된 핵심 기술 과제 (2026-04-20)
- **라우팅 투명성 확보**: `routing_reason`을 모호한 텍스트에서 기술 상수로 표준화하여 모니터링 효율 증대.
- **대시보드 데이터 정합성**: SQLite WAL 모드에서의 읽기/쓰기 경합을 해결하고 스크래핑 지표 렌더링 정상화.
- **리소스 인덱싱 고도화**: 대시보드 UI에 각 키의 고유 번호(#)와 티어 정보를 실시간 매핑.
- **모델 경로 중복 해결**: `ollama/ollama/...`와 같은 경로 중첩 현상을 방지하는 정규화 로직 적용.

## 2. 주요 트러블슈팅 사례 및 조치 내역
### 2.1 동시성 이슈 및 데드락 방지
- **현상**: 높은 병렬 요청 시 `KeyManager`에서 키 선택 중 레이스 컨디션 발생.
- **조치**: 잠금(`asyncio.Lock`) 범위를 선택 및 상태 업데이트 전체 프로세스로 확장하여 원자성 확보.

### 2.2 DB 트랜잭션 충돌 (`database is locked`)
- **현상**: 멀티 스레드/태스크 환경에서 단일 SQLite 연결 공유로 인한 커밋 실패.
- **조치**: 각 태스크마다 독립적인 연결을 사용하도록 `_get_conn` 컨텍스트 매니저 도입 및 동기 쓰기 가드 강화.

### 2.3 스크래퍼 리소스 누수 및 자가 치유
- **현상**: 예외 발생 시 Playwright 컨텍스트 미종료로 인한 메모리 팽창.
- **조치**: `try-finally` 구문을 통한 강제 종료 및 브라우저 연결 끊김 시 자동 재시작 로직 구현.

## 3. 하드닝 라운드 (2026-06-21)
전면 감사 후 복원력·보안·라우팅 결함을 일괄 수정하고 아키텍처를 정리했다.

### 3.1 복원력·키 관리
- **쿨다운 존중**: 자가복구가 `cooldown_until` 경과 키만 재프로브(403→24h/quota→1h 불변식 런타임 정상화).
- **풀 락 일관성**: `KeyManager.report_success/report_failure`를 `asyncio.Lock`으로 보호.
- **CircuitBreaker**: HALF_OPEN 단일 프로브 게이팅(thundering-herd 방지)·프로브 실패 시 즉시 재개방·self-heal.

### 3.2 보안
- **Admin fail-closed**: `ADMIN_API_KEY` 미설정 시 `--debug` 가 아니면 admin 라우트 503 차단(host 0.0.0.0 바인드 위험 대응).
- **SSRF 강화**: 속성 기반 차단(IPv4-mapped IPv6·CGNAT 포함) + 리다이렉트 재검증(최종 URL·Playwright route 가드).
- **5xx 마스킹**: 예상치 못한 예외 원문을 클라이언트에 노출하지 않음.

### 3.3 라우팅 (Gemini 쏠림 완화)
- **웹 de-funnel**: 웹 의도가 더 이상 Gemini 를 강제하지 않음(주입 컨텍스트는 fast tier 도 처리).
- **fast tier 라운드로빈**: Groq↔Cerebras 분산.
- **md 추출**: `trafilatura`로 본문→markdown(신호 밀도↑, 없으면 BS4 폴백).
- 토큰 임계값 일원화(매직넘버 7000 제거)·provider 키워드 우선·멀티모달/웹 우선순위 정정.

### 3.4 아키텍처 정리
- **AgentExecutor** 추출: 로컬 에이전트 실행/모델 해석을 Gateway 에서 분리.
- **MemoryOptimizer** 추출: 세션 Compaction V2(pruning+요약+dedup)를 SessionManager 에서 분리.
- **IContextManager** ABC 도입: Gemini 어댑터의 services 구체 결합 제거(단방향 의존성 완전 준수).
- 어댑터 견고화(cerebras `e.response` None 가드, openai_compat 단일 클라이언트 재사용).

### 3.5 SPEC/테스트
- SPEC.md/SPEC.ko.md 11절 전면 재작성(소스 검증 기반) + 키 부재 라우팅(§5.1·§6.4) 명시.
- 회귀 테스트 대폭 추가(circuit_breaker·scraper SSRF·key cooldown·admin auth·routing 차별점·cerebras).

## 4. 향후 개선 로드맵 / 잔여 과제
- **분산 상태 관리**: Redis 기반 키 상태·서킷 브레이커 동기화(단일 노드 한계).
- ~~라우팅 단일화~~ **재평가됨**: analyzer(결정)/resilience(복구) 분리는 *의도된 설계*(순수성·테스트성·복구 적소)이므로 통합하지 않는다. strict 판정이 덮어쓰인 `request.model`을 읽던 구조 결함(S1)은 `RoutingDecision.strict` 도입으로 **해소 완료**.
- **2차 감사 잔여(MED/LOW)**: local_cli subprocess 타임아웃/kill(R7), gemini 동기 블로킹(R8) 및 전역 configure 키 race(R9), keyword_store 블로킹 IO(R10), has_images substring 오탐(R11), 세션 마이그레이션 롤백(R14)·fork None 가드(R15) 등.
- **LiteLLM 전환 검토**: 표준 인터페이스 점진 마이그레이션.

---
*업데이트 날짜: 2026-06-21*
