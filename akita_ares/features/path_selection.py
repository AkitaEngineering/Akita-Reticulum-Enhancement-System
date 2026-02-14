import time, importlib, random
from akita_ares.core.logger import get_logger
try:
    import RNS
    RNS_AVAILABLE = True
except ImportError:
    RNS_AVAILABLE = False
class PathSelector:
    def __init__(self, config, rns_instance=None, metrics_monitor=None):
        self.rns_instance, self.metrics_monitor = rns_instance, metrics_monitor
        self.logger = get_logger("Feature.PathSelector")
        self.path_metrics_cache, self.known_paths, self.custom_metric_evaluator = {}, {}, None
        self.update_config(config); self._last_metric_update_time = 0
    def update_config(self, new_config):
        self.config = new_config; self.default_metric_type = self.config.get('default_metric','rtt')
        self.metric_update_interval = self.config.get('metric_update_interval_seconds',60)
        self.rtt_probe_timeout = self.config.get('rtt_probe_timeout_seconds',5)
        self.max_paths_to_consider = self.config.get('max_paths_to_consider',5)
        custom_module_path = self.config.get('custom_metrics_module'); old_path = getattr(self,'custom_metrics_module_path',None)
        self.custom_metrics_module_path = custom_module_path
        if custom_module_path != old_path or (custom_module_path and not self.custom_metric_evaluator): self._load_custom_metrics_module()
        self.logger.info(f"PathSel cfg: Metric={self.default_metric_type}, UpdateInt={self.metric_update_interval}s")
    def _load_custom_metrics_module(self):
        if not self.custom_metrics_module_path: self.custom_metric_evaluator = None; self.logger.info("No custom metrics module."); return
        try:
            mod = importlib.import_module(self.custom_metrics_module_path)
            if hasattr(mod,'evaluate_custom_metric'): self.custom_metric_evaluator=mod.evaluate_custom_metric; logger_msg="func 'evaluate_custom_metric'"
            elif hasattr(mod,'CustomMetricEvaluator'): self.custom_metric_evaluator=getattr(mod,'CustomMetricEvaluator')(); logger_msg="class 'CustomMetricEvaluator'"
            else: self.logger.error(f"Custom mod {self.custom_metrics_module_path} lacks required func/class."); self.custom_metric_evaluator=None; return
            self.logger.info(f"Loaded custom metric {logger_msg} from: {self.custom_metrics_module_path}")
        except Exception as e: self.logger.error(f"Err loading custom metric mod {self.custom_metrics_module_path}: {e}"); self.custom_metric_evaluator=None
    def _get_rns_paths(self, dest_hash_bytes):
        if not RNS_AVAILABLE or not self.rns_instance:
            self.logger.warning("RNS not available for path finding.")
            return []
        try:
            # Assuming RNS has a way to find paths, e.g., via destination
            # This is conceptual; actual API may differ
            dest = RNS.Destination.recall(dest_hash_bytes)
            if not dest:
                dest = RNS.Destination(dest_hash_bytes, direction=RNS.Destination.OUT)
            paths = dest.paths  # Assuming dest has a paths attribute
            return list(paths) if paths else []
        except Exception as e:
            self.logger.error(f"Error finding RNS paths for {dest_hash_bytes.hex()[:8]}: {e}")
            return []
    def _measure_rtt_for_path(self, path_info_or_id):
        if not RNS_AVAILABLE or not self.rns_instance:
            return random.uniform(0.05, 0.5)  # Fallback
        try:
            # Conceptual: probe RTT via RNS
            # Assuming path_info has a probe method or use RNS.Transport.probe
            # For now, simulate
            self.logger.debug(f"Probing RTT for path {path_info_or_id}")
            # Placeholder for actual probe
            return random.uniform(0.05, 0.5)
        except Exception as e:
            self.logger.error(f"Error measuring RTT for path {path_info_or_id}: {e}")
            return float('inf')
    def _get_metric_for_path(self, path_info, metric_type):
        path_id = getattr(path_info,'path_id',str(path_info)); cache = self.path_metrics_cache.setdefault(path_id,{})
        cached = cache.get(metric_type); now = time.time()
        if cached and (now - cached.get('timestamp',0)) < self.metric_update_interval/2: return cached['value']
        value = float('inf')
        if metric_type=='rtt': value=self._measure_rtt_for_path(path_info)
        elif metric_type=='hops': value=getattr(path_info,'hops',float('inf'))
        elif metric_type=='link_quality': value=getattr(path_info,'quality',0) # Assume lower is better cost
        elif metric_type=='custom' and self.custom_metric_evaluator:
            try: value=self.custom_metric_evaluator(path_info,self.rns_instance)
            except Exception as e: self.logger.error(f"Err eval custom metric path {path_id}: {e}"); value=float('inf')
        cache[metric_type]={'value':value,'timestamp':now}; return value
    def get_best_path(self, dest_hash_hex):
        if not self.rns_instance: self.logger.warning("PathSel needs RNS instance."); return None
        dest_hash_bytes=bytes.fromhex(dest_hash_hex); paths=self._get_rns_paths(dest_hash_bytes)
        if not paths: self.logger.debug(f"No RNS paths for {dest_hash_hex[:8]}."); return None
        self.known_paths[dest_hash_hex]=paths; evaluated=[]
        for p_info in paths[:self.max_paths_to_consider]: metric_val=self._get_metric_for_path(p_info,self.default_metric_type); evaluated.append({'path_info':p_info,'metric_value':metric_val}); self.logger.debug(f"Path {getattr(p_info,'path_id','N/A')} to {dest_hash_hex[:8]}: {self.default_metric_type}={metric_val}")
        if not evaluated: self.logger.warning(f"No paths evaluated for {dest_hash_hex[:8]}."); return None
        evaluated.sort(key=lambda x:x['metric_value']); best=evaluated[0]
        self.logger.info(f"Best path for {dest_hash_hex[:8]} via {getattr(best['path_info'],'path_id','N/A')} with {self.default_metric_type}={best['metric_value']:.4f}")
        if self.metrics_monitor: self.metrics_monitor.path_selection_evaluations_total.inc(); self.metrics_monitor.path_selection_chosen_metric_value.labels(dest_hash=dest_hash_hex,metric_type=self.default_metric_type).set(best['metric_value'] if best['metric_value']!=float('inf') else -1)
        return best['path_info']
    def periodic_update(self):
        now = time.time()
        interval = self.metric_update_interval
        if (now - self._last_metric_update_time) < interval:
            return
        self.logger.info("PathSel periodic update...")
        self._last_metric_update_time = now
        # Refresh metrics for known paths
        for dest_hex, paths in list(self.known_paths.items()):
            for path_info in paths[:self.max_paths_to_consider]:
                self._get_metric_for_path(path_info, self.default_metric_type)
        # Optionally, remove old known_paths if not used recently
    def influence_rns_routing(self, dest_hash_hex, chosen_path_id):
        self.logger.info(f"Influencing RNS routing for {dest_hash_hex[:8]} via path {chosen_path_id}")
        # Conceptual: Use RNS API to influence routing, e.g., set preferred path
        # Assuming RNS.Transport.influence_path or similar
        if RNS_AVAILABLE and self.rns_instance:
            try:
                # Placeholder for actual influence
                pass
            except Exception as e:
                self.logger.error(f"Error influencing routing: {e}")
    def stop(self): self.logger.info("PathSelector stopping.")
