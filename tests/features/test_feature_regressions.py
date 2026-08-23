import base64
import json
import socket
import time
import unittest
import urllib.request
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from akita_ares.core.logger import setup_logging
from akita_ares.features import proxying
from akita_ares.features.monitoring import MetricsMonitor
from akita_ares.features.path_selection import PathSelector
from akita_ares.features.proxying import ProxyManager
from akita_ares.features.request_retries import RetryManager

setup_logging(level="CRITICAL", console_output=False, log_file=None)


def _reserve_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _FakePath:
    def __init__(self):
        self.path_id = "path-1"
        self.hops = 1


class _FakeLink:
    def __init__(self):
        self.link_id = b"1234567890abcdef"
        self.resource_callback = None
        self.closed_callback = None
        self.active = True

    def set_resource_callback(self, callback):
        self.resource_callback = callback

    def set_link_closed_callback(self, callback):
        self.closed_callback = callback

    def is_active(self):
        return self.active

    def close(self):
        self.active = False
        if self.closed_callback:
            self.closed_callback(self)


class _FakeMetrics:
    def __init__(self):
        self.proxy_client_counts = []
        self.proxy_route_counts = []
        self.proxy_request_outcomes = []
        self.proxy_request_durations = []
        self.proxy_request_phase_durations = []
        self.proxy_policy_denials = []

    def set_active_proxy_clients_count(self, count):
        self.proxy_client_counts.append(count)

    def set_active_proxy_routes_count(self, count):
        self.proxy_route_counts.append(count)

    def increment_proxied_packets(self, proxy_alias, direction="sent_to_proxy"):
        return None

    def increment_proxy_request_outcome(self, proxy_alias, mode, outcome):
        self.proxy_request_outcomes.append((proxy_alias, mode, outcome))

    def record_proxy_request_duration(self, proxy_alias, mode, outcome, dur_s):
        self.proxy_request_durations.append((proxy_alias, mode, outcome, dur_s))

    def record_proxy_request_phase_duration(self, proxy_alias, mode, phase, outcome, dur_s):
        self.proxy_request_phase_durations.append((proxy_alias, mode, phase, outcome, dur_s))

    def increment_proxy_policy_denial(self, proxy_alias, reason):
        self.proxy_policy_denials.append((proxy_alias, reason))


class _FakeRequestReceipt:
    READY = 0x22
    FAILED = 0xFF

    def __init__(self, response=b"pong", status=READY):
        self._response = response
        self._status = status

    def get_response(self):
        return self._response

    def get_status(self):
        return self._status


class _FakeProxyDestination:
    IN = 0
    OUT = 1
    SINGLE = 2
    GROUP = 3
    PLAIN = 4

    def __init__(self, identity, direction, destination_type, *name_parts):
        self.identity = identity
        self.direction = direction
        self.destination_type = destination_type
        self.name_parts = name_parts
        self.hash = b"\xaa" * 16
        self.hexhash = self.hash.hex()
        self.link_established_callback = None
        self.announced = False

    def set_link_established_callback(self, callback):
        self.link_established_callback = callback

    def announce(self):
        self.announced = True

    def close(self):
        return None

    @staticmethod
    def hash_from_name_and_identity(full_name, identity):
        return b"\xbb" * 16

    @staticmethod
    def app_and_aspects_from_name(full_name):
        parts = full_name.split(".")
        return parts[0], parts[1:]


