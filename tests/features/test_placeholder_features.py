import unittest
from akita_ares.features import monitoring, path_selection, proxying, request_retries
from akita_ares.core.logger import setup_logging
setup_logging(level='CRITICAL', console_output=False, log_file=None)
class TestFeatures(unittest.TestCase):
    def test_monitoring_init(self):
        config = {'prometheus_port': 9877, 'metrics_prefix': 'test'}
        monitor = monitoring.MetricsMonitor(config)
        self.assertIsNotNone(monitor)
        monitor.stop()

    def test_path_selection_init(self):
        config = {'default_metric': 'rtt', 'metric_update_interval_seconds': 60}
        selector = path_selection.PathSelector(config)
        self.assertIsNotNone(selector)
        selector.stop()

    def test_proxying_init(self):
        config = {'is_proxy_node': False, 'proxy_routes': []}
        proxy = proxying.ProxyManager(config)
        self.assertIsNotNone(proxy)
        proxy.shutdown()

    def test_request_retries_init(self):
        config = {'default_max_retries': 3}
        retry = request_retries.RetryManager(config)
        self.assertIsNotNone(retry)
if __name__ == '__main__': unittest.main()
