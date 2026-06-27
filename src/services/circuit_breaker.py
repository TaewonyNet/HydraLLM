import logging
import time

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """
    공급자 단위 장애 격리를 위한 Circuit Breaker.

    단일 스레드 asyncio 환경을 전제한다. 모든 메서드는 내부에 ``await`` 가 없는
    동기 메서드이므로 이벤트 루프에서 원자적으로 실행된다(코루틴 간 선점 없음).
    따라서 별도 락 없이도 상태 전이의 정합성이 보장된다.

    HALF_OPEN 상태에서는 단 하나의 "프로브" 요청만 통과시킨다(thundering herd 방지).
    프로브가 성공하면 CLOSED, 실패하면 즉시 OPEN 으로 되돌린다.
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time: float = 0.0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        # HALF_OPEN 단일 프로브 게이팅: 프로브 진행 중 여부와 그 시작 시각.
        self._half_open_in_flight = False
        self._half_open_since: float = 0.0

    def is_available(self) -> bool:
        """요청을 이 공급자로 보낼 수 있는지 판단한다(필요 시 상태 전이 수행)."""
        if self.state == "CLOSED":
            return True

        if self.state == "OPEN":
            # 복구 타임아웃 경과 시 HALF_OPEN 으로 전환하고 첫 프로브 1건만 허용.
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "HALF_OPEN"
                self._half_open_in_flight = True
                self._half_open_since = time.time()
                logger.info("Circuit state changed to HALF_OPEN")
                return True
            return False

        if self.state == "HALF_OPEN":
            # 이미 프로브가 진행 중이면 차단. 단, 프로브가 응답 없이 recovery_timeout 을
            # 넘겨 묶인 경우(크래시 등) 새 프로브를 허용해 self-heal.
            if (
                self._half_open_in_flight
                and (time.time() - self._half_open_since) < self.recovery_timeout
            ):
                return False
            self._half_open_in_flight = True
            self._half_open_since = time.time()
            return True

        return True

    def report_success(self) -> None:
        """성공 호출 보고."""
        if self.state == "HALF_OPEN":
            self.state = "CLOSED"
            self.failure_count = 0
            self._half_open_in_flight = False
            logger.info("Circuit state changed to CLOSED after success")
        elif self.state == "CLOSED":
            self.failure_count = 0

    def report_failure(self) -> None:
        """실패 호출 보고."""
        self.last_failure_time = time.time()

        # HALF_OPEN 프로브 실패 → 임계값을 기다리지 않고 즉시 재개방.
        if self.state == "HALF_OPEN":
            self.state = "OPEN"
            self._half_open_in_flight = False
            logger.warning("Circuit re-opened after HALF_OPEN probe failure")
            return

        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            logger.warning(
                f"Circuit state changed to OPEN after {self.failure_count} failures"
            )