class _FakeClientProxyLink:
    last_sent_message = None

    def __init__(
        self,
        destination,
        established_callback=None,
        closed_callback=None,
        owner=None,
        peer_pub_bytes=None,
        peer_sig_pub_bytes=None,
        mode=1,
    ):
        self.destination = destination
        self.link_id = b"client-proxy-link"
        self.active = True
        self.resource_callback = None
        self.closed_callback = closed_callback
        if established_callback is not None:
            established_callback(self)

    def set_resource_callback(self, callback):
        self.resource_callback = callback

    def set_link_closed_callback(self, callback):
        self.closed_callback = callback

    def send(self, data):
        message = json.loads(data.decode("utf-8"))
        _FakeClientProxyLink.last_sent_message = message
        response = {
            "version": "1.0",
            "type": "response",
            "request_id": message["request_id"],
            "payload": base64.b64encode(b"pong").decode("utf-8"),
        }
        if self.resource_callback is not None:
            resource = SimpleNamespace(data=json.dumps(response).encode("utf-8"), link=self)
            self.resource_callback(resource)
        return True

    def is_active(self):
        return self.active

    def close(self):
        self.active = False
        if self.closed_callback is not None:
            self.closed_callback(self)


class _SlowClientProxyLink:
    def __init__(
        self,
        destination,
        established_callback=None,
        closed_callback=None,
        owner=None,
        peer_pub_bytes=None,
        peer_sig_pub_bytes=None,
        mode=1,
    ):
        self.destination = destination
        self.link_id = b"slow-client-proxy-link"
        self.active = True
        self.resource_callback = None
        self.closed_callback = closed_callback

    def set_resource_callback(self, callback):
        self.resource_callback = callback

    def set_link_closed_callback(self, callback):
        self.closed_callback = callback

    def send(self, data):
        return True

    def is_active(self):
        return self.active

    def close(self):
        self.active = False
        if self.closed_callback is not None:
            self.closed_callback(self)


class _NoResponseClientProxyLink(_SlowClientProxyLink):
    def __init__(
        self,
        destination,
        established_callback=None,
        closed_callback=None,
        owner=None,
        peer_pub_bytes=None,
        peer_sig_pub_bytes=None,
        mode=1,
    ):
        super().__init__(
            destination,
            established_callback,
            closed_callback,
            owner,
            peer_pub_bytes,
            peer_sig_pub_bytes,
            mode,
        )
        if established_callback is not None:
            established_callback(self)


class _FakeTargetLink:
    last_request = None

    def __init__(
        self,
        destination,
        established_callback=None,
        closed_callback=None,
        owner=None,
        peer_pub_bytes=None,
        peer_sig_pub_bytes=None,
        mode=1,
    ):
        self.destination = destination
        self.link_id = b"target-link"
        self.active = True
        self.closed_callback = closed_callback
        if established_callback is not None:
            established_callback(self)

    def request(
        self,
        path,
        data=None,
        response_callback=None,
        failed_callback=None,
        progress_callback=None,
        timeout=None,
    ):
        _FakeTargetLink.last_request = {
            "path": path,
            "data": data,
            "timeout": timeout,
        }
        receipt = _FakeRequestReceipt(response=b"pong")
        if response_callback is not None:
            response_callback(receipt)
        return receipt

    def is_active(self):
        return self.active

    def close(self):
        self.active = False
        if self.closed_callback is not None:
            self.closed_callback(self)


