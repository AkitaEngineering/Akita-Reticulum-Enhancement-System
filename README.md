# Akita Reticulum Enhancement System (ARES)

ARES is a Python service and CLI for operational hardening around the Reticulum Network Stack (RNS). It adds resilience, observability, and safer runtime defaults for applications that depend on RNS.

## Operational Surface

ARES does not ship a graphical frontend. The production-facing interfaces are:

- CLI commands: `start`, `configtest`, `status`, `healthcheck`
- JSON configuration with JSON Schema validation
- HTTP monitoring endpoints: `/metrics` and `/health`

## Features

- Circuit breaker protection for repeated failures.
- Configurable request retries with backoff and jitter.
- Evaluation of Reticulum-discovered paths by hop count, measured RTT, link quality, or a custom metric, with Prometheus instrumentation. Stock Reticulum exposes one active path per destination; adapters that expose multiple candidates can use ARES to rank them.
- Destination proxying with policy-aware route selection, one-way forwarding, application-level request/response proxying over `RNS Link.request()`, and Prometheus outcome plus phase-latency metrics.
- Centralized logging with rotation and per-module log levels.
- Prometheus metrics and a basic health endpoint.

## Current Proxying Scope

Destination proxying supports one-way forwarding and application-level request/response flows to known RNS destinations.

- Clients must provide both the target destination hash and the full target destination name.
- Request/response proxying is available by supplying a `request_path`; ARES will proxy the request over `RNS Link.request()` and return the byte response through the provided callback.
- When no proxy alias is supplied, ARES selects the most specific matching route based on `target_network_prefix` and `allowed_target_aspects`.
- Requests outside a route's allowed target prefix or aspect policy are rejected before any proxy link setup occurs. Proxy nodes independently enforce `inbound_target_policies`, so client-side policy cannot be bypassed.
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
python -m pip install -e '.[test]'
```

3. Validate the bundled example configuration.

```bash
python -m akita_ares.main --config examples/sample_config.json configtest
```

4. Inspect the effective runtime status.

```bash
python -m akita_ares.main --config examples/sample_config.json status
```

When monitoring is enabled and the local metrics endpoint is reachable, `status` also summarizes live retry, path-selection, and proxy counters plus latency metrics from `/metrics` into JSON and derives a small health summary from those signals. Use `status --wait 3` to briefly poll `/metrics` during startup before declaring it unavailable.

5. Run an automation-friendly health check.

```bash
python -m akita_ares.main --config examples/sample_config.json healthcheck --wait 3
```

`healthcheck` prints the same JSON summary and exits non-zero when health is degraded or unknown.

6. Start ARES.

```bash
python -m akita_ares.main --config /path/to/config.json --loglevel INFO
```

Startup requires an explicit `--config` path so the bundled example cannot be launched accidentally as a live service.

## Security Notes

- RNS handles transport-layer confidentiality and authentication for encrypted destination types. ARES relies on RNS for network encryption rather than layering custom crypto on top.
- The monitoring server is plain HTTP and binds to `127.0.0.1` by default. If you need remote access, place it behind TLS and authentication.
- Keep configuration and log files in directories with restricted filesystem permissions.
- ARES persists its RNS identity at `ares_core.identity_path`, or at `<rns_config_path>/ares_identity` when no explicit path is set. The file is created with mode `0600`; back it up securely before replacing a proxy node.
- Proxy nodes fail closed unless `destination_proxying.inbound_target_policies` permits a target. `allow_all_targets_on_proxy_node` is available for deliberately unrestricted deployments and should be used only on trusted networks.
- The bundled sample config validates against the bundled schema and is intended to be a safe production starting point.

## Configuration Highlights

- `monitoring.listen_host`: defaults to `127.0.0.1`.
- `monitoring.prometheus_port`: Prometheus and health endpoint port.
- `destination_proxying.max_payload_size_bytes`: upper bound for proxied payloads.
- `destination_proxying.default_request_timeout_seconds`: default timeout for proxied `Link.request()` calls when the caller does not override it.
- `destination_proxying.proxy_routes[].entry_destination_name`: outbound proxy service destination name.
- `destination_proxying.proxy_routes[].target_network_prefix`: target namespace that a route is allowed to serve.
- `destination_proxying.proxy_routes[].allowed_target_aspects`: allowed aspects within that namespace; used for automatic route selection and preflight policy rejection.
- `destination_proxying.inbound_target_policies`: server-side target prefixes/aspects accepted by a proxy node.
- `destination_proxying.listen_destination_name`: complete RNS destination name announced by a proxy node. When omitted, ARES uses `ares.proxy.<listen_on_aspect>`.
- `destination_proxying.announce_interval_seconds`: interval for refreshing the proxy service announce; minimum 30 seconds.
- `ares_core.config_schema_path`: bundled schema path or a custom schema override.
- `ares_core.rns_config_path`: the sample config defaults to `~/.ares-reticulum` so it does not inherit machine-specific Reticulum interfaces.
- `ares_core.identity_path`: optional explicit path for the persistent ARES identity.

## Production Checklist

Before deployment:

1. Run `akita-ares --config /path/to/config.json configtest` and require exit code 0.
2. Configure Reticulum interfaces in the directory selected by `ares_core.rns_config_path` and verify announces/path discovery on the actual network.
3. Keep the monitoring listener on loopback, or put it behind authenticated TLS when exposing it remotely.
4. For proxy-node mode, configure `listen_destination_name` and at least one `inbound_target_policies` rule; distribute the resulting identity hash to clients.
5. Supervise the process with a service manager, send `SIGTERM` for shutdown, and use `healthcheck --wait 3` as the readiness probe.
6. Back up the identity file and test restore, log rotation, port binding, and proxy request/response behavior in the deployment environment.

`SIGHUP` reloads logging and feature settings atomically when possible. Changes to `rns_config_path` or `identity_path` require a process restart and are rejected during live reload.

## Testing

Run the full suite with:

```bash
python -m pytest -q
python -m ruff check .
python -m ruff format --check akita_ares tests
python -m build
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
