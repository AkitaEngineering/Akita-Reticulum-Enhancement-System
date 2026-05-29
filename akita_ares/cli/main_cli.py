import argparse, os, re, sys, json, urllib.request 
from akita_ares import VERSION 
DEFAULT_CONFIG_PATH_CLI = os.path.join(os.path.dirname(__file__),"..","..","examples","sample_config.json")
DEFAULT_SCHEMA_PATH_CLI = os.path.join(os.path.dirname(__file__),"..","..","examples","config_schema.json")
PROMETHEUS_LINE_RE = re.compile(r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)$')
PROMETHEUS_LABEL_RE = re.compile(r'(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:\\.|[^"\\])*)"')

def _resolve_schema_path(args, cfg_path):
    schema_path = args.schema
    pre_conf = {}
    if not schema_path:
        try:
            if os.path.exists(cfg_path):
                with open(cfg_path,'r', encoding='utf-8') as f:
                    pre_conf=json.load(f)
            schema_path=pre_conf.get('ares_core',{}).get('config_schema_path')
        except Exception:
            pre_conf = {}
    if not schema_path:
        schema_path=DEFAULT_SCHEMA_PATH_CLI
    if not os.path.isabs(schema_path) and '~' not in schema_path:
         if args.schema: pass 
         elif pre_conf.get('ares_core',{}).get('config_schema_path'): schema_path=os.path.join(os.path.dirname(cfg_path),schema_path)
         else: schema_path=DEFAULT_SCHEMA_PATH_CLI
    return schema_path

def _rns_available():
    try:
        import RNS  # noqa: F401
        return True
    except ImportError:
        return False

def _normalize_status_metrics_host(listen_host):
    if listen_host in (None, '', '0.0.0.0'): return '127.0.0.1'
    if listen_host == '::': return '::1'
    return listen_host

def _parse_prometheus_labels(labels_blob):
    labels = {}
    for match in PROMETHEUS_LABEL_RE.finditer(labels_blob or ''):
        labels[match.group('name')] = bytes(match.group('value'), 'utf-8').decode('unicode_escape')
    return labels

def _parse_prometheus_metrics(metrics_body):
    parsed = {}
    for raw_line in metrics_body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'): continue
        match = PROMETHEUS_LINE_RE.match(line)
        if not match: continue
        parsed.setdefault(match.group('name'), []).append((_parse_prometheus_labels(match.group('labels')), float(match.group('value'))))
    return parsed

def _normalize_metric_value(value):
    return int(value) if float(value).is_integer() else value

def _metric_gauge_value(metrics, metric_name):
    series = metrics.get(metric_name, [])
    if not series: return None
    return _normalize_metric_value(series[0][1])

def _summarize_counter_series(metrics, metric_name, label_keys):
    return [
        {**{key: labels.get(key) for key in label_keys if key in labels}, 'count': _normalize_metric_value(value)}
        for labels, value in sorted(metrics.get(metric_name, []), key=lambda item: tuple(item[0].get(key, '') for key in label_keys))
    ]

def _summarize_histogram_series(metrics, base_metric_name, label_keys):
    counts = {tuple((key, labels.get(key)) for key in label_keys if key in labels): value for labels, value in metrics.get(f'{base_metric_name}_count', [])}
    sums = {tuple((key, labels.get(key)) for key in label_keys if key in labels): value for labels, value in metrics.get(f'{base_metric_name}_sum', [])}
    summaries = []
    for key in sorted(set(counts) | set(sums), key=lambda item: tuple(value or '' for _, value in item)):
        labels = dict(key); count = counts.get(key, 0.0); total = sums.get(key, 0.0)
        summaries.append({
            **{label_key: labels.get(label_key) for label_key in label_keys if label_key in labels},
            'count': _normalize_metric_value(count),
            'avg_seconds': round(total / count, 6) if count else None,
        })
    return summaries

def _summarize_gauge_series(metrics, metric_name, label_keys, value_key='value'):
    return [
        {**{key: labels.get(key) for key in label_keys if key in labels}, value_key: _normalize_metric_value(value)}
        for labels, value in sorted(metrics.get(metric_name, []), key=lambda item: tuple(item[0].get(key, '') for key in label_keys))
    ]

