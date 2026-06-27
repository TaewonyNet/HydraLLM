# Live Checks (수동 검증 스크립트)

이 디렉터리의 스크립트들은 **유효한 공급자 키 / 로컬 에이전트가 구성된 환경**에서
게이트웨이의 실제 라우팅·웹 보강 동작을 눈으로 확인하기 위한 라이브 스모크 검증기다.
`scripts/validate_flow.py` 와 같은 성격이며, 결정적 단위 테스트가 아니다.

원래 `tests/integration/` 에 `test_*.py` 로 있었으나, 실패를 `except…print` 로 삼켜
키 없이도 항상 통과하거나(false-green) 키 없이 실패하는(false-red) 문제가 있어
pytest 수집 대상에서 분리했다. 동일 동작의 결정적 회귀 가드는 다음으로 대체된다:

- 라우팅(provider 고정/매핑): `tests/unit/test_model_routing.py`, `tests/unit/test_analyzer.py`
- 웹 컨텍스트 주입 로그: `tests/integration/test_web_context_injection_log.py`

## 실행

```bash
# .env 에 유효한 키 설정 후
python scripts/live_checks/check_auto_models.py
python scripts/live_checks/check_auto_models_functionality.py
python scripts/live_checks/check_date_priority.py
python scripts/live_checks/check_auto_provider_validation.py
```
