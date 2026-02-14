# Akita Reticulum Enhancement System (ARES)

**Organization:** Akita Engineering
**Website:** www.akitaengineering.com
**License:** GPLv3
**Version:** 0.1.5-alpha

## Overview

The Akita Reticulum Enhancement System (ARES) is designed to elevate the Reticulum network stack by providing robust, modular, and feature-rich enhancements. This system aims to improve the resilience, performance, observability, and overall capabilities of applications built on Reticulum.

ARES is built with a modular architecture, allowing features to be developed, integrated, and maintained independently.

## Core Goals

* **Enhance Robustness:** Implement features like advanced request retries and circuit breakers.
* **Improve Performance:** Introduce intelligent mechanisms like metric-based path selection.
* **Increase Capabilities:** Add functionalities such as globally routable multicast and destination proxying.
* **Simplify Management:** Provide comprehensive configuration, centralized logging, and a CLI.
* **Ensure Reliability:** Through comprehensive error handling and thorough testing.

## Features (Planned & In Development)

* **Robust Request Retries:**
    * Automatic retry mechanism for `Reticulum.request()` calls.
    * Configurable retry parameters (retries, delay, jitter).
    * Detailed logging of retry attempts and failures.
    * Metrics for retry attempts, successes, failures, and duration.
* **Metric-Based Path Selection:**
    * Intelligent routing decisions based on network metrics (e.g., RTT, hop count, link quality).
    * Dynamic path selection to optimize network performance.
    * Support for custom metric evaluation modules.
    * Metrics for path selection events and chosen path quality.
# Akita Reticulum Enhancement System (ARES)

**Organization:** Akita Engineering
**Website:** https://www.akitaengineering.com
**License:** MIT
**Version:** 0.1.6

Overview
--------

ARES (Akita Reticulum Enhancement System) provides modular enhancements to the Reticulum (RNS) network stack. It focuses on improving resilience, observability, and operational behavior for applications using RNS.

Status
------

- Core modules implemented and unit tested.
- Current test coverage: all unit tests pass locally (29 tests).

Key Features
------------

- Robust request retries with configurable backoff and jitter.
- Circuit breaker implementation for isolating failing operations.
- Metric-based path selection (RTT / hops / custom metrics).
- Destination proxying support (client and proxy node logic).
- Prometheus metrics with a simple `/metrics` and `/health` endpoint.
- JSON-based configuration with optional JSON Schema validation.

Project Layout
--------------

- `akita_ares/` - package source
  - `core/` - config, logging, utilities
  - `features/` - pluggable features (monitoring, proxying, path selection, retries)
  - `cli/` - command line interface
  - `main.py` - application orchestration
- `tests/` - unit tests (run with `pytest`)
- `examples/` - example config and schema files

Quickstart
----------

1. Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate   # on Windows: .venv\Scripts\Activate.ps1
```

2. Install dependencies:

```bash
pip install -r requirements.txt
pip install pytest
```

3. Copy and adjust configuration:

```bash
cp examples/sample_config.json ~/.ares_config.json
# edit the file as needed
```

4. Run tests:

```bash
python -m pytest -q
```

5. Run the CLI (start is default):

```bash
python -m akita_ares.cli.main_cli --config /path/to/config.json --loglevel INFO
```

Notes
-----

- RNS (Reticulum) is optional for running unit tests; features that require RNS have fallbacks and will log warnings when RNS is not present.
- The package uses a small built-in HTTP server to expose Prometheus metrics and a `/health` endpoint.

Contributing
------------

Contributions, bug reports, and PRs are welcome. Please follow the standard GitHub workflow: fork, branch, commit with clear messages, and open a PR against `main`.

License
-------

This project is licensed under the MIT License — see the `LICENSE` file for details.

Contact
-------

For questions, reach out to the Akita Engineering team via the project repository.

