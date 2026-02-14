import json, os
from jsonschema import validate, exceptions as jsonschema_exceptions
from .logger import get_logger
logger = get_logger("ConfigManager")
class ConfigManager:
    def __init__(self, config_fp, schema_fp=None, validate_on_load=True):
        self.config_fp=os.path.expanduser(config_fp); self.schema_path=os.path.expanduser(schema_fp) if schema_fp else None
        self.config={}; self.schema=None; self.validate_on_load=validate_on_load
        if self.schema_path and os.path.exists(self.schema_path): self._load_schema()
        self.load_config()
    def _load_schema(self):
        logger.debug(f"Loading schema: {self.schema_path}")
        try:
            with open(self.schema_path,'r') as f: self.schema=json.load(f)
            logger.info(f"Schema loaded: {self.schema_path}")
        except Exception as e: logger.error(f"Err loading schema {self.schema_path}: {e}"); self.schema=None
    def load_config(self):
        logger.debug(f"Loading config: {self.config_fp}")
        tmp_cfg={}
        try:
            if not os.path.exists(self.config_fp): logger.error(f"Config file not found: {self.config_fp}"); self.config={}; return
            with open(self.config_fp,'r') as f: tmp_cfg=json.load(f)
            if self.schema and self.validate_on_load: self.validate_config_schema(tmp_cfg)
            self.config=tmp_cfg; logger.info(f"Config loaded: {self.config_fp}")
        except json.JSONDecodeError as e: logger.error(f"JSON decode err in {self.config_fp}: {e}"); self._handle_load_fail()
        except jsonschema_exceptions.ValidationError as e: logger.error(f"Config validation err: {e.message} (Path:{list(e.path)})"); self._handle_load_fail()
        except IOError as e: logger.error(f"IOError reading {self.config_fp}: {e}"); self._handle_load_fail()
        except Exception as e: logger.error(f"Unexpected err loading config: {e}"); self._handle_load_fail()
    def _handle_load_fail(self):
        # If there was no previously-loaded config, reset to an empty dict.
        # If a config was already loaded, keep it unchanged (do not set to None).
        if not self.config:
            self.config = {}

    def validate_config_schema(self, cfg_data):
        if not self.schema:
            if self.schema_path:
                logger.warning(f"Schema '{self.schema_path}' not loaded, skipping validation.")
            return

        logger.debug("Validating config vs schema...")
        validate(instance=cfg_data, schema=self.schema)
        logger.info("Config schema validation OK.")
    def get_config(self): return self.config
    def get_section(self, sec_name, default=None): return self.config.get(sec_name,default if default is not None else {})
    def reload_config(self):
        logger.info(f"Reloading config: {self.config_fp}..."); old_chk=hash(json.dumps(self.config,sort_keys=True))
        self.load_config(); new_chk=hash(json.dumps(self.config,sort_keys=True))
        logger.info("Config changed after reload.") if old_chk!=new_chk else logger.info("Config unchanged after reload.")
        return self.get_config()
