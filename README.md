# Akita Reticulum Enhancement System

The Akita Reticulum Enhancement System is designed to elevate the Reticulum network stack by providing robust, modular, and feature-rich enhancements.

## Features

* **Robust Request Retries:**
    * Automatic retry mechanism for all `Reticulum.request()` calls.
    * Configurable retry parameters (retries, delay, jitter).
    * Detailed logging of retry attempts and failures.
* **Metric-Based Path Selection:**
    * Intelligent routing decisions based on network metrics (e.g., latency).
    * Dynamic path selection to optimize network performance.
    * Integration with Reticulum's transport layer.
* **Globally Routable Multicast:**
    * Efficient delivery of data to multiple destinations simultaneously.
    * Integration with Reticulum's multicast transport layer.
* **Destination Proxying:**
    * Enables communication with destinations behind firewalls or in private networks.
    * Manages proxy routes within the Reticulum transport layer.
* **Comprehensive Configuration Management:**
    * JSON schema validation for configuration files.
    * Default configuration file with sensible defaults.
    * Dynamic configuration reloading.
* **Centralized Logging and Monitoring:**
    * Centralized logging system with configurable log levels.
    * Metric monitoring using Prometheus.
    * Alerting capabilities (to be further implemented).
* **Command-Line Interface (CLI):**
    * CLI for managing configuration, metrics, and logging.
* **Robust Error Handling:**
    * Comprehensive Exception handling.
    * Circuit breaker pattern implementation.
    * Health checks.
* **Comprehensive Testing:**
    * Unit and integration tests.

## Directory Structure

```
reticulum_akita/
├── init.py
├── akita_retry.py
├── akita_routing.py
├── akita_multicast.py
├── akita_proxy.py
├── akita_config.py
├── akita_logging.py
├── akita_metrics.py
├── akita_cli.py
└── tests/
├── init.py
├── test_akita_retry.py
├── test_akita_routing.py
├── ...
```
## Installation

1.  Clone the repository or download the source code.
2.  Install the required dependencies: `pip install jsonschema prometheus_client`
3.  Place the `reticulum_akita` directory in your Python path.
4.  Ensure you have Reticulum installed and configured.

## Usage

### Configuration

* **Load Configuration:** `python -m reticulum_akita.akita_cli config load --file akita_config.json`
* **Save Configuration:** `python -m reticulum_akita.akita_cli config save --file akita_config.json key1=value1 key2={"nested": "value"}`

### Metrics

* **Start Metrics Server:** `python -m reticulum_akita.akita_cli metrics --port 8000`
* Configure Prometheus to scrape the metrics endpoint at `http://localhost:8000/metrics`.

### Logging

* **Set Logging Level:** `python -m reticulum_akita.akita_cli logging --level DEBUG`

### Using Modules in Your Code

```python
import reticulum_akita as akita
import RNS

# Example using retry
retry = akita.RequestRetry(retries=5)
success = retry.execute(RNS.Destination("dest_string"), b"content")

# Example using routing
routing = akita.RoutingManager()
routing.update_metric(RNS.Transport.interfaces[0], "latency", 10)
best_interface = routing.select_best_path(RNS.Destination("dest_string"))

# Example using multicast
multicast = akita.MulticastManager()
multicast.join_group("group", RNS.Destination("dest_string"))

# Example using proxy
proxy = akita.ProxyManager()
proxy.add_proxy_route(RNS.Destination("dest1"), RNS.Destination("dest2"))

#Example using config
config = akita.load_config()
print(config)
akita.save_config({"test":"test"})

#Example using logging
akita.setup_logging(logging.DEBUG)

#Example using metrics
akita.setup_metrics(8001)
```

# Integration

To fully integrate these modules into a live Reticulum environment, modifications to Reticulum's core code are required.

* **Request Retries:** Replace direct `RNS.Reticulum.request()` calls with `RequestRetry.execute()`.
* **Routing:** Modify `RNS.Transport` routing logic to use `RoutingManager.select_best_path()`.
* **Multicast:** Use the `MulticastManager` methods.
* **Proxying:** Use the `ProxyManager` methods.

# Testing

* **Run Unit Tests:** `python -m unittest discover reticulum_akita/tests`

# Contributing

Contributions are welcome! Please submit pull requests or open issues for bug reports or feature requests.