def _summarize_retry_metrics(metrics, prefix):
    operations = {}

    def _ensure(operation_name):
        return operations.setdefault(operation_name, {
            'operation_name': operation_name,
            'executions': 0,
            'successes': 0,
            'successes_on_retry': 0,
            'failures': 0,
            'avg_seconds': None,
        })

    for labels, value in metrics.get(f'{prefix}_retry_executions_total', []):
        _ensure(labels.get('operation_name', 'unknown'))['executions'] = _normalize_metric_value(value)
    for labels, value in metrics.get(f'{prefix}_retry_successes_total', []):
        _ensure(labels.get('operation_name', 'unknown'))['successes'] = _normalize_metric_value(value)
    for labels, value in metrics.get(f'{prefix}_retry_successes_on_retry_total', []):
        _ensure(labels.get('operation_name', 'unknown'))['successes_on_retry'] = _normalize_metric_value(value)
    for labels, value in metrics.get(f'{prefix}_retry_failures_total', []):
        _ensure(labels.get('operation_name', 'unknown'))['failures'] = _normalize_metric_value(value)
    for summary in _summarize_histogram_series(metrics, f'{prefix}_retry_operation_duration_seconds', ['operation_name']):
        _ensure(summary['operation_name'])['avg_seconds'] = summary['avg_seconds']

    return [operations[name] for name in sorted(operations)]

def _summarize_path_selection_metrics(metrics, prefix):
    return {
        'evaluations_total': _metric_gauge_value(metrics, f'{prefix}_path_selection_evaluations_total'),
        'chosen_metric_values': _summarize_gauge_series(metrics, f'{prefix}_path_selection_chosen_metric_value', ['destination_hash', 'metric_type'], value_key='metric_value'),
    }

def _sum_metric_counts(entries, count_key='count', predicate=None):
    total = 0
    for entry in entries or []:
        if predicate is None or predicate(entry):
            total += entry.get(count_key, 0) or 0
    return _normalize_metric_value(total)

def _fetch_runtime_metrics(config):
    monitoring_config = config.get('monitoring', {})
    if not monitoring_config.get('enabled', True): return {'available': False, 'reason': 'monitoring_disabled'}
    listen_host = _normalize_status_metrics_host(monitoring_config.get('listen_host', '127.0.0.1'))
    port = monitoring_config.get('prometheus_port', 9876)
    prefix = monitoring_config.get('metrics_prefix', 'ares')
    endpoint = f'http://{listen_host}:{port}/metrics'
    try:
        response = urllib.request.urlopen(endpoint, timeout=1)
        metrics_body = response.read().decode('utf-8')
    except Exception as exc:
        return {'available': False, 'endpoint': endpoint, 'error': str(exc)}

    metrics = _parse_prometheus_metrics(metrics_body)
    return {
        'available': True,
        'endpoint': endpoint,
        'active_features_count': _metric_gauge_value(metrics, f'{prefix}_active_features_count'),
        'active_proxy_routes_count': _metric_gauge_value(metrics, f'{prefix}_active_proxy_routes_count'),
        'active_proxy_clients_count': _metric_gauge_value(metrics, f'{prefix}_active_proxy_clients_count'),
        'retry_operations': _summarize_retry_metrics(metrics, prefix),
        'path_selection': _summarize_path_selection_metrics(metrics, prefix),
        'proxy_request_outcomes': _summarize_counter_series(metrics, f'{prefix}_proxy_request_outcomes_total', ['proxy_alias', 'mode', 'outcome']),
        'proxy_policy_denials': _summarize_counter_series(metrics, f'{prefix}_proxy_policy_denials_total', ['proxy_alias', 'reason']),
        'proxy_request_latencies': _summarize_histogram_series(metrics, f'{prefix}_proxy_request_duration_seconds', ['proxy_alias', 'mode', 'outcome']),
        'proxy_request_phase_latencies': _summarize_histogram_series(metrics, f'{prefix}_proxy_request_phase_duration_seconds', ['proxy_alias', 'mode', 'phase', 'outcome']),
    }