class TestFeatureRegressions(unittest.TestCase):
    def test_monitoring_serves_metrics_and_health(self):
        port = _reserve_port()
        prefix = f"test_{uuid.uuid4().hex[:8]}"
        monitor = MetricsMonitor(
            {
                "prometheus_port": port,
                "listen_host": "127.0.0.1",
                "metrics_prefix": prefix,
                "enable_health_endpoint": True,
            }
        )
        monitor.start()
        try:
            time.sleep(0.1)
            metrics_body = (
                urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2)
                .read()
                .decode("utf-8")
            )
            health_body = (
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
                .read()
                .decode("utf-8")
            )
        finally:
            monitor.stop()
        self.assertIn(f"{prefix}_info", metrics_body)
        self.assertIn(
            f"# HELP {prefix}_proxy_request_duration_seconds Proxy request duration seconds",
            metrics_body,
        )
        self.assertIn(
            f"# HELP {prefix}_proxy_request_phase_duration_seconds Proxy request phase duration seconds",
            metrics_body,
        )
        self.assertEqual(health_body, "OK")

    def test_path_selection_with_metrics_does_not_raise(self):
        prefix = f"test_{uuid.uuid4().hex[:8]}"
        monitor = MetricsMonitor({"prometheus_port": _reserve_port(), "metrics_prefix": prefix})
        selector = PathSelector(
            {"default_metric": "hops", "max_paths_to_consider": 1},
            rns_instance=object(),
            metrics_monitor=monitor,
        )
        selector._get_rns_paths = lambda dest_hash: [_FakePath()]
        best_path = selector.get_best_path("abcdef1234567890abcdef1234567890")
        self.assertEqual(best_path.path_id, "path-1")

    def test_path_selection_removes_stale_metric_labels_on_stop(self):
        removed = []
        monitor = SimpleNamespace(
            path_selection_chosen_metric_value=SimpleNamespace(
                remove=lambda *labels: removed.append(labels)
            )
        )
        selector = PathSelector({"default_metric": "hops"}, metrics_monitor=monitor)
        destination_hash = "abcdef1234567890abcdef1234567890"
        path = _FakePath()
        selector.known_paths[destination_hash] = [path]
        selector.path_metrics_cache[path.path_id] = {
            "hops": {"value": 1.0, "timestamp": time.monotonic()}
        }

        selector.stop()

        self.assertEqual(removed, [(destination_hash, "hops")])

    def test_proxy_manager_registers_active_link(self):
        metrics = _FakeMetrics()
        manager = ProxyManager(
            {"is_proxy_node": False, "proxy_routes": []},
            rns_instance=SimpleNamespace(identity=object()),
            metrics_monitor=metrics,
        )
        link = _FakeLink()
        manager._handle_client_link_established(link)
        self.assertIn(link.link_id.hex(), manager.active_client_links)
        self.assertEqual(metrics.proxy_client_counts[-1], 1)

    def test_proxy_manager_requires_target_destination_name(self):
        errors = []
        manager = ProxyManager(
            {
                "is_proxy_node": False,
                "proxy_routes": [
                    {
                        "alias": "default",
                        "entry_destination_name": "ares.proxy.edge",
                        "exit_node_identity_hash": "abcdef1234567890abcdef1234567890",
                    }
                ],
            },
            rns_instance=SimpleNamespace(identity=object()),
        )
        result = manager.send_via_proxy(
            "abcdef1234567890abcdef1234567890",
            b"data",
            response_callback=lambda payload, error: errors.append(error),
        )
        self.assertIsNone(result)
        self.assertEqual(errors, ["target_destination_name is required for proxying"])

    def test_proxy_manager_requires_request_path_for_response_mode(self):
        errors = []
        manager = ProxyManager(
            {
                "is_proxy_node": False,
                "proxy_routes": [
                    {
                        "alias": "default",
                        "entry_destination_name": "ares.proxy.edge",
                        "exit_node_identity_hash": "abcdef1234567890abcdef1234567890",
                        "allow_all_targets": True,
                    }
                ],
            },
            rns_instance=SimpleNamespace(identity=object()),
        )
        result = manager.send_via_proxy(
            "abcdef1234567890abcdef1234567890",
            b"data",
            response_callback=lambda payload, error: errors.append(error),
            target_destination_name="app_name.service_behind_firewall.data_service",
        )
        self.assertIsNone(result)
        self.assertEqual(errors, ["request_path is required when response_callback is provided"])

    def test_proxy_manager_request_response_mode_returns_payload(self):
        payloads = []
        errors = []
        metrics = _FakeMetrics()
        manager = ProxyManager(
            {
                "is_proxy_node": False,
                "proxy_routes": [
                    {
                        "alias": "default",
                        "entry_destination_name": "ares.proxy.edge",
                        "exit_node_identity_hash": "abcdef1234567890abcdef1234567890",
                        "allow_all_targets": True,
                    }
                ],
                "default_request_timeout_seconds": 12.5,
            },
            rns_instance=SimpleNamespace(identity=object()),
            metrics_monitor=metrics,
        )
        _FakeClientProxyLink.last_sent_message = None
        with (
            patch("akita_ares.features.proxying.Identity.recall", return_value=SimpleNamespace()),
            patch("akita_ares.features.proxying.Destination", _FakeProxyDestination),
            patch("akita_ares.features.proxying.Link", _FakeClientProxyLink),
        ):
            request_id = manager.send_via_proxy(
                "abcdef1234567890abcdef1234567890",
                b"data",
                response_callback=lambda payload, error: (
                    payloads.append(payload),
                    errors.append(error),
                ),
                target_destination_name="app_name.service_behind_firewall.data_service",
                request_path="/status",
            )
        self.assertIsNotNone(request_id)
        self.assertEqual(payloads, [b"pong"])
        self.assertEqual(errors, [None])
        self.assertEqual(_FakeClientProxyLink.last_sent_message["request_timeout_s"], 12.5)
        self.assertEqual(metrics.proxy_request_outcomes[-1], ("default", "request", "success"))
        self.assertEqual(metrics.proxy_request_durations[-1][:3], ("default", "request", "success"))
        self.assertGreaterEqual(metrics.proxy_request_durations[-1][3], 0)
        self.assertTrue(
            any(
                item[:4] == ("default", "request", "proxy_link_setup", "success")
                for item in metrics.proxy_request_phase_durations
            )
        )
        self.assertTrue(
            any(
                item[:4] == ("default", "request", "proxy_roundtrip", "success")
                for item in metrics.proxy_request_phase_durations
            )
        )

    def test_proxy_manager_rejects_target_outside_route_policy(self):
        errors = []
        metrics = _FakeMetrics()
        manager = ProxyManager(
            {
                "is_proxy_node": False,
                "proxy_routes": [
                    {
                        "alias": "secure_exit_1",
                        "entry_destination_name": "ares.proxy.entry.exit1",
                        "exit_node_identity_hash": "abcdef1234567890abcdef1234567890",
                        "target_network_prefix": "app_name.service_behind_firewall",
                        "allow_all_targets": False,
                        "allowed_target_aspects": ["data_service"],
                    }
                ],
            },
            rns_instance=SimpleNamespace(identity=object()),
            metrics_monitor=metrics,
        )
        result = manager.send_via_proxy(
            "abcdef1234567890abcdef1234567890",
            b"data",
            response_callback=lambda payload, error: errors.append(error),
            target_destination_name="otherapp.public.control",
            request_path="/status",
        )
        self.assertIsNone(result)
        self.assertEqual(
            errors,
            [
                "target_destination_name 'otherapp.public.control' is outside proxy route 'secure_exit_1' prefix 'app_name.service_behind_firewall'"
            ],
        )
        self.assertEqual(metrics.proxy_policy_denials, [("secure_exit_1", "prefix")])

    def test_proxy_manager_prefers_most_specific_matching_route(self):
        manager = ProxyManager(
            {
                "is_proxy_node": False,
                "proxy_routes": [
                    {
                        "alias": "catch_all",
                        "entry_destination_name": "ares.proxy.entry.catchall",
                        "exit_node_identity_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "allow_all_targets": True,
                    },
                    {
                        "alias": "secure_exit_1",
                        "entry_destination_name": "ares.proxy.entry.exit1",
                        "exit_node_identity_hash": "abcdef1234567890abcdef1234567890",
                        "target_network_prefix": "app_name.service_behind_firewall",
                        "allow_all_targets": False,
                        "allowed_target_aspects": ["data_service"],
                    },
                ],
            },
            rns_instance=SimpleNamespace(identity=object()),
        )
        route, error = manager._select_proxy_route(
            None, "app_name.service_behind_firewall.data_service"
        )
        self.assertIsNone(error)
        self.assertEqual(route["alias"], "secure_exit_1")

    def test_proxy_manager_forwards_request_via_target_link(self):
        metrics = _FakeMetrics()
        client_link = SimpleNamespace(
            link_id=b"client-link",
            sent_messages=[],
            send=lambda payload: client_link.sent_messages.append(
                json.loads(payload.decode("utf-8"))
            ),
            is_active=lambda: True,
        )
        with patch("akita_ares.features.proxying.Destination", _FakeProxyDestination):
            manager = ProxyManager(
                {
                    "is_proxy_node": True,
                    "proxy_routes": [],
                    "default_request_timeout_seconds": 9.5,
                    "allow_all_targets_on_proxy_node": True,
                },
                rns_instance=SimpleNamespace(identity=object()),
                metrics_monitor=metrics,
            )
        resource = SimpleNamespace(
            data=json.dumps(
                {
                    "version": "1.0",
                    "type": "request",
                    "request_id": "req-1",
                    "target_destination_hash": "abcdef1234567890abcdef1234567890",
                    "target_destination_name": "app_name.service_behind_firewall.data_service",
                    "request_path": "/status",
                    "payload": base64.b64encode(b"data").decode("utf-8"),
                }
            ).encode("utf-8")
        )
        manager._build_outbound_destination = lambda target_hash, target_name: (
            SimpleNamespace(hash=b"\xaa" * 16, hexhash="aa" * 16),
            None,
        )
        _FakeTargetLink.last_request = None
        with patch("akita_ares.features.proxying.Link", _FakeTargetLink):
            manager._handle_proxied_request_on_link(resource, client_link)
        self.assertEqual(_FakeTargetLink.last_request["path"], "/status")
        self.assertEqual(_FakeTargetLink.last_request["data"], b"data")
        self.assertEqual(_FakeTargetLink.last_request["timeout"], 9.5)
        self.assertEqual(client_link.sent_messages[0]["request_id"], "req-1")
        self.assertEqual(base64.b64decode(client_link.sent_messages[0]["payload"]), b"pong")
        self.assertEqual(
            metrics.proxy_request_outcomes[-1], ("proxy_node_service", "request", "success")
        )
        self.assertEqual(
            metrics.proxy_request_durations[-1][:3], ("proxy_node_service", "request", "success")
        )
        self.assertGreaterEqual(metrics.proxy_request_durations[-1][3], 0)
        self.assertTrue(
            any(
                item[:4] == ("proxy_node_service", "request", "target_link_setup", "success")
                for item in metrics.proxy_request_phase_durations
            )
        )
        self.assertTrue(
            any(
                item[:4] == ("proxy_node_service", "request", "target_request_service", "success")
                for item in metrics.proxy_request_phase_durations
            )
        )

    def test_proxy_manager_records_timeout_metric_when_proxy_link_setup_times_out(self):
        errors = []
        metrics = _FakeMetrics()
        manager = ProxyManager(
            {
                "is_proxy_node": False,
                "proxy_routes": [
                    {
                        "alias": "default",
                        "entry_destination_name": "ares.proxy.edge",
                        "exit_node_identity_hash": "abcdef1234567890abcdef1234567890",
                        "allow_all_targets": True,
                    }
                ],
            },
            rns_instance=SimpleNamespace(identity=object()),
            metrics_monitor=metrics,
        )
        with (
            patch("akita_ares.features.proxying.Identity.recall", return_value=SimpleNamespace()),
            patch("akita_ares.features.proxying.Destination", _FakeProxyDestination),
            patch("akita_ares.features.proxying.Link", _SlowClientProxyLink),
        ):
            request_id = manager.send_via_proxy(
                "abcdef1234567890abcdef1234567890",
                b"data",
                response_callback=lambda payload, error: errors.append(error),
                target_destination_name="app_name.service_behind_firewall.data_service",
                request_path="/status",
                timeout_s=0.01,
            )
        self.assertIsNone(request_id)
        self.assertEqual(errors, ["timeout establishing link to proxy server"])
        self.assertEqual(metrics.proxy_request_outcomes[-1], ("default", "request", "timeout"))
        self.assertEqual(metrics.proxy_request_durations[-1][:3], ("default", "request", "timeout"))
        self.assertGreaterEqual(metrics.proxy_request_durations[-1][3], 0)
        self.assertTrue(
            any(
                item[:4] == ("default", "request", "proxy_link_setup", "timeout")
                for item in metrics.proxy_request_phase_durations
            )
        )

    def test_proxy_manager_times_out_when_proxy_never_responds(self):
        errors = []
        metrics = _FakeMetrics()
        manager = ProxyManager(
            {
                "is_proxy_node": False,
                "proxy_routes": [
                    {
                        "alias": "default",
                        "entry_destination_name": "ares.proxy.edge",
                        "exit_node_identity_hash": "abcdef1234567890abcdef1234567890",
                        "allow_all_targets": True,
                    }
                ],
            },
            rns_instance=SimpleNamespace(identity=object()),
            metrics_monitor=metrics,
        )
        with (
            patch("akita_ares.features.proxying.Identity.recall", return_value=SimpleNamespace()),
            patch("akita_ares.features.proxying.Destination", _FakeProxyDestination),
            patch("akita_ares.features.proxying.Link", _NoResponseClientProxyLink),
        ):
            request_id = manager.send_via_proxy(
                "abcdef1234567890abcdef1234567890",
                b"data",
                response_callback=lambda payload, error: errors.append(error),
                target_destination_name="app_name.service_behind_firewall.data_service",
                request_path="/status",
                request_timeout_s=0.01,
            )
            self.assertIsNotNone(request_id)
            time.sleep(0.05)
        self.assertEqual(errors, [f"proxy response timeout for request {request_id}"])
        self.assertEqual(metrics.proxy_request_outcomes[-1], ("default", "request", "timeout"))

    def test_proxy_node_denies_targets_without_inbound_policy(self):
        client_link = SimpleNamespace(
            link_id=b"client-link",
            sent_messages=[],
            send=lambda payload: client_link.sent_messages.append(
                json.loads(payload.decode("utf-8"))
            ),
            is_active=lambda: True,
        )
        manager = ProxyManager(
            {"is_proxy_node": False, "proxy_routes": []},
            rns_instance=SimpleNamespace(identity=object()),
        )
        resource = SimpleNamespace(
            data=json.dumps(
                {
                    "version": "1.0",
                    "type": "request",
                    "request_id": "req-denied",
                    "target_destination_hash": "abcdef1234567890abcdef1234567890",
                    "target_destination_name": "private.service.control",
                    "request_path": "/status",
                    "payload": base64.b64encode(b"data").decode("utf-8"),
                }
            ).encode("utf-8")
        )
        manager._handle_proxied_request_on_link(resource, client_link)
        self.assertEqual(
            client_link.sent_messages[0]["error"], "target_destination_denied_by_proxy_node_policy"
        )

    def test_proxy_manager_uses_rns_resource_when_link_has_no_send_adapter(self):
        captured = {}

        class ResourceFactory:
            def __init__(self, data, link, callback=None, timeout=None):
                captured.update(data=data, link=link, callback=callback, timeout=timeout)

        manager = ProxyManager(
            {"is_proxy_node": False, "proxy_routes": []},
            rns_instance=SimpleNamespace(identity=object()),
        )
        link = SimpleNamespace()

        def callback(resource):
            return None

        with patch("akita_ares.features.proxying.Resource", ResourceFactory):
            receipt = manager._send_link_payload(link, b"payload", callback, timeout=4.5)
        self.assertIsInstance(receipt, ResourceFactory)
        self.assertEqual(captured["data"], b"payload")
        self.assertIs(captured["link"], link)
        self.assertIs(captured["callback"], callback)
        self.assertEqual(captured["timeout"], 4.5)

    def test_proxy_manager_rejects_non_string_destination_hash(self):
        errors = []
        manager = ProxyManager(
            {"is_proxy_node": False, "proxy_routes": []},
            rns_instance=SimpleNamespace(identity=object()),
        )
        result = manager.send_via_proxy(
            None,
            b"data",
            response_callback=lambda payload, error: errors.append(error),
            target_destination_name="app.service",
            request_path="/status",
        )
        self.assertIsNone(result)
        self.assertEqual(errors, ["invalid target_destination_hash format: None"])

    def test_proxy_manager_rejects_non_bytes_payload_without_allocating(self):
        errors = []
        manager = ProxyManager(
            {
                "is_proxy_node": False,
                "proxy_routes": [
                    {
                        "alias": "default",
                        "entry_destination_name": "ares.proxy.edge",
                        "exit_node_identity_hash": "abcdef1234567890abcdef1234567890",
                        "allow_all_targets": True,
                    }
                ],
            },
            rns_instance=SimpleNamespace(identity=object()),
        )
        result = manager.send_via_proxy(
            "abcdef1234567890abcdef1234567890",
            10_000_000,
            response_callback=lambda payload, error: errors.append(error),
            target_destination_name="app.service",
            request_path="/status",
        )
        self.assertIsNone(result)
        self.assertEqual(errors, ["proxy payload must be bytes-like"])

    def test_proxy_node_initialization_failure_is_fatal(self):
        with patch(
            "akita_ares.features.proxying.Destination",
            side_effect=RuntimeError("destination unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Failed to create proxy service destination"):
                ProxyManager(
                    {
                        "is_proxy_node": True,
                        "allow_all_targets_on_proxy_node": True,
                    },
                    rns_instance=SimpleNamespace(identity=object()),
                )

    def test_proxy_manager_configures_real_resource_conclusion_callback(self):
        link = SimpleNamespace(strategy=None, callback=None)
        link.set_resource_strategy = lambda strategy: setattr(link, "strategy", strategy)
        link.set_resource_concluded_callback = lambda callback: setattr(link, "callback", callback)

        def callback(resource):
            return None

        manager = ProxyManager(
            {"is_proxy_node": False, "proxy_routes": []},
            rns_instance=SimpleNamespace(identity=object()),
        )
        manager._configure_link_resource_receiver(link, callback)
        self.assertEqual(link.strategy, proxying.Link.ACCEPT_ALL)
        self.assertIs(link.callback, callback)

    def test_retry_manager_honors_zero_retries(self):
        attempts = {"count": 0}

        def fail_once():
            attempts["count"] += 1
            raise ValueError("boom")

        manager = RetryManager({"default_max_retries": 3, "default_delay_seconds": 0})
        with self.assertRaises(ValueError):
            manager.exec_w_retry(fail_once, max_r=0, delay_s=0, op_name="probe")
        self.assertEqual(attempts["count"], 1)

    def test_retry_manager_retries_transient_io_errors(self):
        attempts = {"count": 0}

        def transient_operation():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise TimeoutError("temporary timeout")
            return "ok"

        manager = RetryManager({"default_max_retries": 2, "default_delay_seconds": 0})
        self.assertEqual(manager.exec_w_retry(transient_operation), "ok")
        self.assertEqual(attempts["count"], 2)

    def test_retry_manager_does_not_retry_programming_errors_by_default(self):
        attempts = {"count": 0}

        def invalid_operation():
            attempts["count"] += 1
            raise ValueError("invalid input")

        manager = RetryManager({"default_max_retries": 3, "default_delay_seconds": 0})
        with self.assertRaises(ValueError):
            manager.exec_w_retry(invalid_operation)
        self.assertEqual(attempts["count"], 1)
