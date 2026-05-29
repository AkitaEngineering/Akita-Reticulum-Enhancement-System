# Akita Reticulum Enhancement System (ARES)

ARES is a Python service and CLI for operational hardening around the Reticulum Network Stack (RNS). It adds resilience, observability, and safer runtime defaults for applications that depend on RNS.

## Operational Surface

ARES does not ship a graphical frontend. The production-facing interfaces are:

- CLI commands: `start`, `configtest`, `status`
- JSON configuration with JSON Schema validation
- HTTP monitoring endpoints: `/metrics` and `/health`

## Features

- Circuit breaker protection for repeated failures.
- Configurable request retries with backoff and jitter.
- Metric-based path selection with Prometheus instrumentation.
- Destination proxying with policy-aware route selection, one-way forwarding, application-level request/response proxying over `RNS Link.request()`, and Prometheus outcome plus phase-latency metrics.
- Centralized logging with rotation and per-module log levels.
- Prometheus metrics and a basic health endpoint.

## Current Proxying Scope

Destination proxying supports one-way forwarding and application-level request/response flows to known RNS destinations.

- Clients must provide both the target destination hash and the full target destination name.
- Request/response proxying is available by supplying a `request_path`; ARES will proxy the request over `RNS Link.request()` and return the byte response through the provided callback.
- When no proxy alias is supplied, ARES selects the most specific matching route based on `target_network_prefix` and `allowed_target_aspects`.
- Requests outside a route's allowed target prefix or aspect policy are rejected before any proxy link setup occurs.
- Prometheus metrics expose request outcomes, end-to-end proxy request latency, phase latency for proxy-hop setup and target-service handling, and route policy denials so operators can distinguish success, timeout, and policy rejection paths.
- Proxy payload size is limited by `destination_proxying.max_payload_size_bytes`.
- Request/response proxying expects the target service to expose an application-level request handler path.
- Unknown destinations fail closed after a path request is triggered.
- ARES now uses an application-level request contract over `RNS Link.request()` for proxy request/response flows instead of generic packet forwarding.

## Quickstart

1. Create and activate a virtual environment.

```bash
python -m venv .venv
source .venv/bin/activate
```

2. Install dependencies.

```bash
python -m pip install -U pip
python -m pip install -r requirements.txt pytest
```

3. Validate the bundled example configuration.

```bash
python -m akita_ares.main --config examples/sample_config.json configtest
```

4. Inspect the effective runtime status.

```bash
python -m akita_ares.main --config examples/sample_config.json status
```

When monitoring is enabled and the local metrics endpoint is reachable, `status` also summarizes live retry, path-selection, and proxy counters plus latency metrics from `/metrics` into JSON and derives a small health summary from those signals.

5. Start ARES.

```bash
python -m akita_ares.main --config /path/to/config.json --loglevel INFO
```

## Security Notes

- RNS handles transport-layer confidentiality and authentication for encrypted destination types. ARES relies on RNS for network encryption rather than layering custom crypto on top.
- The monitoring server is plain HTTP and binds to `127.0.0.1` by default. If you need remote access, place it behind TLS and authentication.
- Keep configuration and log files in directories with restricted filesystem permissions.
- The bundled sample config validates against the bundled schema and is intended to be a safe production starting point.

## Configuration Highlights

- `monitoring.listen_host`: defaults to `127.0.0.1`.
- `monitoring.prometheus_port`: Prometheus and health endpoint port.
- `destination_proxying.max_payload_size_bytes`: upper bound for proxied payloads.
- `destination_proxying.default_request_timeout_seconds`: default timeout for proxied `Link.request()` calls when the caller does not override it.
- `destination_proxying.proxy_routes[].entry_destination_name`: outbound proxy service destination name.
- `destination_proxying.proxy_routes[].target_network_prefix`: target namespace that a route is allowed to serve.
- `destination_proxying.proxy_routes[].allowed_target_aspects`: allowed aspects within that namespace; used for automatic route selection and preflight policy rejection.
- `ares_core.config_schema_path`: bundled schema path or a custom schema override.
- `ares_core.rns_config_path`: the sample config defaults to `~/.ares-reticulum` so it does not inherit machine-specific Reticulum interfaces.

## Testing

Run the full suite with:

```bash
python -m pytest -q
```

## Project Layout

- `akita_ares/`: package source
- `akita_ares/core/`: config, logging, circuit breaker
- `akita_ares/features/`: monitoring, proxying, path selection, retries
- `akita_ares/cli/`: command-line interface
- `examples/`: validated sample configuration and schema
- `tests/`: unit and regression tests

## License

This project is licensed under the GNU General Public License v3.0. See `LICENSE` for the full text.