def _build_status_health_summary(config, runtime_metrics):
    issues = []
    notices = []
    monitoring_enabled = bool(config.get('monitoring', {}).get('enabled', True))

    if monitoring_enabled and not runtime_metrics.get('available'):
        issues.append({
            'code': 'metrics_unavailable',
            'severity': 'warning',
            'message': 'Live monitoring metrics were not reachable for status inspection.',
        })
        return {
            'overall': 'unknown',
            'issues': issues,
            'notices': notices,
        }

    retry_failures = _sum_metric_counts(runtime_metrics.get('retry_operations', []), count_key='failures')
    if retry_failures:
        issues.append({
            'code': 'retry_failures_present',
            'severity': 'warning',
            'count': retry_failures,
            'message': f'{retry_failures} retry failures have been recorded.',
        })

    retries_needed = _sum_metric_counts(runtime_metrics.get('retry_operations', []), count_key='successes_on_retry')
    if retries_needed:
        notices.append({
            'code': 'retries_were_needed',
            'count': retries_needed,
            'message': f'{retries_needed} operations succeeded only after one or more retries.',
        })

    proxy_problem_outcomes = _sum_metric_counts(
        runtime_metrics.get('proxy_request_outcomes', []),
        predicate=lambda entry: entry.get('outcome') not in (None, 'success'),
    )
    if proxy_problem_outcomes:
        issues.append({
            'code': 'proxy_request_errors_present',
            'severity': 'warning',
            'count': proxy_problem_outcomes,
            'message': f'{proxy_problem_outcomes} non-success proxy request outcomes have been recorded.',
        })

    proxy_policy_denials = _sum_metric_counts(runtime_metrics.get('proxy_policy_denials', []))
    if proxy_policy_denials:
        notices.append({
            'code': 'proxy_policy_denials_observed',
            'count': proxy_policy_denials,
            'message': f'{proxy_policy_denials} proxy policy denials have been recorded.',
        })

    if bool(config.get('path_selection', {}).get('enabled', False)) and runtime_metrics.get('available'):
        path_evaluations = runtime_metrics.get('path_selection', {}).get('evaluations_total')
        if path_evaluations == 0:
            notices.append({
                'code': 'path_selection_idle',
                'message': 'Path selection is enabled but has not evaluated any paths yet.',
            })

    return {
        'overall': 'degraded' if issues else 'ok',
        'issues': issues,
        'notices': notices,
    }

