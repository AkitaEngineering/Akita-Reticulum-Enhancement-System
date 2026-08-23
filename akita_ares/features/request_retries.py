import random
import threading
import time

from akita_ares.core.logger import get_logger


RNS_RETRYABLE_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)


class RetryManager:
    def __init__(self, config, metrics_monitor=None):
        self.logger = get_logger("Feature.RetryManager")
        self.metrics_monitor = metrics_monitor
        self._stats_lock = threading.Lock()
        self.stats = {
            "total_executions": 0,
            "successes": 0,
            "failures_after_retries": 0,
            "successes_on_retry": 0,
        }
        self.update_config(config)

    def update_config(self, config):
        config = config or {}
        self.config = dict(config)
        self.default_max_retries = int(config.get("default_max_retries", 3))
        self.default_delay_seconds = float(config.get("default_delay_seconds", 1))
        self.default_backoff_factor = float(config.get("default_backoff_factor", 2))
        self.default_jitter_max_seconds = float(config.get("default_jitter_max_seconds", 0.5))
        self.log_retries = bool(config.get("log_retries", True))
        self._validate_retry_settings(
            self.default_max_retries,
            self.default_delay_seconds,
            self.default_backoff_factor,
            self.default_jitter_max_seconds,
        )

    @staticmethod
    def _validate_retry_settings(max_retries, delay, backoff, jitter):
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if delay < 0 or jitter < 0:
            raise ValueError("retry delay and jitter cannot be negative")
        if backoff < 1:
            raise ValueError("retry backoff factor must be at least 1")

    @staticmethod
    def _calc_delay(attempt, base_delay, backoff_factor, jitter_max):
        delay = base_delay * (backoff_factor ** (attempt - 1))
        if jitter_max > 0:
            delay += random.uniform(-jitter_max, jitter_max)
        return max(0.0, delay)

    @staticmethod
    def _validate_retry_exceptions(retry_exceptions):
        if isinstance(retry_exceptions, type) and issubclass(retry_exceptions, Exception):
            retry_exceptions = (retry_exceptions,)
        if not isinstance(retry_exceptions, tuple) or not retry_exceptions:
            raise TypeError("retry_ex must be an exception class or non-empty tuple of exception classes")
        if not all(isinstance(item, type) and issubclass(item, Exception) for item in retry_exceptions):
            raise TypeError("retry_ex must contain only Exception subclasses")
        return retry_exceptions

    def _increment_stat(self, key):
        with self._stats_lock:
            self.stats[key] += 1

    def exec_w_retry(
        self,
        op_func,
        *args,
        max_r=None,
        delay_s=None,
        back_f=None,
        jit_max_s=None,
        retry_ex=None,
        op_name="UnnamedOp",
        **kwargs,
    ):
        if not callable(op_func):
            raise TypeError("op_func must be callable")
        max_retries = self.default_max_retries if max_r is None else int(max_r)
        delay = self.default_delay_seconds if delay_s is None else float(delay_s)
        backoff = self.default_backoff_factor if back_f is None else float(back_f)
        jitter = self.default_jitter_max_seconds if jit_max_s is None else float(jit_max_s)
        retry_exceptions = self._validate_retry_exceptions(retry_ex or RNS_RETRYABLE_EXCEPTIONS)
        self._validate_retry_settings(max_retries, delay, backoff, jitter)

        self._increment_stat("total_executions")
        started_at = time.monotonic()
        for attempt in range(1, max_retries + 2):
            try:
                result = op_func(*args, **kwargs)
            except retry_exceptions as exc:
                if attempt > max_retries:
                    self._increment_stat("failures_after_retries")
                    self._record_metrics(op_name, started_at, False, attempt - 1)
                    self.logger.error(
                        "Operation '%s' failed after %s retries: %s",
                        op_name,
                        attempt - 1,
                        exc,
                    )
                    raise
                sleep_seconds = self._calc_delay(attempt, delay, backoff, jitter)
                if self.log_retries:
                    self.logger.warning(
                        "Operation '%s' attempt %s failed with %s; retrying in %.3fs",
                        op_name,
                        attempt,
                        exc.__class__.__name__,
                        sleep_seconds,
                    )
                if sleep_seconds:
                    time.sleep(sleep_seconds)
            except Exception:
                self._increment_stat("failures_after_retries")
                self._record_metrics(op_name, started_at, False, attempt - 1)
                raise
            else:
                self._increment_stat("successes")
                required_retries = attempt - 1
                if required_retries:
                    self._increment_stat("successes_on_retry")
                self._record_metrics(op_name, started_at, True, required_retries)
                return result
        raise RuntimeError(f"Retry loop for {op_name!r} terminated unexpectedly")

    def _record_metrics(self, op_name, started_at, success, required_retries):
        if not self.metrics_monitor:
            return
        self.metrics_monitor.record_operation_duration(op_name, time.monotonic() - started_at)
        self.metrics_monitor.update_retry_stats(
            op_name,
            success=success,
            required_retries=required_retries,
        )

    def wrap_rns_req(self, rns_req_f, op_name_pref="RNSReq"):
        def wrapped(*args, **kwargs):
            destination = kwargs.get("destination", args[0] if args else None)
            operation_name = op_name_pref
            if hasattr(destination, "name_hash"):
                name_hash = destination.name_hash()
                if isinstance(name_hash, bytes):
                    name_hash = name_hash.hex()
                operation_name = f"{op_name_pref}.{str(name_hash)[:8]}"
            return self.exec_w_retry(rns_req_f, *args, op_name=operation_name, **kwargs)

        return wrapped

    def get_stats(self):
        with self._stats_lock:
            return self.stats.copy()
