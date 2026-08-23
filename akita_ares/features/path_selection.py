import importlib
import math
import threading
import time

from akita_ares.core.logger import get_logger

try:
    import RNS

    RNS_AVAILABLE = True
except ImportError:
    RNS = None
    RNS_AVAILABLE = False


class PathSelector:
    """Evaluate Reticulum's discovered paths without fabricating measurements.

    Stock Reticulum exposes its active path per destination. Adapters may expose
    multiple candidates through ``get_paths(destination_hash)``; ARES evaluates
    all candidates when that extension is present.
    """

    SUPPORTED_METRICS = {"rtt", "hops", "link_quality", "custom"}

    def __init__(self, config, rns_instance=None, metrics_monitor=None):
        self.rns_instance = rns_instance
        self.metrics_monitor = metrics_monitor
        self.logger = get_logger("Feature.PathSelector")
        self._cache_lock = threading.RLock()
        self.path_metrics_cache = {}
        self.known_paths = {}
        self.known_path_last_seen = {}
        self.custom_metric_evaluator = None
        self.custom_metrics_module_path = None
        self._last_metric_update_time = 0.0
        self.update_config(config)

    def update_config(self, new_config):
        self.config = dict(new_config or {})
        metric_type = self.config.get("default_metric", "hops")
        if metric_type not in self.SUPPORTED_METRICS:
            raise ValueError(f"Unsupported path metric: {metric_type!r}")
        previous_metric_type = getattr(self, "default_metric_type", None)
        self.default_metric_type = metric_type
        self.metric_update_interval = float(self.config.get("metric_update_interval_seconds", 60))
        self.rtt_probe_timeout = float(self.config.get("rtt_probe_timeout_seconds", 5))
        self.max_paths_to_consider = int(self.config.get("max_paths_to_consider", 5))
        if self.metric_update_interval <= 0 or self.rtt_probe_timeout <= 0:
            raise ValueError("path selection intervals and timeouts must be positive")
        if self.max_paths_to_consider < 1:
            raise ValueError("max_paths_to_consider must be at least 1")

        module_path = self.config.get("custom_metrics_module")
        old_path = self.custom_metrics_module_path
        self.custom_metrics_module_path = module_path
        if module_path != old_path or (module_path and self.custom_metric_evaluator is None):
            self._load_custom_metrics_module()
        if previous_metric_type and previous_metric_type != metric_type:
            with self._cache_lock:
                destination_hashes = list(self.known_paths)
                self.path_metrics_cache.clear()
            self._remove_metric_series(destination_hashes, previous_metric_type)
        self.logger.info(
            "PathSelector config: metric=%s, update_interval=%ss",
            self.default_metric_type,
            self.metric_update_interval,
        )

    def _load_custom_metrics_module(self):
        if not self.custom_metrics_module_path:
            self.custom_metric_evaluator = None
            return
        try:
            module = importlib.import_module(self.custom_metrics_module_path)
            evaluator = getattr(module, "evaluate_custom_metric", None)
            if evaluator is None and hasattr(module, "CustomMetricEvaluator"):
                instance = module.CustomMetricEvaluator()
                evaluator = getattr(instance, "evaluate_custom_metric", None)
                if evaluator is None and callable(instance):
                    evaluator = instance
            if not callable(evaluator):
                raise TypeError(
                    "custom metric module must export evaluate_custom_metric(path, rns_instance) "
                    "or a callable CustomMetricEvaluator"
                )
            self.custom_metric_evaluator = evaluator
            self.logger.info("Loaded custom path metric from %s", self.custom_metrics_module_path)
        except (ImportError, AttributeError, TypeError) as exc:
            self.custom_metric_evaluator = None
            raise ValueError(
                f"Unable to load custom metric module {self.custom_metrics_module_path!r}: {exc}"
            ) from exc

    @staticmethod
    def _field(path_info, name, default=None):
        if isinstance(path_info, dict):
            return path_info.get(name, default)
        return getattr(path_info, name, default)

    @classmethod
    def _path_id(cls, path_info):
        explicit = cls._field(path_info, "path_id")
        if explicit is not None:
            return str(explicit)
        components = (
            cls._field(path_info, "hash"),
            cls._field(path_info, "via"),
            cls._field(path_info, "interface"),
        )
        normalized = []
        for component in components:
            normalized.append(
                component.hex() if isinstance(component, bytes) else str(component or "")
            )
        return "|".join(normalized)

    @staticmethod
    def _validate_destination_hash(dest_hash_hex):
        if not isinstance(dest_hash_hex, str) or len(dest_hash_hex) != 32:
            raise ValueError("destination hash must be a 32-character hexadecimal string")
        try:
            return bytes.fromhex(dest_hash_hex)
        except ValueError as exc:
            raise ValueError("destination hash must be a 32-character hexadecimal string") from exc

    def _get_rns_paths(self, dest_hash_bytes):
        if not RNS_AVAILABLE or self.rns_instance is None:
            self.logger.warning("RNS is unavailable for path discovery.")
            return []
        try:
            get_paths = getattr(self.rns_instance, "get_paths", None)
            if callable(get_paths):
                return list(get_paths(dest_hash_bytes) or [])

            get_path_table = getattr(self.rns_instance, "get_path_table", None)
            if not callable(get_path_table):
                self.logger.error("The supplied RNS instance does not expose get_path_table().")
                return []
            matching_paths = []
            for entry in get_path_table() or []:
                entry_hash = self._field(entry, "hash")
                if entry_hash == dest_hash_bytes or (
                    isinstance(entry_hash, str) and entry_hash.lower() == dest_hash_bytes.hex()
                ):
                    matching_paths.append(entry)
            if not matching_paths:
                RNS.Transport.request_path(dest_hash_bytes)
            return matching_paths
        except Exception as exc:
            self.logger.error(
                "Error discovering RNS paths for %s: %s",
                dest_hash_bytes.hex()[:8],
                exc,
                exc_info=True,
            )
            return []

    def _measure_rtt_for_path(self, path_info):
        for field_name in ("rtt", "latency"):
            value = self._field(path_info, field_name)
            if value is not None:
                try:
                    value = float(value)
                    return value if value >= 0 and math.isfinite(value) else math.inf
                except (TypeError, ValueError):
                    return math.inf
        link = self._field(path_info, "link")
        link_rtt = getattr(link, "rtt", None) if link is not None else None
        if link_rtt is not None:
            try:
                return max(0.0, float(link_rtt))
            except (TypeError, ValueError):
                return math.inf
        probe = self._field(path_info, "probe")
        if callable(probe):
            started_at = time.monotonic()
            try:
                result = probe(timeout=self.rtt_probe_timeout)
                if result is False or result is None:
                    return math.inf
                reported_rtt = getattr(result, "get_rtt", lambda: None)()
                return (
                    float(reported_rtt)
                    if reported_rtt is not None
                    else time.monotonic() - started_at
                )
            except (OSError, TimeoutError, ValueError, TypeError) as exc:
                self.logger.warning("RTT probe failed for %s: %s", self._path_id(path_info), exc)
        return math.inf

    def _get_metric_for_path(self, path_info, metric_type):
        path_id = self._path_id(path_info)
        now = time.monotonic()
        with self._cache_lock:
            cached = self.path_metrics_cache.get(path_id, {}).get(metric_type)
        if cached and now - cached["timestamp"] < self.metric_update_interval / 2:
            return cached["value"]

        if metric_type == "rtt":
            value = self._measure_rtt_for_path(path_info)
        elif metric_type == "hops":
            value = self._field(path_info, "hops", math.inf)
        elif metric_type == "link_quality":
            quality = self._field(path_info, "quality", None)
            value = -float(quality) if quality is not None else math.inf
        elif metric_type == "custom" and self.custom_metric_evaluator:
            try:
                value = self.custom_metric_evaluator(path_info, self.rns_instance)
            except Exception as exc:
                self.logger.error("Custom metric failed for %s: %s", path_id, exc, exc_info=True)
                value = math.inf
        else:
            value = math.inf
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = math.inf
        if not math.isfinite(value):
            value = math.inf
        with self._cache_lock:
            cache = self.path_metrics_cache.setdefault(path_id, {})
            cache[metric_type] = {"value": value, "timestamp": now}
        return value

    def get_best_path(self, dest_hash_hex):
        dest_hash_bytes = self._validate_destination_hash(dest_hash_hex)
        if self.rns_instance is None:
            self.logger.warning("PathSelector requires an RNS instance.")
            return None
        paths = self._get_rns_paths(dest_hash_bytes)
        if not paths:
            return None

        now = time.monotonic()
        with self._cache_lock:
            self.known_paths[dest_hash_hex] = paths
            self.known_path_last_seen[dest_hash_hex] = now
        evaluated = []
        for path_info in paths[: self.max_paths_to_consider]:
            metric_value = self._get_metric_for_path(path_info, self.default_metric_type)
            if math.isfinite(metric_value):
                evaluated.append((metric_value, path_info))
        if not evaluated:
            self.logger.warning(
                "No measurable %s value is available for destination %s.",
                self.default_metric_type,
                dest_hash_hex[:8],
            )
            return None

        metric_value, best_path = min(evaluated, key=lambda item: item[0])
        if self.metrics_monitor:
            self.metrics_monitor.path_selection_evaluations_total.inc()
            self.metrics_monitor.path_selection_chosen_metric_value.labels(
                destination_hash=dest_hash_hex,
                metric_type=self.default_metric_type,
            ).set(metric_value)
        self.logger.info(
            "Best discovered path for %s is %s with %s=%s",
            dest_hash_hex[:8],
            self._path_id(best_path),
            self.default_metric_type,
            metric_value,
        )
        return best_path

    def periodic_update(self):
        now = time.monotonic()
        if now - self._last_metric_update_time < self.metric_update_interval:
            return
        self._last_metric_update_time = now
        stale_after = self.metric_update_interval * 10
        with self._cache_lock:
            destination_hashes = list(self.known_paths)
        for destination_hash in destination_hashes:
            with self._cache_lock:
                is_stale = now - self.known_path_last_seen.get(destination_hash, 0) > stale_after
                if is_stale:
                    stale_paths = self.known_paths.pop(destination_hash, [])
                    metric_types = set()
                    for path in stale_paths:
                        cached_metrics = self.path_metrics_cache.pop(self._path_id(path), {})
                        metric_types.update(cached_metrics)
                    self.known_path_last_seen.pop(destination_hash, None)
            if is_stale:
                self._remove_metric_series([destination_hash], metric_types)
                continue
            self.get_best_path(destination_hash)

    def influence_rns_routing(self, dest_hash_hex, chosen_path_id):
        """Request rediscovery on a selected interface when the adapter exposes it."""
        destination_hash = self._validate_destination_hash(dest_hash_hex)
        with self._cache_lock:
            chosen_path = next(
                (
                    path
                    for path in self.known_paths.get(dest_hash_hex, [])
                    if self._path_id(path) == str(chosen_path_id)
                ),
                None,
            )
        if chosen_path is None:
            self.logger.error(
                "Unknown path %s for destination %s", chosen_path_id, dest_hash_hex[:8]
            )
            return False
        interface = self._field(chosen_path, "interface_object") or self._field(
            chosen_path, "interface"
        )
        if interface is None or isinstance(interface, str):
            self.logger.error(
                "Path %s does not expose a usable Reticulum interface object", chosen_path_id
            )
            return False
        try:
            drop_path = getattr(self.rns_instance, "drop_path", None)
            if callable(drop_path):
                drop_path(destination_hash)
            RNS.Transport.request_path(destination_hash, on_interface=interface)
            return True
        except Exception as exc:
            self.logger.error(
                "Unable to request selected route %s: %s", chosen_path_id, exc, exc_info=True
            )
            return False

    def stop(self):
        with self._cache_lock:
            destination_hashes = list(self.known_paths)
            metric_types = {
                metric_type
                for cached_metrics in self.path_metrics_cache.values()
                for metric_type in cached_metrics
            }
            self.known_paths.clear()
            self.known_path_last_seen.clear()
            self.path_metrics_cache.clear()
        self._remove_metric_series(destination_hashes, metric_types)

    def _remove_metric_series(self, destination_hashes, metric_types):
        if isinstance(metric_types, str):
            metric_types = [metric_types]
        gauge = getattr(self.metrics_monitor, "path_selection_chosen_metric_value", None)
        remove = getattr(gauge, "remove", None)
        if not callable(remove):
            return
        for destination_hash in destination_hashes:
            for metric_type in metric_types:
                try:
                    remove(destination_hash, metric_type)
                except KeyError:
                    continue
