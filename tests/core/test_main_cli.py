import io
import json
import unittest

from unittest.mock import patch

from akita_ares.cli.main_cli import handle_status_command, parse_args


class TestMainCli(unittest.TestCase):
    def test_parse_args_defaults_to_start_with_global_options(self):
        args = parse_args(['--loglevel', 'CRITICAL'])
        self.assertEqual(args.command, 'start')
        self.assertEqual(args.loglevel, 'CRITICAL')
        self.assertEqual(args.func.__name__, 'handle_start_command')

    def test_parse_args_preserves_explicit_subcommand(self):
        args = parse_args(['--config', 'config.json', 'status'])
        self.assertEqual(args.command, 'status')
        self.assertEqual(args.config, 'config.json')
        self.assertEqual(args.func.__name__, 'handle_status_command')

    def test_status_command_includes_runtime_metrics_snapshot(self):
        args = parse_args(['--config', 'config.json', 'status'])
        config = {
            'monitoring': {'enabled': True, 'listen_host': '127.0.0.1', 'prometheus_port': 9876, 'metrics_prefix': 'ares'},
            'request_retries': {'enabled': True},
            'path_selection': {'enabled': False},
            'destination_proxying': {
                'enabled': True,
                'is_proxy_node': False,
                'proxy_routes': [{'alias': 'default'}],
                'default_request_timeout_seconds': 15,
                'max_payload_size_bytes': 1048576,
                'listen_on_aspect': 'proxy_service',
            },
        }
        metrics_body = '\n'.join([
            'ares_active_features_count 2',
            'ares_active_proxy_routes_count 1',
            'ares_active_proxy_clients_count 0',
            'ares_retry_executions_total{operation_name="probe"} 4',
            'ares_retry_successes_total{operation_name="probe"} 3',
            'ares_retry_successes_on_retry_total{operation_name="probe"} 2',
            'ares_retry_failures_total{operation_name="probe"} 1',
            'ares_retry_operation_duration_seconds_sum{operation_name="probe"} 1.2',
            'ares_retry_operation_duration_seconds_count{operation_name="probe"} 4',
            'ares_path_selection_evaluations_total 7',
            'ares_path_selection_chosen_metric_value{destination_hash="abcd1234",metric_type="rtt"} 0.42',
            'ares_proxy_request_outcomes_total{proxy_alias="default",mode="request",outcome="success"} 5',
            'ares_proxy_policy_denials_total{proxy_alias="default",reason="prefix"} 2',
            'ares_proxy_request_duration_seconds_sum{proxy_alias="default",mode="request",outcome="success"} 2.5',
            'ares_proxy_request_duration_seconds_count{proxy_alias="default",mode="request",outcome="success"} 5',
            'ares_proxy_request_phase_duration_seconds_sum{proxy_alias="default",mode="request",phase="proxy_link_setup",outcome="success"} 0.5',
            'ares_proxy_request_phase_duration_seconds_count{proxy_alias="default",mode="request",phase="proxy_link_setup",outcome="success"} 5',
        ])
        stdout = io.StringIO()
        with patch('akita_ares.core.config_manager.ConfigManager') as config_manager_cls, \
             patch('akita_ares.cli.main_cli.urllib.request.urlopen') as urlopen, \
             patch('sys.stdout', stdout):
            config_manager_cls.return_value.get_config.return_value = config
            urlopen.return_value.read.return_value = metrics_body.encode('utf-8')
            with self.assertRaises(SystemExit) as exc:
                handle_status_command(args, None)
        self.assertEqual(exc.exception.code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload['proxy_mode'], 'client')
        self.assertEqual(payload['proxy']['route_count'], 1)
        self.assertTrue(payload['runtime_metrics']['available'])
        self.assertEqual(payload['runtime_metrics']['active_features_count'], 2)
        self.assertEqual(payload['runtime_metrics']['retry_operations'][0]['operation_name'], 'probe')
        self.assertEqual(payload['runtime_metrics']['retry_operations'][0]['executions'], 4)
        self.assertEqual(payload['runtime_metrics']['retry_operations'][0]['avg_seconds'], 0.3)
        self.assertEqual(payload['runtime_metrics']['path_selection']['evaluations_total'], 7)
        self.assertEqual(payload['runtime_metrics']['path_selection']['chosen_metric_values'][0]['metric_type'], 'rtt')
        self.assertEqual(payload['runtime_metrics']['proxy_request_outcomes'][0]['count'], 5)
        self.assertEqual(payload['runtime_metrics']['proxy_request_latencies'][0]['avg_seconds'], 0.5)
        self.assertEqual(payload['runtime_metrics']['proxy_request_phase_latencies'][0]['phase'], 'proxy_link_setup')
        self.assertEqual(payload['health_summary']['overall'], 'degraded')
        self.assertEqual(payload['health_summary']['issues'][0]['code'], 'retry_failures_present')
        self.assertEqual(payload['health_summary']['issues'][0]['count'], 1)
        self.assertEqual(payload['health_summary']['notices'][0]['code'], 'retries_were_needed')

    def test_status_command_reports_metrics_fetch_error(self):
        args = parse_args(['--config', 'config.json', 'status'])
        config = {
            'monitoring': {'enabled': True, 'listen_host': '127.0.0.1', 'prometheus_port': 9876, 'metrics_prefix': 'ares'},
            'destination_proxying': {'enabled': False},
        }
        stdout = io.StringIO()
        with patch('akita_ares.core.config_manager.ConfigManager') as config_manager_cls, \
             patch('akita_ares.cli.main_cli.urllib.request.urlopen', side_effect=OSError('connection refused')), \
             patch('sys.stdout', stdout):
            config_manager_cls.return_value.get_config.return_value = config
            with self.assertRaises(SystemExit) as exc:
                handle_status_command(args, None)
        self.assertEqual(exc.exception.code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload['runtime_metrics']['available'])
        self.assertIn('connection refused', payload['runtime_metrics']['error'])
        self.assertEqual(payload['health_summary']['overall'], 'unknown')
        self.assertEqual(payload['health_summary']['issues'][0]['code'], 'metrics_unavailable')