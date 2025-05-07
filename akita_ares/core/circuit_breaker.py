import time; from enum import Enum; from akita_ares.core.logger import get_logger
logger=get_logger("CircuitBreaker"); class S(Enum): C,O,H="CLOSED","OPEN","HALF_OPEN"
class CircuitBreaker:
    def __init__(self,ft,rt,n="DefCB"): self.ft,self.rt,self.n,self.s,self.fc,self.lt=ft,rt,n,S.C,0,None; logger.info(f"CB '{n}' init: thr={ft},t/o={rt}s")
    def execute(self,f,*a,**k):
        if self.s==S.O:
            if self.lt and (time.monotonic()-self.lt)>self.rt: self._to_h()
            else: raise CircuitBreakerOpenException(f"CB '{self.n}' is OPEN")
        elif self.s==S.H:
            logger.debug(f"CB '{self.n}' H test: {f.__name__}"); try:r=f(*a,**k);self._ok();return r
            except Exception as e:self._fail();logger.error(f"CB '{self.n}' H fail:{e}");raise
        try: r=f(*a,**k); self._ok() if self.s==S.C else None; return r
        except Exception as e: self._fail() if self.s==S.C else None; logger.warning(f"CB '{self.n}' caught:{e}"); raise
    def _ok(self):
        if self.s==S.H: self._to_c()
        elif self.s==S.C and self.fc>0: logger.info(f"CB '{self.n}' ok, reset."); self.fc,self.lt=0,None
    def _fail(self): self.fc+=1;self.lt=time.monotonic(); logger.warning(f"CB '{self.n}' fail. Cnt:{self.fc}/{self.ft}"); self._to_o() if self.fc>=self.ft and self.s!=S.O else None
    def _to_c(self): logger.info(f"CB '{self.n}' to C."); self.s,self.fc,self.lt=S.C,0,None
    def _to_o(self): logger.warning(f"CB '{self.n}' to O for {self.rt}s."); self.s=S.O
    def _to_h(self): logger.info(f"CB '{self.n}' to H."); self.s=S.H
class CircuitBreakerOpenException(Exception): pass
