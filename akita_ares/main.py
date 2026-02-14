
import time, os, signal, sys, logging, threading # Added threading
from .core.config_manager import ConfigManager
from .core.logger import setup_logging, get_logger, update_module_log_levels, ARES_LOGGER_NAME
from .features import request_retries, path_selection, proxying, monitoring
from .cli.main_cli import parse_args

# Attempt to import RNS
try:
    import RNS
    RNS_AVAILABLE = True
except ImportError:
    print("CRITICAL: Reticulum (RNS) library not found. ARES cannot function.", file=sys.stderr)
    RNS_AVAILABLE = False
    # Optionally exit here if RNS is absolutely required
    # sys.exit(1)

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "examples", "sample_config.json")
DEFAULT_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "examples", "config_schema.json")

class ARESApp:
    def __init__(self, args):
        self.args = args; self.cli_args_config_path = args.config; self.cli_args_schema_path = args.schema
        effective_config_path = self.cli_args_config_path or DEFAULT_CONFIG_PATH
        config_specified_schema_path = None
        if not self.cli_args_schema_path:
            try:
                peek_manager = ConfigManager(effective_config_path, schema_fp=None, validate_on_load=False)
                config_specified_schema_path = peek_manager.get_section('ares_core').get('config_schema_path')
            except Exception as e: print(f"Warning: Could not pre-load config '{effective_config_path}': {e}", file=sys.stderr)
        effective_schema_path = self.cli_args_schema_path or config_specified_schema_path or DEFAULT_SCHEMA_PATH
        if not os.path.isabs(effective_schema_path) and '~' not in effective_schema_path:
            if self.cli_args_schema_path: pass
            elif config_specified_schema_path: effective_schema_path = os.path.join(os.path.dirname(effective_config_path), effective_schema_path)
        self.config_manager = ConfigManager(effective_config_path, effective_schema_path)
        self.config = self.config_manager.get_config()
        log_config = self.config.get('logging', {}); cli_log_level = self.args.loglevel; effective_log_level = cli_log_level or log_config.get('level', 'INFO')
        setup_logging(level=effective_log_level, log_file=log_config.get('file', 'ares.log'), max_bytes=log_config.get('max_bytes', 10*1024*1024), backup_count=log_config.get('backup_count', 5), console_output=log_config.get('console_output', True), module_levels=log_config.get('module_levels'))
        self.logger = get_logger("ARESApp")
        self.logger.info(f"ARES Version {self.__get_version()} initializing...")
        self.logger.info(f"Using config: {self.config_manager.config_file_path}")
        if self.config_manager.schema_path and os.path.exists(self.config_manager.schema_path): self.logger.info(f"Using schema: {self.config_manager.schema_path}")
        elif self.config_manager.schema_path: self.logger.warning(f"Schema not found: {self.config_manager.schema_path}. Validation skipped.")
        if cli_log_level: self.logger.info(f"Log level overridden by CLI to: {cli_log_level}")
        else: self.logger.info(f"Effective global log level: {effective_log_level}")

        self.rns_instance = self._initialize_rns() # Initialize RNS
        if not self.rns_instance and RNS_AVAILABLE: # Check if init failed but lib was found
             self.logger.critical("RNS initialization failed. Exiting.")
             sys.exit(1)
        elif not RNS_AVAILABLE:
             self.logger.warning("RNS library not found. ARES features requiring RNS will be disabled or non-functional.")


        self.retry_manager = None; self.path_selector = None; self.proxy_manager = None; self.metrics_monitor = None
        self._initialize_features(); self._setup_signal_handlers()
        self.logger.info("ARES initialization complete.")

    def __get_version(self): try: from . import VERSION; return VERSION; except ImportError: return "unknown"

    def _initialize_rns(self):
        """ Initializes the Reticulum instance. """
        if not RNS_AVAILABLE:
            return None

        self.logger.info("Initializing Reticulum instance...")
        try:
            rns_config_path_str = self.config.get('ares_core', {}).get('rns_config_path', '~/.reticulum')
            expanded_rns_config_path = os.path.expanduser(rns_config_path_str)
            if not os.path.isdir(expanded_rns_config_path):
                self.logger.warning(f"RNS config directory not found: {expanded_rns_config_path}. RNS will use defaults. Creating directory.")
                try:
                    os.makedirs(expanded_rns_config_path, exist_ok=True)
                except OSError as e:
                    self.logger.error(f"Could not create RNS config directory {expanded_rns_config_path}: {e}")
                    # Continue, RNS might handle it internally or fail later

            # Initialize Reticulum. This might block if interfaces take time.
            # Consider running this in a separate thread if it blocks too long.
            rns_instance = RNS.Reticulum(configdir=expanded_rns_config_path, log_level=logging.WARNING) # Use a quieter log level for RNS itself?

            # Check if transport is enabled if ARES needs it (e.g., for proxy node)
            ares_needs_transport = self.config.get('destination_proxying',{}).get('is_proxy_node', False) # Example check
            if ares_needs_transport and not rns_instance.is_transport_enabled():
               self.logger.warning("ARES feature (e.g., Proxy Node) requires Transport, but Reticulum transport is NOT enabled in its config. Functionality may be limited.")

            self.logger.info(f"Reticulum instance initialized using config directory: {expanded_rns_config_path}")
            return rns_instance
        except ImportError: # Should have been caught earlier, but double-check
             self.logger.critical("RNS library import failed during initialization.")
             return None
        except Exception as e:
            self.logger.critical(f"Failed to initialize Reticulum: {e}", exc_info=True) # Log traceback
            return None

    def _initialize_features(self):
        self.logger.info("Initializing/updating ARES features..."); self.config = self.config_manager.get_config(); active_feature_count = 0
        update_module_log_levels(self.config.get('logging', {}).get('module_levels'))
        monitoring_config = self.config.get('monitoring', {})
        if monitoring_config.get('enabled', True):
            if not self.metrics_monitor: self.metrics_monitor = monitoring.MetricsMonitor(config=monitoring_config); self.metrics_monitor.start(); self.logger.info(f"Metrics Monitor initialized. Port {monitoring_config.get('prometheus_port', 9876)}.")
            else: self.metrics_monitor.update_config(monitoring_config); self.logger.info("Metrics Monitor config updated.")
        retry_config = self.config.get('request_retries', {})
        if retry_config.get('enabled', False):
            active_feature_count += 1
            if not self.retry_manager: self.retry_manager = request_retries.RetryManager(config=retry_config, metrics_monitor=self.metrics_monitor); self.logger.info("RetryMan initialized.")
            else: self.retry_manager.update_config(retry_config); self.logger.info("RetryMan config updated.")
        elif self.retry_manager: self.logger.info("Disabling RetryMan."); self.retry_manager = None
        path_selection_config = self.config.get('path_selection', {})
        if path_selection_config.get('enabled', False):
            active_feature_count += 1
            if not self.path_selector: self.path_selector = path_selection.PathSelector(config=path_selection_config, rns_instance=self.rns_instance, metrics_monitor=self.metrics_monitor); self.logger.info("PathSel initialized.")
            else: self.path_selector.update_config(path_selection_config); self.logger.info("PathSel config updated.")
        elif self.path_selector: self.logger.info("Disabling PathSel."); self.path_selector.stop() if hasattr(self.path_selector, 'stop') else None; self.path_selector = None
        proxy_config = self.config.get('destination_proxying', {})
        if proxy_config.get('enabled', False):
            # Only enable proxy manager if RNS is available
            if RNS_AVAILABLE and self.rns_instance:
                active_feature_count += 1
                if not self.proxy_manager: self.proxy_manager = proxying.ProxyManager(config=proxy_config, rns_instance=self.rns_instance, metrics_monitor=self.metrics_monitor); self.logger.info("ProxyMan initialized.")
                else: self.proxy_manager.update_config(proxy_config); self.logger.info("ProxyMan config updated.")
            else:
                 self.logger.warning("Proxying feature enabled in config, but RNS is not available or failed to initialize. Disabling ProxyManager.")
                 if self.proxy_manager: self.proxy_manager.shutdown(); self.proxy_manager = None # Ensure shutdown if it existed
        elif self.proxy_manager: self.logger.info("Disabling ProxyMan."); self.proxy_manager.shutdown() if hasattr(self.proxy_manager, 'shutdown') else None; self.proxy_manager = None
        if self.metrics_monitor: self.metrics_monitor.set_active_features_count(active_feature_count); self.metrics_monitor.set_active_proxy_routes_count(len(self.proxy_manager.proxy_routes)) if self.proxy_manager else self.metrics_monitor.set_active_proxy_routes_count(0)
        self.logger.info(f"Feature init/update finished. Active features: {active_feature_count}")

    def _setup_signal_handlers(self):
        if hasattr(signal,'SIGHUP'): signal.signal(signal.SIGHUP,self.handle_sighup); self.logger.info("SIGHUP handler registered.")
        else: self.logger.info("SIGHUP not available.")
        signal.signal(signal.SIGINT,self.handle_sigint_sigterm); signal.signal(signal.SIGTERM,self.handle_sigint_sigterm); self.logger.info("SIGINT/SIGTERM handlers registered.")

    def handle_sighup(self, signum, frame):
        self.logger.info(f"SIGHUP received. Reloading config & re-init features..."); self.config_manager.reload_config(); self.config = self.config_manager.get_config()
        # Re-apply log levels *after* getting new config
        new_log_config = self.config.get('logging', {})
        cli_log_level = self.args.loglevel
        effective_log_level = cli_log_level or new_log_config.get('level', 'INFO')
        logging.getLogger(ARES_LOGGER_NAME).setLevel(getattr(logging, effective_log_level))
        if cli_log_level: self.logger.info(f"Re-applying CLI log level override: {cli_log_level}")
        update_module_log_levels(new_log_config.get('module_levels'))
        # Re-initialize features
        self._initialize_features(); self.logger.info("Config reloaded & features re-initialized.")

    def handle_sigint_sigterm(self, signum, frame): self.logger.info(f"Signal {signum} received. Shutting down..."); self.shutdown(); sys.exit(0)

    def run(self):
        if not RNS_AVAILABLE and not self.rns_instance :
             self.logger.critical("Cannot run ARES without a functional RNS instance (library missing or init failed).")
             return # Exit run method

        self.logger.info("ARES running. Ctrl+C or SIGTERM to exit.")
        try:
            while True:
                # Check if RNS instance is still alive (conceptual check)
                # if self.rns_instance and not self.rns_instance.is_running(): # Assuming RNS has such a method
                #    self.logger.error("RNS instance appears to have stopped. Shutting down ARES.")
                #    break

                if self.path_selector: self.path_selector.periodic_update()
                if self.proxy_manager: self.proxy_manager.periodic_check()
                time.sleep(self.config.get('ares_core',{}).get('main_loop_sleep_interval',30)); self.logger.debug("ARES main loop tick.")
        except KeyboardInterrupt: self.logger.info("KeyboardInterrupt. Shutting down.")
        finally: self.shutdown()

    def shutdown(self):
        self.logger.info("ARES shutting down...");
        if self.path_selector: self.path_selector.stop()
        if self.proxy_manager: self.proxy_manager.shutdown()
        if self.metrics_monitor: self.metrics_monitor.stop()
        # Shutdown RNS instance if ARES owns it
        if self.rns_instance and hasattr(self.rns_instance, 'exit') :
            self.logger.info("Shutting down Reticulum instance...")
            # RNS might exit automatically when program ends, but explicit call is cleaner if available
            # self.rns_instance.exit()
        self.logger.info("ARES shutdown complete.")

def main_entry():
    args = parse_args()
    if hasattr(args, 'func') and args.command != 'start': args.func(args, ARESApp)
    else: handle_start_command(args, ARESApp); app = ARESApp(args=args); app.run()

if __name__ == "__main__": main_entry()
