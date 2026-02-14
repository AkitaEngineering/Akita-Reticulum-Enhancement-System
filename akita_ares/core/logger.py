import logging, logging.handlers, sys, os
ARES_LOGGER_NAME="ARES"
def setup_logging(level='INFO',log_file='ares.log',max_bytes=10485760,backup_count=5,console_output=True,module_levels=None):
    root_logger=logging.getLogger(ARES_LOGGER_NAME)
    if root_logger.hasHandlers(): root_logger.handlers.clear()
    log_level=getattr(logging,level.upper(),logging.INFO); root_logger.setLevel(log_level)
    formatter=logging.Formatter('%(asctime)s-%(name)s-%(levelname)s-%(module)s:%(lineno)d-%(message)s')
    if console_output: ch=logging.StreamHandler(sys.stdout); ch.setFormatter(formatter); root_logger.addHandler(ch)
    if log_file:
        try:
            abs_f=os.path.abspath(log_file); log_d=os.path.dirname(abs_f)
            if log_d and not os.path.exists(log_d): os.makedirs(log_d,exist_ok=True)
            fh=logging.handlers.RotatingFileHandler(abs_f,maxBytes=max_bytes,backupCount=backup_count); fh.setFormatter(formatter); root_logger.addHandler(fh)
            print(f"INFO: Logging to file: {abs_f}")
        except Exception as e:
            print(f"CRITICAL: Failed file logger {log_file}: {e}.",file=sys.stderr)
            if not any(isinstance(h,logging.StreamHandler) for h in root_logger.handlers): ch=logging.StreamHandler(sys.stdout); ch.setFormatter(formatter); root_logger.addHandler(ch); print("WARNING: Fallback console logging.",file=sys.stderr)
    update_module_log_levels(module_levels); root_logger.info(f"ARES logging init. Root Level: {level.upper()}.")
def update_module_log_levels(module_levels_dict):
    if not module_levels_dict:
        return
    root_logger = logging.getLogger(ARES_LOGGER_NAME)
    root_logger.debug(f"Applying module levels: {module_levels_dict}")

    for name, lvl_str in module_levels_dict.items():
        try:
            mod_log = logging.getLogger(name)
            lvl = getattr(logging, lvl_str.upper(), None)

            if lvl is not None:
                mod_log.setLevel(lvl)
                root_logger.info(f"Set level for '{name}' to {lvl_str.upper()}")
            else:
                root_logger.warning(f"Invalid level '{lvl_str}' for logger '{name}'.")
        except Exception as e:
            root_logger.error(f"Err setting level for '{name}': {e}")
def get_logger(name=None): return logging.getLogger(f"{ARES_LOGGER_NAME}.{name}") if name else logging.getLogger(ARES_LOGGER_NAME)
if not logging.getLogger(ARES_LOGGER_NAME).hasHandlers(): _h=logging.StreamHandler(sys.stdout); _f=logging.Formatter('%(asctime)s-%(name)s-%(levelname)s-%(message)s'); _h.setFormatter(_f); logging.getLogger(ARES_LOGGER_NAME).addHandler(_h); logging.getLogger(ARES_LOGGER_NAME).setLevel(logging.WARNING)
