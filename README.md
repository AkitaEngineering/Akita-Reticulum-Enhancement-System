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
* **Globally Routable Multicast:** (Planned)
    * Efficient delivery of data to multiple destinations simultaneously.
* **Destination Proxying:**
    * Enables communication with destinations behind firewalls or in private networks.
    * Client-side configuration for using proxies.
    * Server-side functionality for acting as a proxy node.
    * Simple protocol for proxying requests and responses (conceptual).
    * Validation of configured proxy hash formats.
    * Metrics for proxied traffic and active clients/routes.
* **Comprehensive Configuration Management:**
    * JSON configuration file.
    * JSON schema validation for configuration files.
    * Default configuration file with sensible defaults.
    * Dynamic configuration reloading via SIGHUP.
* **Centralized Logging and Monitoring:**
    * Centralized logging system with configurable root level and **per-module levels**.
    * Log rotation based on size and backup count.
    * **CLI override** for global log level.
    * Metric monitoring and export via Prometheus.
    * Basic health check endpoint (conceptual).
* **Command-Line Interface (CLI):**
    * `start`: Starts the ARES application (default command).
    * `configtest`: Validates the configuration file syntax and schema.
    * `status`: Placeholder for querying runtime status.
    * Global options: `--config`, `--schema`, `--loglevel`, `--version`.
* **Robust Error Handling:**
    * Comprehensive exception handling.
    * Circuit breaker pattern implementation (basic).
    * Health checks (planned).
* **Comprehensive Testing:** (In Progress)
    * Unit tests for individual modules and functions (`tests/`). **(Core tests implemented)**
    * Integration tests for interactions between ARES components (planned).
    * End-to-end tests for user scenarios (planned, requires RNS environment).

## Project Structure

The project is organized into the following main directories:

* `akita_ares/`: Source code.
    * `core/`: Core components like configuration management, logging, and common utilities.
    * `features/`: Individual enhancement modules.
    * `cli/`: Command-line interface components.
    * `main.py`: Main application orchestrator.
* `tests/`: Unit and integration tests.
    * `core/` **(Unit tests added)**
    * `features/`
* `examples/`: Example configurations (`sample_config.json`, `config_schema.json`).
* `docs/`: Project documentation (to be added).

## Getting Started

### Prerequisites

* Python 3.7+
* Reticulum Network Stack (RNS) installed and configured. (`pip install rns`)
* Dependencies: `jsonschema`, `prometheus_client` (install via `pip install -r requirements.txt`).

### Configuration

1.  Copy `examples/sample_config.json` to a working location (e.g., `~/.ares/config.json` or project root).
2.  Modify the configuration as needed. Key sections:
    * `ares_core`: Path to your RNS configuration.
    * `logging`: Logging level, file, and per-module levels.
    * Feature-specific sections (`request_retries`, `path_selection`, `destination_proxying`, `monitoring`): Enable/disable and configure features.
3.  Ensure the schema (`examples/config_schema.json` or custom) is accessible if validation is desired.

### Running ARES

Use the command-line interface:

```bash
# From project root or with PYTHONPATH set
# Ensure RNS is running or configured correctly first!
python -m akita_ares.cli.main_cli --config /path/to/your/ares_config.json --loglevel DEBUG
# (No command needed, 'start' is default)

# Validate config:
python -m akita_ares.cli.main_cli configtest --config /path/to/config.json

# Show version:
python -m akita_ares.cli.main_cli --version

# Run unit tests:
python -m unittest discover tests
```

