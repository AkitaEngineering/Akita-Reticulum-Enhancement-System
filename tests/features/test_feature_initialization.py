import unittest

from akita_ares.core.logger import setup_logging
from akita_ares.features import monitoring, path_selection, proxying, request_retries


setup_logging(level="CRITICAL", console_output=False, log_file=None)


class TestFeatureInitialization(unittest.TestCase):
    def test_monitoring_init(self):
        monitor = monitoring.MetricsMonitor({"prometheus_port": 9877, "metrics_prefix": "test"})
        self.assertFalse(monitor.running)
        monitor.stop()

    def test_path_selection_init(self):
        selector = path_selection.PathSelector(
            {"default_metric": "hops", "metric_update_interval_seconds": 60}
        )
        self.assertEqual(selector.default_metric_type, "hops")
        selector.stop()

    def test_proxying_init(self):
        proxy = proxying.ProxyManager({"is_proxy_node": False, "proxy_routes": []})
        self.assertEqual(proxy.proxy_routes, [])
        proxy.shutdown()

    def test_request_retries_init(self):
        retry = request_retries.RetryManager({"default_max_retries": 3})
        self.assertEqual(retry.default_max_retries, 3)


if __name__ == "__main__":
    unittest.main()
