import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from akita_ares.core.logger import get_logger

METRIC_PREFIX_PATTERN = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


class MetricsMonitor:
    """Owns ARES metrics and its local HTTP health/metrics server."""

    def __init__(self, config):
        self.logger = get_logger("Feature.MetricsMonitor")
        self._http_server = None
        self._http_server_thread = None
        self._server_lock = threading.RLock()
        self.running = False
        self.metrics_initialized = False
        self.custom_registry = CollectorRegistry(auto_describe=True)
        self.update_config(config)

    def update_config(self, config):
        config = config or {}
        new_port = int(config.get("prometheus_port", 9876))
        new_prefix = str(config.get("metrics_prefix", "ares"))
        new_host = str(config.get("listen_host", "127.0.0.1"))
        new_health_enabled = bool(config.get("enable_health_endpoint", True))
        if not 1 <= new_port <= 65535:
            raise ValueError("prometheus_port must be between 1 and 65535")
        if not METRIC_PREFIX_PATTERN.fullmatch(new_prefix):
            raise ValueError("metrics_prefix is not a valid Prometheus metric prefix")

        old_server_config = (
            getattr(self, "listen_host", None),
            getattr(self, "port", None),
            getattr(self, "enable_health_endpoint", None),
        )
        was_running = self.running
        prefix_changed = self.metrics_initialized and self.prefix != new_prefix
        self.config = dict(config)
        self.port = new_port
        self.prefix = new_prefix
        self.listen_host = new_host
        self.enable_health_endpoint = new_health_enabled

        if prefix_changed:
            if self.running:
                self.stop()
            self.custom_registry = CollectorRegistry(auto_describe=True)
            self.metrics_initialized = False
        if not self.metrics_initialized:
            self._initialize_metrics()
            self.metrics_initialized = True

        new_server_config = (self.listen_host, self.port, self.enable_health_endpoint)
        if self.running and old_server_config != new_server_config:
            self.logger.info("Monitoring endpoint configuration changed; restarting HTTP server.")
            self.stop()
        if was_running and not self.running:
            self.start()
        self.logger.info(
            "MetricsMonitor config: host=%s, port=%s, prefix=%s, health=%s",
            self.listen_host,
            self.port,
            self.prefix,
            self.enable_health_endpoint,
        )

    def _initialize_metrics(self):
        registry = self.custom_registry

        def register(metric_class, name, documentation, labels=None):
            return metric_class(
                f"{self.prefix}_{name}",
                documentation,
                labels or [],
                registry=registry,
            )

        from akita_ares import VERSION

        self.ares_info = register(Gauge, "info", "Info about ARES instance", ["version"])
        self.ares_info.labels(version=VERSION).set(1)
        self.active_features = register(
            Gauge, "active_features_count", "Number of active ARES features"
        )
        self.retry_executions_total = register(
            Counter,
            "retry_executions_total",
            "Operations executed via RetryManager",
            ["operation_name"],
        )
        self.retry_successes_total = register(
            Counter,
            "retry_successes_total",
            "Successful operations via RetryManager",
            ["operation_name"],
        )
        self.retry_successes_on_retry_total = register(
            Counter,
            "retry_successes_on_retry_total",
            "Operations succeeding after retries",
            ["operation_name"],
        )
        self.retry_failures_total = register(
            Counter,
            "retry_failures_total",
            "Operations failing after all retries",
            ["operation_name"],
        )
        self.retry_operation_duration_seconds = register(
            Histogram,
            "retry_operation_duration_seconds",
            "Operation duration including retries",
            ["operation_name"],
        )
        self.proxied_packets_total = register(
            Counter, "proxied_packets_total", "Proxied packets", ["proxy_alias", "direction"]
        )
        self.proxy_request_outcomes_total = register(
            Counter,
            "proxy_request_outcomes_total",
            "Proxy request outcomes",
            ["proxy_alias", "mode", "outcome"],
        )
        self.proxy_request_duration_seconds = register(
            Histogram,
            "proxy_request_duration_seconds",
            "Proxy request duration seconds",
            ["proxy_alias", "mode", "outcome"],
        )
        self.proxy_request_phase_duration_seconds = register(
            Histogram,
            "proxy_request_phase_duration_seconds",
            "Proxy request phase duration seconds",
            ["proxy_alias", "mode", "phase", "outcome"],
        )
        self.proxy_policy_denials_total = register(
            Counter,
            "proxy_policy_denials_total",
            "Proxy route policy denials",
            ["proxy_alias", "reason"],
        )
        self.active_proxy_routes = register(
            Gauge, "active_proxy_routes_count", "Active client proxy routes"
        )
        self.active_proxy_clients = register(
            Gauge, "active_proxy_clients_count", "Active clients on this proxy node"
        )
        self.path_selection_evaluations_total = register(
            Counter, "path_selection_evaluations_total", "Path selection evaluations"
        )
        self.path_selection_chosen_metric_value = register(
            Gauge,
            "path_selection_chosen_metric_value",
            "Metric value for chosen path",
            ["destination_hash", "metric_type"],
        )

    def start(self):
        with self._server_lock:
            if self.running:
                return True
            registry = self.custom_registry
            monitor = self

            class MetricsHTTPServer(ThreadingHTTPServer):
                daemon_threads = True
                allow_reuse_address = True
                address_family = socket.AF_INET6 if ":" in monitor.listen_host else socket.AF_INET

            class HealthHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    path = self.path.partition("?")[0]
                    if path == "/health" and monitor.enable_health_endpoint:
                        payload = b"OK"
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                    elif path == "/metrics":
                        payload = generate_latest(registry)
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                    else:
                        payload = b"Not Found\n"
                        self.send_response(404)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def log_message(self, format_string, *args):
                    monitor.logger.debug(
                        "Metrics HTTP %s - %s", self.address_string(), format_string % args
                    )

            try:
                self._http_server = MetricsHTTPServer((self.listen_host, self.port), HealthHandler)
                self._http_server_thread = threading.Thread(
                    target=self._http_server.serve_forever,
                    daemon=True,
                    name="ARESMonitoringHTTPServer",
                )
                self._http_server_thread.start()
                self.running = True
                self.logger.info("Monitoring server started on %s:%s", self.listen_host, self.port)
                return True
            except OSError as exc:
                self._http_server = None
                self._http_server_thread = None
                self.running = False
                raise RuntimeError(
                    f"Failed to start monitoring server on {self.listen_host}:{self.port}: {exc}"
                ) from exc

    def stop(self):
        with self._server_lock:
            server = self._http_server
            thread = self._http_server_thread
            self._http_server = None
            self._http_server_thread = None
            self.running = False
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)

    def increment_retry_attempt(self, op_name, success=False):
        """Backward-compatible single-attempt retry metric update."""
        self.update_retry_stats(op_name, success=success, required_retries=0)

    def record_operation_duration(self, op_name, dur_s):
        self.retry_operation_duration_seconds.labels(op_name).observe(max(0.0, float(dur_s)))

    def update_retry_stats(self, op_name, success, required_retries):
        self.retry_executions_total.labels(op_name).inc()
        if success:
            self.retry_successes_total.labels(op_name).inc()
            if required_retries > 0:
                self.retry_successes_on_retry_total.labels(op_name).inc()
        else:
            self.retry_failures_total.labels(op_name).inc()

    def increment_proxied_packets(self, proxy_alias, direction="sent_to_proxy"):
        self.proxied_packets_total.labels(proxy_alias, direction).inc()

    def increment_proxy_request_outcome(self, proxy_alias, mode, outcome):
        self.proxy_request_outcomes_total.labels(proxy_alias, mode, outcome).inc()

    def record_proxy_request_duration(self, proxy_alias, mode, outcome, dur_s):
        self.proxy_request_duration_seconds.labels(proxy_alias, mode, outcome).observe(
            max(0.0, float(dur_s))
        )

    def record_proxy_request_phase_duration(self, proxy_alias, mode, phase, outcome, dur_s):
        self.proxy_request_phase_duration_seconds.labels(proxy_alias, mode, phase, outcome).observe(
            max(0.0, float(dur_s))
        )

    def increment_proxy_policy_denial(self, proxy_alias, reason):
        self.proxy_policy_denials_total.labels(proxy_alias, reason).inc()

    def set_active_features_count(self, count):
        self.active_features.set(int(count))

    def set_active_proxy_routes_count(self, count):
        self.active_proxy_routes.set(int(count))

    def set_active_proxy_clients_count(self, count):
        self.active_proxy_clients.set(int(count))
