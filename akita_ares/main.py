import os
import signal
import sysconfig
import threading

from .cli.main_cli import parse_args
from .core.config_manager import ConfigManager
from .core.logger import get_logger, setup_logging, update_module_log_levels
from .features import monitoring, path_selection, proxying, request_retries

try:
    import RNS

    RNS_AVAILABLE = True
except ImportError:
    RNS = None
    RNS_AVAILABLE = False


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _bundled_example_path(filename):
    source_path = os.path.join(PROJECT_ROOT, "examples", filename)
    if os.path.exists(source_path):
        return source_path
    return os.path.join(sysconfig.get_path("data"), "share", "akita-ares", "examples", filename)


DEFAULT_CONFIG_PATH = _bundled_example_path("sample_config.json")
DEFAULT_SCHEMA_PATH = _bundled_example_path("config_schema.json")


class ARESApp:
    def __init__(self, args):
        self.args = args
        self.logger = get_logger("ARESApp")
        self.rns_instance = None
        self.rns_identity = None
        self.retry_manager = None
        self.path_selector = None
        self.proxy_manager = None
        self.metrics_monitor = None
        self._stop_event = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False

        effective_config_path = args.config or DEFAULT_CONFIG_PATH
        effective_schema_path = self._resolve_schema_path(effective_config_path, args.schema)
        self.config_manager = ConfigManager(effective_config_path, effective_schema_path)
        self.config_manager.require_valid(require_schema=True)
        self.config = self.config_manager.get_config()

        log_config = self.config.get("logging", {})
        effective_log_level = args.loglevel or log_config.get("level", "INFO")
        setup_logging(
            level=effective_log_level,
            log_file=log_config.get("file", "ares.log"),
            max_bytes=log_config.get("max_bytes", 10 * 1024 * 1024),
            backup_count=log_config.get("backup_count", 5),
            console_output=log_config.get("console_output", True),
            module_levels=log_config.get("module_levels"),
        )
        self.logger = get_logger("ARESApp")
        self.logger.info("ARES version %s initializing", self._get_version())
        self.logger.info(
            "Using config %s and schema %s", self.config_manager.config_fp, effective_schema_path
        )

        if not RNS_AVAILABLE:
            raise RuntimeError("Reticulum (RNS) is required to start ARES")
        try:
            self.rns_instance = self._initialize_rns()
            self.rns_identity = self._load_or_create_identity()
            self._initialize_features()
            self._setup_signal_handlers()
        except Exception:
            self.shutdown()
            raise
        self.logger.info("ARES initialization complete")

    @staticmethod
    def _resolve_schema_path(config_path, cli_schema_path):
        if cli_schema_path:
            return os.path.abspath(os.path.expanduser(cli_schema_path))
        configured_path = None
        try:
            peek_manager = ConfigManager(config_path, schema_fp=None, validate_on_load=False)
            if peek_manager.last_load_error is None:
                configured_path = peek_manager.get_section("ares_core").get("config_schema_path")
        except (OSError, TypeError, ValueError):
            configured_path = None
        if configured_path:
            configured_path = os.path.expanduser(configured_path)
            if not os.path.isabs(configured_path):
                configured_path = os.path.join(
                    os.path.dirname(os.path.abspath(config_path)), configured_path
                )
            return os.path.abspath(configured_path)
        return DEFAULT_SCHEMA_PATH

    @staticmethod
    def _get_version():
        from . import VERSION

        return VERSION

    def _initialize_rns(self):
        core_config = self.config.get("ares_core", {})
        config_directory = os.path.abspath(
            os.path.expanduser(core_config.get("rns_config_path", "~/.reticulum"))
        )
        os.makedirs(config_directory, mode=0o750, exist_ok=True)
        rns_instance = RNS.Reticulum(configdir=config_directory, loglevel=RNS.LOG_WARNING)
        needs_transport = bool(core_config.get("enable_transport_node_features", False))
        if needs_transport and not RNS.Reticulum.transport_enabled():
            raise RuntimeError(
                "ARES transport-node features are enabled, but Reticulum transport is disabled"
            )
        return rns_instance

    def _load_or_create_identity(self):
        core_config = self.config.get("ares_core", {})
        identity_path = core_config.get("identity_path")
        if not identity_path:
            rns_config_path = os.path.abspath(
                os.path.expanduser(core_config.get("rns_config_path", "~/.reticulum"))
            )
            identity_path = os.path.join(rns_config_path, "ares_identity")
        identity_path = os.path.abspath(os.path.expanduser(identity_path))
        if os.path.exists(identity_path):
            identity = RNS.Identity.from_file(identity_path)
            if identity is None:
                raise RuntimeError(f"Unable to load ARES identity from {identity_path}")
        else:
            os.makedirs(os.path.dirname(identity_path), mode=0o750, exist_ok=True)
            identity = RNS.Identity()
            file_descriptor = os.open(
                identity_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(file_descriptor)
            if not identity.to_file(identity_path):
                try:
                    os.unlink(identity_path)
                except OSError:
                    self.logger.error("Unable to remove incomplete identity file %s", identity_path)
                raise RuntimeError(f"Unable to persist ARES identity to {identity_path}")
        os.chmod(identity_path, 0o600)
        self.logger.info("ARES identity loaded: %s", identity.hexhash)
        return identity

    def _initialize_features(self):
        self.config = self.config_manager.get_config()
        update_module_log_levels(self.config.get("logging", {}).get("module_levels"))
        active_feature_count = 0

        monitoring_config = self.config.get("monitoring", {})
        if monitoring_config.get("enabled", True):
            if self.metrics_monitor is None:
                self.metrics_monitor = monitoring.MetricsMonitor(monitoring_config)
                self.metrics_monitor.start()
            else:
                self.metrics_monitor.update_config(monitoring_config)
        elif self.metrics_monitor is not None:
            self.metrics_monitor.stop()
            self.metrics_monitor = None

        retry_config = self.config.get("request_retries", {})
        if retry_config.get("enabled", False):
            active_feature_count += 1
            if self.retry_manager is None:
                self.retry_manager = request_retries.RetryManager(
                    retry_config, self.metrics_monitor
                )
            else:
                self.retry_manager.metrics_monitor = self.metrics_monitor
                self.retry_manager.update_config(retry_config)
        else:
            self.retry_manager = None

        path_config = self.config.get("path_selection", {})
        if path_config.get("enabled", False):
            active_feature_count += 1
            if self.path_selector is None:
                self.path_selector = path_selection.PathSelector(
                    path_config,
                    rns_instance=self.rns_instance,
                    metrics_monitor=self.metrics_monitor,
                )
            else:
                self.path_selector.metrics_monitor = self.metrics_monitor
                self.path_selector.update_config(path_config)
        elif self.path_selector is not None:
            self.path_selector.stop()
            self.path_selector = None

        proxy_config = self.config.get("destination_proxying", {})
        if proxy_config.get("enabled", False):
            active_feature_count += 1
            if self.proxy_manager is None:
                self.proxy_manager = proxying.ProxyManager(
                    proxy_config,
                    rns_instance=self.rns_instance,
                    metrics_monitor=self.metrics_monitor,
                    identity=self.rns_identity,
                    path_selector=self.path_selector,
                )
                if (
                    proxy_config.get("is_proxy_node", False)
                    and self.proxy_manager.service_destination is None
                ):
                    raise RuntimeError("Proxy-node service destination could not be initialized")
            else:
                self.proxy_manager.metrics_monitor = self.metrics_monitor
                self.proxy_manager.path_selector = self.path_selector
                self.proxy_manager.update_config(proxy_config)
        elif self.proxy_manager is not None:
            self.proxy_manager.shutdown()
            self.proxy_manager = None

        if self.metrics_monitor:
            self.metrics_monitor.set_active_features_count(active_feature_count)
            route_count = len(self.proxy_manager.proxy_routes) if self.proxy_manager else 0
            self.metrics_monitor.set_active_proxy_routes_count(route_count)

    def _setup_signal_handlers(self):
        if threading.current_thread() is not threading.main_thread():
            self.logger.warning("Signal handlers can only be installed from the main thread")
            return
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self.handle_sighup)
        signal.signal(signal.SIGINT, self.handle_sigint_sigterm)
        signal.signal(signal.SIGTERM, self.handle_sigint_sigterm)

    def handle_sighup(self, signum, frame):
        self.logger.info("SIGHUP received; reloading configuration")
        old_config = self.config_manager.get_config()
        self.config_manager.reload_config()
        if not self.config_manager.last_reload_succeeded:
            return
        new_config = self.config_manager.get_config()
        old_core = old_config.get("ares_core", {})
        new_core = new_config.get("ares_core", {})
        immutable_keys = (
            "rns_config_path",
            "identity_path",
            "config_schema_path",
            "enable_transport_node_features",
        )
        changed_immutable_keys = [
            key for key in immutable_keys if old_core.get(key) != new_core.get(key)
        ]
        if changed_immutable_keys:
            self.config_manager.config = old_config
            self.config_manager.last_reload_succeeded = False
            self.logger.error(
                "Config reload rejected because %s require a process restart",
                ", ".join(changed_immutable_keys),
            )
            return
        log_config = new_config.get("logging", {})
        effective_level = self.args.loglevel or log_config.get("level", "INFO")
        try:
            setup_logging(
                level=effective_level,
                log_file=log_config.get("file", "ares.log"),
                max_bytes=log_config.get("max_bytes", 10 * 1024 * 1024),
                backup_count=log_config.get("backup_count", 5),
                console_output=log_config.get("console_output", True),
                module_levels=log_config.get("module_levels"),
            )
            self._initialize_features()
        except Exception as exc:
            self.logger.error("Unable to apply reloaded configuration: %s", exc, exc_info=True)
            self.config_manager.config = old_config
            self.config_manager.last_reload_succeeded = False
            rollback_failed = False
            try:
                old_log_config = old_config.get("logging", {})
                setup_logging(
                    level=self.args.loglevel or old_log_config.get("level", "INFO"),
                    log_file=old_log_config.get("file", "ares.log"),
                    max_bytes=old_log_config.get("max_bytes", 10 * 1024 * 1024),
                    backup_count=old_log_config.get("backup_count", 5),
                    console_output=old_log_config.get("console_output", True),
                    module_levels=old_log_config.get("module_levels"),
                )
                self.logger = get_logger("ARESApp")
            except Exception as logging_rollback_exc:
                rollback_failed = True
                self.logger.critical(
                    "Unable to restore the previous logging configuration: %s",
                    logging_rollback_exc,
                    exc_info=True,
                )
            try:
                self._initialize_features()
            except Exception as rollback_exc:
                rollback_failed = True
                self.logger.critical(
                    "Unable to restore the previous feature configuration: %s",
                    rollback_exc,
                    exc_info=True,
                )
            if rollback_failed:
                self._stop_event.set()

    def handle_sigint_sigterm(self, signum, frame):
        self.logger.info("Signal %s received; stopping", signum)
        self._stop_event.set()

    def run(self):
        self.logger.info("ARES running")
        try:
            while not self._stop_event.is_set():
                if self.path_selector:
                    self.path_selector.periodic_update()
                if self.proxy_manager:
                    self.proxy_manager.periodic_check()
                interval = self.config.get("ares_core", {}).get("main_loop_sleep_interval", 30)
                self._stop_event.wait(float(interval))
        finally:
            self.shutdown()

    def shutdown(self):
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutdown_complete = True
        self._stop_event.set()
        cleanup_actions = (
            ("path selector", self.path_selector.stop if self.path_selector else None),
            ("proxy manager", self.proxy_manager.shutdown if self.proxy_manager else None),
            ("metrics monitor", self.metrics_monitor.stop if self.metrics_monitor else None),
            (
                "Reticulum",
                RNS.Reticulum.exit_handler
                if self.rns_instance is not None and RNS_AVAILABLE
                else None,
            ),
        )
        for component_name, cleanup in cleanup_actions:
            if cleanup is None:
                continue
            try:
                cleanup()
            except Exception:
                self.logger.exception("Error shutting down %s", component_name)
        self.logger.info("ARES shutdown complete")


def main_entry():
    args = parse_args()
    try:
        args.func(args, ARESApp)
    except (OSError, ValueError) as exc:
        get_logger("ARESApp").critical("ARES command failed: %s", exc)
        raise SystemExit(1) from exc
    except Exception as exc:
        get_logger("ARESApp").critical("ARES command failed: %s", exc, exc_info=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main_entry()
