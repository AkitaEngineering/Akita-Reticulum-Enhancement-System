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
- Destination proxying with route validation and payload size limits.
- Centralized logging with rotation and per-module log levels.
- Prometheus metrics and a basic health endpoint.

## Current Proxying Scope

Destination proxying currently supports one-way forwarding to known RNS destinations.

- Clients must provide both the target destination hash and the full target destination name.
- Proxy payload size is limited by `destination_proxying.max_payload_size_bytes`.
- Response proxying is not supported by the current RNS integration and will fail explicitly.
- Unknown destinations fail closed after a path request is triggered.

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
- `destination_proxying.proxy_routes[].entry_destination_name`: outbound proxy service destination name.
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

This project is licensed under the MIT License. See `LICENSE` for the full text.
