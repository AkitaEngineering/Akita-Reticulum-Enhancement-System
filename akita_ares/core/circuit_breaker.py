import threading
import time
from enum import Enum

from akita_ares.core.logger import get_logger

logger = get_logger("CircuitBreaker")


class CircuitBreakerState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Thread-safe circuit breaker with a single half-open probe."""

    def __init__(
        self, failure_threshold: int, recovery_timeout_seconds: float, name: str = "DefCB"
    ):
        self.failure_threshold = int(failure_threshold)
        self.recovery_timeout_seconds = float(recovery_timeout_seconds)
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.recovery_timeout_seconds < 0:
            raise ValueError("recovery_timeout_seconds cannot be negative")
        self.name = str(name)
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None
        self._half_open_call_in_flight = False
        self._lock = threading.Lock()
        logger.info(
            "Circuit breaker '%s' initialized: threshold=%s, recovery_timeout=%ss",
            self.name,
            self.failure_threshold,
            self.recovery_timeout_seconds,
        )

    def execute(self, func, *args, **kwargs):
        if not callable(func):
            raise TypeError("func must be callable")

        with self._lock:
            if self.state == CircuitBreakerState.OPEN:
                elapsed = (
                    time.monotonic() - self.last_failure_time
                    if self.last_failure_time is not None
                    else 0
                )
                if elapsed >= self.recovery_timeout_seconds:
                    self._to_half_open_locked()
                else:
                    raise CircuitBreakerOpenException(f"CB '{self.name}' is OPEN")

            is_half_open_probe = self.state == CircuitBreakerState.HALF_OPEN
            if is_half_open_probe:
                if self._half_open_call_in_flight:
                    raise CircuitBreakerOpenException(
                        f"CB '{self.name}' is HALF_OPEN and its probe is already running"
                    )
                self._half_open_call_in_flight = True

        try:
            result = func(*args, **kwargs)
        except Exception:
            with self._lock:
                self._record_failure_locked(force_open=is_half_open_probe)
            raise
        else:
            with self._lock:
                if is_half_open_probe:
                    self._to_closed_locked()
                else:
                    self.failure_count = 0
                    self.last_failure_time = None
            return result

    def _record_failure_locked(self, force_open=False):
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        self._half_open_call_in_flight = False
        logger.warning(
            "CB '%s' failure. Count: %s/%s",
            self.name,
            self.failure_count,
            self.failure_threshold,
        )
        if force_open or self.failure_count >= self.failure_threshold:
            self._to_open_locked()

    def _to_closed_locked(self):
        logger.info("CB '%s' -> CLOSED", self.name)
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None
        self._half_open_call_in_flight = False

    def _to_open_locked(self):
        logger.warning("CB '%s' -> OPEN for %ss", self.name, self.recovery_timeout_seconds)
        self.state = CircuitBreakerState.OPEN
        self._half_open_call_in_flight = False

    def _to_half_open_locked(self):
        logger.info("CB '%s' -> HALF_OPEN", self.name)
        self.state = CircuitBreakerState.HALF_OPEN
        self._half_open_call_in_flight = False


class CircuitBreakerOpenException(Exception):
    """Raised when a circuit breaker is not accepting work."""