def parse_args(args=None):
    parser = argparse.ArgumentParser(description="ARES", formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--version', action='version', version=f'%(prog)s {VERSION}')
    parser.add_argument('--config', type=str, help=f"Path to ARES JSON config. Default: {DEFAULT_CONFIG_PATH_CLI}")
    parser.add_argument('--schema', type=str, help=f"Path to ARES JSON schema. Default: {DEFAULT_SCHEMA_PATH_CLI}")
    parser.add_argument('--loglevel', type=str, choices=['DEBUG','INFO','WARNING','ERROR','CRITICAL'], help="Override config log level.")
    subparsers = parser.add_subparsers(dest='command', title='Available commands')
    start_parser = subparsers.add_parser('start', help="Start ARES (default action).")
    start_parser.set_defaults(func=handle_start_command)
    configtest_parser = subparsers.add_parser('configtest', help="Validate ARES config.")
    configtest_parser.set_defaults(func=handle_configtest_command)
    status_parser = subparsers.add_parser('status', help="Show ARES status.")
    status_parser.set_defaults(func=handle_status_command)
    args_list = args if args is not None else sys.argv[1:]
    parsed_args, remaining = parser.parse_known_args(args_list)
    if parsed_args.command is None:
        parsed_args.command = 'start'
        parsed_args.func = handle_start_command
        if remaining:
            parser.error(f"unrecognized arguments: {' '.join(remaining)}")
        return parsed_args
    if remaining:
        parser.error(f"unrecognized arguments: {' '.join(remaining)}")
    return parsed_args
def handle_start_command(args, app_class): print(f"CLI: Preparing to start ARES...") 
def handle_configtest_command(args, app_class):
    from akita_ares.core.config_manager import ConfigManager; from akita_ares.core.logger import setup_logging, get_logger; import jsonschema
    setup_logging(level=args.loglevel or 'INFO', console_output=True, log_file=None); logger = get_logger("ConfigTest"); logger.info("Performing config test...")
    cfg_path = args.config or DEFAULT_CONFIG_PATH_CLI; schema_path = _resolve_schema_path(args, cfg_path)
    logger.info(f"Testing config file: {os.path.abspath(cfg_path)}")
    if schema_path and os.path.exists(os.path.expanduser(schema_path)): logger.info(f"Using schema: {os.path.abspath(os.path.expanduser(schema_path))}")
    elif schema_path: logger.warning(f"Schema file not found: {os.path.expanduser(schema_path)}.")
    else: logger.info("No schema specified.")
    try:
        manager = ConfigManager(config_fp=cfg_path, schema_fp=schema_path)
        if not manager.config and os.path.exists(cfg_path): logger.error("Config test FAILED: File exists but failed load/parse."); sys.exit(1)
        elif not os.path.exists(cfg_path): logger.error(f"Config test FAILED: File not found: {cfg_path}"); sys.exit(1)
        if manager.schema: logger.info("Config test successful: Parsed and validated.")
        else: logger.info("Config test successful: Parsed (schema validation skipped/failed load).")
        sys.exit(0)
    except jsonschema.exceptions.ValidationError as e: logger.error(f"Config validation FAILED: {e.message} path {list(e.path)}"); sys.exit(1)
    except Exception as e: logger.error(f"Config test FAILED: {e}"); sys.exit(1)
def handle_status_command(args, app_class):
    from akita_ares.core.config_manager import ConfigManager

    cfg_path = args.config or DEFAULT_CONFIG_PATH_CLI
    schema_path = _resolve_schema_path(args, cfg_path)

    try:
        manager = ConfigManager(config_fp=cfg_path, schema_fp=schema_path)
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}, indent=2))
        sys.exit(1)

    config = manager.get_config()
    proxy_config = config.get('destination_proxying', {})
    monitoring_config = config.get('monitoring', {})
    status = {
        "status": "ok",
        "config_path": os.path.abspath(cfg_path),
        "schema_path": os.path.abspath(os.path.expanduser(schema_path)) if schema_path else None,
        "rns_available": _rns_available(),
        "features": {
            "monitoring": bool(config.get('monitoring', {}).get('enabled', True)),
            "request_retries": bool(config.get('request_retries', {}).get('enabled', False)),
            "path_selection": bool(config.get('path_selection', {}).get('enabled', False)),
            "destination_proxying": bool(proxy_config.get('enabled', False)),
        },
        "monitoring": {
            "listen_host": monitoring_config.get('listen_host', '127.0.0.1'),
            "prometheus_port": monitoring_config.get('prometheus_port', 9876),
            "metrics_prefix": monitoring_config.get('metrics_prefix', 'ares'),
        },
        "proxy_mode": "node" if proxy_config.get('enabled', False) and proxy_config.get('is_proxy_node', False) else "client" if proxy_config.get('enabled', False) else "disabled",
        "proxy": {
            "enabled": bool(proxy_config.get('enabled', False)),
            "is_proxy_node": bool(proxy_config.get('is_proxy_node', False)),
            "route_count": len(proxy_config.get('proxy_routes', [])),
            "default_request_timeout_seconds": proxy_config.get('default_request_timeout_seconds'),
            "max_payload_size_bytes": proxy_config.get('max_payload_size_bytes'),
            "listen_on_aspect": proxy_config.get('listen_on_aspect'),
        },
        "runtime_metrics": _fetch_runtime_metrics(config),
    }
    status["health_summary"] = _build_status_health_summary(config, status["runtime_metrics"])
    print(json.dumps(status, indent=2, sort_keys=True))
    sys.exit(0)
if __name__ == "__main__":
    print("Testing CLI parsing (run 'python -m akita_ares.main' to start app)..."); test_args = parse_args() 
    print(f"Parsed arguments: {test_args}"); print(f"Function to call: {test_args.func.__name__}") if hasattr(test_args,'func') else print("Default command logic handled by caller.")
