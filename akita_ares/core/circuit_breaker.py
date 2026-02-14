import time
import logging
from enum import Enum
from akita_ares.core.logger import get_logger

logger = get_logger("CircuitBreaker")


class CircuitBreakerState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Simple circuit breaker.

    Attributes expected by tests:
      - state: CircuitBreakerState
      - failure_count: int
      - last_failure_time: float | None
      - recovery_timeout_seconds: float
    """

    def __init__(self, failure_threshold: int, recovery_timeout_seconds: float, name: str = "DefCB"):
        self.failure_threshold = int(failure_threshold)
        self.recovery_timeout_seconds = float(recovery_timeout_seconds)
        self.name = name

        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None

        logger.info(f"CB '{self.name}' init: thr={self.failure_threshold}, t/o={self.recovery_timeout_seconds}s")

    def execute(self, func, *args, **kwargs):
        """Execute `func`. Behavior:
        - If OPEN and recovery timeout hasn't elapsed -> raise CircuitBreakerOpenException
        - If OPEN and timeout elapsed -> move to HALF_OPEN and try one call
        - In HALF_OPEN a failure re-opens the circuit, success closes it
        - In CLOSED failures increment failure_count and open circuit when threshold reached
        """
        # If OPEN, check timeout
        if self.state == CircuitBreakerState.OPEN:
            if self.last_failure_time and (time.monotonic() - self.last_failure_time) > self.recovery_timeout_seconds:
                self._to_half_open()
            else:
                raise CircuitBreakerOpenException(f"CB '{self.name}' is OPEN")

        # HALF_OPEN: single trial
        if self.state == CircuitBreakerState.HALF_OPEN:
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                self._record_failure()
                logger.error(f"CB '{self.name}' HALF_OPEN trial failed: {exc}")
                raise
            else:
                self._record_success()
                return result

        # CLOSED: normal operation
        try:
            result = func(*args, **kwargs)
        except Exception:
            self._record_failure()
            logger.warning(f"CB '{self.name}' caught exception; failure_count={self.failure_count}")
            raise
        else:
            # on success while CLOSED, reset failure_count
            if self.state == CircuitBreakerState.CLOSED and self.failure_count > 0:
                self.failure_count = 0
                self.last_failure_time = None
                logger.info(f"CB '{self.name}' success — failure counter reset")
            return result

    # --- internal helpers -------------------------------------------------
    def _record_success(self):
        if self.state == CircuitBreakerState.HALF_OPEN:
            self._to_closed()
        elif self.state == CircuitBreakerState.CLOSED:
            self.failure_count = 0
            self.last_failure_time = None

    def _record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        logger.warning(f"CB '{self.name}' failure. Count: {self.failure_count}/{self.failure_threshold}")
        if self.failure_count >= self.failure_threshold and self.state != CircuitBreakerState.OPEN:
            self._to_open()

    def _to_closed(self):
        logger.info(f"CB '{self.name}' -> CLOSED")
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None

    def _to_open(self):
        logger.warning(f"CB '{self.name}' -> OPEN for {self.recovery_timeout_seconds}s")
        self.state = CircuitBreakerState.OPEN

    def _to_half_open(self):
        logger.info(f"CB '{self.name}' -> HALF_OPEN")
        self.state = CircuitBreakerState.HALF_OPEN


class CircuitBreakerOpenException(Exception):
    pass
