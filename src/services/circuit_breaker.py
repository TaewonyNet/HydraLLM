import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """
    Implements a Circuit Breaker pattern to isolate failing providers.
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time: float = 0.0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self._lock = asyncio.Lock()

    def is_available(self) -> bool:
        """Check if the provider is available."""
        if self.state == "CLOSED":
            return True

        if self.state == "OPEN":
            # Check if recovery timeout has passed
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "HALF_OPEN"
                logger.info("Circuit state changed to HALF_OPEN")
                return True
            return False

        if self.state == "HALF_OPEN":
            return True

        return True

    def report_success(self) -> None:
        """Report a successful call."""
        if self.state == "HALF_OPEN":
            self.state = "CLOSED"
            self.failure_count = 0
            logger.info("Circuit state changed to CLOSED after success")
        elif self.state == "CLOSED":
            self.failure_count = 0

    def report_failure(self) -> None:
        """Report a failed call."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            logger.warning(
                f"Circuit state changed to OPEN after {self.failure_count} failures"
            )
