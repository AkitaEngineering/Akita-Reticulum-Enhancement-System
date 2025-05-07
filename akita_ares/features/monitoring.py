from prometheus_client import start_http_server, Gauge, Counter, Histogram, REGISTRY
import threading
from akita_ares.core.logger import get_logger
class MetricsMonitor:
    def __init__(self, config):
        self.logger = get_logger("Feature.MetricsMonitor"); self._http_server_thread = None; self.running = False; self.metrics_initialized = False
        self.custom_registry = REGISTRY 
        self.update_config(config)
    def update_config(self, config):
        self.config = config; new_port = config.get('prometheus_port',9876); new_prefix = config.get('metrics_prefix','ares')
        self.enable_health_endpoint = config.get('enable_health_endpoint', True) 
        if hasattr(self,'port') and (self.port!=new_port or self.prefix!=new_prefix) and self.running: self.logger.warning(f"Prometheus port/prefix changed. Restart ARES for full effect.")
        self.port = new_port; self.prefix = new_prefix
        if not self.metrics_initialized: self._initialize_metrics(); self.metrics_initialized = True
        self.logger.info(f"MetricsMonitor cfg: Port {self.port}, Prefix '{self.prefix}', HealthEP: {self.enable_health_endpoint}")
    def _initialize_metrics(self):
        self.logger.debug(f"Init Prometheus metrics with prefix: {self.prefix}"); registry = self.custom_registry
        def _reg(m_cls, name, *a, **kw): name=f'{self.prefix}_{name}'; try: return m_cls(name,*a,registry=registry,**kw) except ValueError: self.logger.warning(f"Metric {name} already registered."); return registry._names_to_collectors.get(name)
        from akita_ares import VERSION
        self.ares_info = _reg(Gauge,'info','Info about ARES instance',['version']); self.ares_info.labels(version=VERSION).set(1) if self.ares_info else None
        self.active_features = _reg(Gauge,'active_features_count','Num active ARES features')
        self.retry_executions_total = _reg(Counter,'retry_executions_total','Total ops executed via RetryManager',['operation_name'])
        self.retry_successes_total = _reg(Counter,'retry_successes_total','Total successes via RetryManager',['operation_name'])
        self.retry_successes_on_retry_total = _reg(Counter,'retry_successes_on_retry_total','Total successes that required retries',['operation_name'])
        self.retry_failures_total = _reg(Counter,'retry_failures_total','Total failures after all retries',['operation_name'])
        self.retry_operation_duration_seconds = _reg(Histogram,'retry_operation_duration_seconds','Op duration hist with retries',['operation_name'])
        self.proxied_packets_total = _reg(Counter,'proxied_packets_total','Total proxied packets',['proxy_alias','direction'])
        self.active_proxy_routes = _reg(Gauge,'active_proxy_routes_count','Num active client proxy routes')
        self.active_proxy_clients = _reg(Gauge,'active_proxy_clients_count','Num active clients on this proxy node')
        self.path_selection_evaluations_total = _reg(Counter,'path_selection_evaluations_total','Total path selection evals')
        self.path_selection_chosen_metric_value = _reg(Gauge,'path_selection_chosen_metric_value','Metric value for chosen path',['destination_hash','metric_type'])
        self.logger.info("Prometheus metrics (re)checked/defined.")
    def start(self):
        if self.running: self.logger.warning("Prometheus HTTP server already running."); return
        try:
            if not self.metrics_initialized: self._initialize_metrics(); self.metrics_initialized=True
            self._http_server_thread = threading.Thread(target=start_http_server,args=(self.port, '', self.custom_registry),daemon=True,name="PrometheusServerThread")
            self._http_server_thread.start(); self.running=True
            self.logger.info(f"Prometheus metrics server started on port {self.port}. Health endpoint {'enabled (conceptual)' if self.enable_health_endpoint else 'disabled'}.")
            if self.enable_health_endpoint: self.logger.warning("Health endpoint requires custom HTTP server setup (not implemented).")
        except Exception as e: self.logger.error(f"Failed to start Prometheus server on port {self.port}: {e}"); self.running=False
    def stop(self):
        if self.running: self.logger.info("Prometheus server stopping (daemon thread exits with app)."); self.running=False
        else: self.logger.debug("Prometheus server not running or already stopped.")
    def increment_retry_attempt(self, op_name, success=False): pass # Deprecated
    def record_operation_duration(self, op_name, dur_s): self.retry_operation_duration_seconds.labels(op_name).observe(dur_s) if self.retry_operation_duration_seconds else None
    def update_retry_stats(self, op_name, success, required_retries):
        if self.retry_executions_total: self.retry_executions_total.labels(op_name).inc()
        if success:
            if self.retry_successes_total: self.retry_successes_total.labels(op_name).inc()
            if required_retries > 0 and self.retry_successes_on_retry_total: self.retry_successes_on_retry_total.labels(op_name).inc()
        else:
            if self.retry_failures_total: self.retry_failures_total.labels(op_name).inc()
    def increment_proxied_packets(self, proxy_alias, direction='sent_to_proxy'): self.proxied_packets_total.labels(proxy_alias,direction).inc() if self.proxied_packets_total else None
    def set_active_features_count(self, count): self.active_features.set(count) if self.active_features else None
    def set_active_proxy_routes_count(self, count): self.active_proxy_routes.set(count) if self.active_proxy_routes else None
    def set_active_proxy_clients_count(self, count): self.active_proxy_clients.set(count) if self.active_proxy_clients else None
