import socket
import time
import unittest
import urllib.request
import uuid

from types import SimpleNamespace

from akita_ares.core.logger import setup_logging
from akita_ares.features.monitoring import MetricsMonitor
from akita_ares.features.path_selection import PathSelector
from akita_ares.features.proxying import ProxyManager
from akita_ares.features.request_retries import RetryManager


setup_logging(level='CRITICAL', console_output=False, log_file=None)


def _reserve_port():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _FakePath:
    def __init__(self):
        self.path_id = 'path-1'
        self.hops = 1


class _FakeLink:
    def __init__(self):
        self.link_id = b'1234567890abcdef'
        self.resource_callback = None
        self.closed_callback = None

    def set_resource_callback(self, callback):
        self.resource_callback = callback

    def set_link_closed_callback(self, callback):
        self.closed_callback = callback


class _FakeMetrics:
    def __init__(self):
        self.proxy_client_counts = []
        self.proxy_route_counts = []

    def set_active_proxy_clients_count(self, count):
        self.proxy_client_counts.append(count)

    def set_active_proxy_routes_count(self, count):
        self.proxy_route_counts.append(count)

    def increment_proxied_packets(self, proxy_alias, direction='sent_to_proxy'):
        return None


class TestFeatureRegressions(unittest.TestCase):
    def test_monitoring_serves_metrics_and_health(self):
        port = _reserve_port()
        prefix = f"test_{uuid.uuid4().hex[:8]}"
        monitor = MetricsMonitor({
            'prometheus_port': port,
            'listen_host': '127.0.0.1',
            'metrics_prefix': prefix,
            'enable_health_endpoint': True,
        })
        monitor.start()
        try:
            time.sleep(0.1)
            metrics_body = urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics', timeout=2).read().decode('utf-8')
            health_body = urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2).read().decode('utf-8')
        finally:
            monitor.stop()
        self.assertIn(f'{prefix}_info', metrics_body)
        self.assertEqual(health_body, 'OK')

    def test_path_selection_with_metrics_does_not_raise(self):
        prefix = f"test_{uuid.uuid4().hex[:8]}"
        monitor = MetricsMonitor({'prometheus_port': _reserve_port(), 'metrics_prefix': prefix})
        selector = PathSelector({'default_metric': 'hops', 'max_paths_to_consider': 1}, rns_instance=object(), metrics_monitor=monitor)
        selector._get_rns_paths = lambda dest_hash: [_FakePath()]
        best_path = selector.get_best_path('abcdef1234567890abcdef1234567890')
        self.assertEqual(best_path.path_id, 'path-1')

    def test_proxy_manager_registers_active_link(self):
        metrics = _FakeMetrics()
        manager = ProxyManager({'is_proxy_node': False, 'proxy_routes': []}, rns_instance=SimpleNamespace(identity=object()), metrics_monitor=metrics)
        link = _FakeLink()
        manager._handle_client_link_established(link)
        self.assertIn(link.link_id.hex(), manager.active_client_links)
        self.assertEqual(metrics.proxy_client_counts[-1], 1)

    def test_proxy_manager_requires_target_destination_name(self):
        errors = []
        manager = ProxyManager(
            {
                'is_proxy_node': False,
                'proxy_routes': [
                    {
                        'alias': 'default',
                        'entry_destination_name': 'ares.proxy.edge',
                        'exit_node_identity_hash': 'abcdef1234567890abcdef1234567890',
                    }
                ],
            },
            rns_instance=SimpleNamespace(identity=object()),
        )
        result = manager.send_via_proxy(
            'abcdef1234567890abcdef1234567890',
            b'data',
            response_callback=lambda payload, error: errors.append(error),
        )
        self.assertIsNone(result)
        self.assertEqual(errors, ['target_destination_name is required for proxying'])

    def test_retry_manager_honors_zero_retries(self):
        attempts = {'count': 0}

        def fail_once():
            attempts['count'] += 1
            raise ValueError('boom')

        manager = RetryManager({'default_max_retries': 3, 'default_delay_seconds': 0})
        with self.assertRaises(ValueError):
            manager.exec_w_retry(fail_once, max_r=0, delay_s=0, op_name='probe')
        self.assertEqual(attempts['count'], 1)