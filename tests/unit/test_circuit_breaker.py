"""CircuitBreaker 상태 기계 회귀 가드.

5회 실패→OPEN, 복구 타임아웃 후 HALF_OPEN 단일 프로브 게이팅, 프로브 성공→CLOSED /
실패→즉시 재OPEN 을 검증한다. (services/circuit_breaker.py)
"""
import pytest

from src.services.circuit_breaker import CircuitBreaker

pytestmark = pytest.mark.unit


def _expire_open(cb: CircuitBreaker) -> None:
    """recovery_timeout 경과를 시뮬레이션(sleep 없이 last_failure_time 조작)."""
    cb.last_failure_time -= cb.recovery_timeout + 1


def test_closed_until_threshold_then_open():
    cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
    assert cb.is_available() is True
    for _ in range(4):
        cb.report_failure()
    assert cb.state == "CLOSED"  # 임계값 미만이면 닫힌 상태 유지
    cb.report_failure()  # 5번째
    assert cb.state == "OPEN"
    assert cb.is_available() is False  # OPEN 동안 차단


def test_half_open_single_probe_gating():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
    cb.report_failure()
    assert cb.state == "OPEN"
    _expire_open(cb)
    # 첫 호출만 HALF_OPEN 프로브로 통과, 동시 후속 호출은 차단.
    assert cb.is_available() is True
    assert cb.state == "HALF_OPEN"
    assert cb.is_available() is False
    assert cb.is_available() is False


def test_half_open_failure_reopens_immediately():
    cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
    for _ in range(5):
        cb.report_failure()
    _expire_open(cb)
    assert cb.is_available() is True and cb.state == "HALF_OPEN"
    cb.report_failure()  # 프로브 실패
    assert cb.state == "OPEN"  # 임계값 재도달을 기다리지 않고 즉시 재개방


def test_half_open_success_closes_and_resets():
    cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
    for _ in range(5):
        cb.report_failure()
    _expire_open(cb)
    assert cb.is_available() is True and cb.state == "HALF_OPEN"
    cb.report_success()  # 프로브 성공
    assert cb.state == "CLOSED"
    assert cb.failure_count == 0
    assert cb.is_available() is True


def test_stuck_probe_self_heals_after_timeout():
    """프로브가 응답 없이 묶여도 recovery_timeout 후 새 프로브를 허용(self-heal)."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
    cb.report_failure()
    _expire_open(cb)
    assert cb.is_available() is True  # 첫 프로브
    assert cb.is_available() is False  # 묶여 있는 동안 차단
    cb._half_open_since -= cb.recovery_timeout + 1  # 프로브가 오래 묶임
    assert cb.is_available() is True  # 새 프로브 허용


def test_remaining_recovery_seconds_zero_when_not_open():
    """CLOSED/HALF_OPEN 이면 0 — 거부 중이 아니므로 기다릴 필요가 없다는 뜻."""
    cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
    assert cb.remaining_recovery_seconds() == 0.0

    for _ in range(5):
        cb.report_failure()
    _expire_open(cb)
    cb.is_available()  # HALF_OPEN 으로 전환
    assert cb.state == "HALF_OPEN"
    assert cb.remaining_recovery_seconds() == 0.0


def test_remaining_recovery_seconds_counts_down_while_open():
    """OPEN 이면 recovery_timeout 에서 경과 시간을 뺀 남은 시간을 돌려준다 — strict
    라우팅이 Circuit OPEN 거부 시 Retry-After 헤더로 쓸 값(item 2)."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
    cb.report_failure()
    assert cb.state == "OPEN"

    remaining = cb.remaining_recovery_seconds()
    assert 55 < remaining <= 60  # 방금 실패했으니 거의 전체 시간이 남아있어야 함

    cb.last_failure_time -= 50  # 50초 경과를 시뮬레이션
    assert 5 < cb.remaining_recovery_seconds() <= 10

    _expire_open(cb)
    assert cb.remaining_recovery_seconds() == 0.0  # 이미 다 지났으면 0(음수 아님)
