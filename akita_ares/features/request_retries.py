import time, random
from akita_ares.core.logger import get_logger
RNS_RETRYABLE_EXCEPTIONS = (Exception,)
class RetryManager:
    def __init__(self, config, metrics_monitor=None):
        self.logger = get_logger("Feature.RetryManager"); self.metrics_monitor = metrics_monitor
        self.stats = {'total_executions': 0, 'successes': 0, 'failures_after_retries': 0, 'successes_on_retry': 0}
        self.update_config(config)
    def update_config(self, config):
        self.config = config; self.default_max_retries=config.get('default_max_retries',3); self.default_delay_seconds=config.get('default_delay_seconds',1); self.default_backoff_factor=config.get('default_backoff_factor',2); self.default_jitter_max_seconds=config.get('default_jitter_max_seconds',0.5); self.log_retries=config.get('log_retries',True)
        self.logger.info(f"RetryMan cfg updated: MaxR={self.default_max_retries}, Delay={self.default_delay_seconds}s...")
    def _calc_delay(self, att, base_d, back_f, jit_max): delay=base_d*(back_f**(att-1)); delay=max(0,delay+random.uniform(-jit_max,jit_max)) if jit_max>0 else delay; return delay
    def exec_w_retry(self, op_func, *args, max_r=None,delay_s=None,back_f=None,jit_max_s=None,retry_ex=None,op_name="UnnamedOp",**kwargs):
        self.stats['total_executions'] += 1; required_retries = 0
        _mr,_d,_b,_j = max_r or self.default_max_retries,delay_s or self.default_delay_seconds,back_f or self.default_backoff_factor,jit_max_s or self.default_jitter_max_seconds
        _rx = retry_ex or RNS_RETRYABLE_EXCEPTIONS
        if not isinstance(_rx,tuple): self.logger.error("retryable_exceptions must be tuple"); _rx=(Exception,)
        last_ex,start_t = None,time.monotonic()
        for att in range(1,_mr+2):
            try:
                if self.log_retries and att>1: self.logger.info(f"Att {att}/{_mr+1} for '{op_name}'.")
                res=op_func(*args,**kwargs)
                self.stats['successes'] += 1; success = True
                if att>1: self.stats['successes_on_retry'] += 1; required_retries = att - 1
                if self.metrics_monitor: dur=time.monotonic()-start_t; self.metrics_monitor.record_operation_duration(op_name,dur); self.metrics_monitor.update_retry_stats(op_name, success=True, required_retries=required_retries)
                return res
            except _rx as e:
                last_ex=e
                if self.log_retries: self.logger.warning(f"Op '{op_name}' att {att} fail: {e.__class__.__name__}: {e}")
                if att>_mr:
                    self.stats['failures_after_retries'] += 1; success = False
                    self.logger.error(f"Op '{op_name}' failed after {att-1} retries. Err: {e}")
                    if self.metrics_monitor: dur=time.monotonic()-start_t; self.metrics_monitor.record_operation_duration(op_name,dur); self.metrics_monitor.update_retry_stats(op_name, success=False, required_retries=att-1)
                    raise last_ex
                cur_d=self._calc_delay(att,_d,_b,_j)
                if self.log_retries: self.logger.info(f"Retry Op '{op_name}' in {cur_d:.2f}s...")
                time.sleep(cur_d)
            except Exception as e:
                self.stats['failures_after_retries'] += 1; success = False
                self.logger.error(f"Op '{op_name}' non-retryable err: {e.__class__.__name__}: {e}")
                if self.metrics_monitor: dur=time.monotonic()-start_t; self.metrics_monitor.record_operation_duration(op_name,dur); self.metrics_monitor.update_retry_stats(op_name, success=False, required_retries=att-1)
                raise
        self.stats['failures_after_retries'] += 1
        if self.metrics_monitor: self.metrics_monitor.update_retry_stats(op_name, success=False, required_retries=_mr)
        if last_ex: raise last_ex
        raise Exception(f"Retry logic fail for {op_name}")
    def wrap_rns_req(self, rns_req_f, op_name_pref="RNSReq"):
        def wr(*a,**kw): op_n=op_name_pref; dest=kw.get('destination',a[0] if a else None); op_n=f"{op_name_pref}.{dest.name_hash()[:8]}" if hasattr(dest,'name_hash') else op_n; return self.exec_w_retry(rns_req_f,*a,op_name=op_n,**kw)
        return wr
    def get_stats(self): return self.stats.copy()
