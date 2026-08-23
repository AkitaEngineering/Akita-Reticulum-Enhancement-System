import copy
import json
import os

from jsonschema import validators
from jsonschema import exceptions as jsonschema_exceptions

from .logger import get_logger


logger = get_logger("ConfigManager")


class ConfigManager:
    """Load, validate, and atomically reload an ARES JSON configuration."""

    def __init__(self, config_fp, schema_fp=None, validate_on_load=True):
        self.config_fp = os.path.abspath(os.path.expanduser(os.fspath(config_fp)))
        self.schema_path = (
            os.path.abspath(os.path.expanduser(os.fspath(schema_fp))) if schema_fp else None
        )
        self.config = {}
        self.schema = None
        self.validate_on_load = bool(validate_on_load)
        self.schema_load_error = None
        self.last_load_error = None
        self.last_reload_succeeded = None

        if self.schema_path:
            self._load_schema()
            if self.schema_load_error is not None and not isinstance(
                self.schema_load_error, FileNotFoundError
            ):
                raise ValueError(
                    f"Failed to load schema '{self.schema_path}': {self.schema_load_error}"
                ) from self.schema_load_error
        self.load_config()

    def _load_schema(self):
        logger.debug("Loading schema: %s", self.schema_path)
        try:
            with open(self.schema_path, "r", encoding="utf-8") as schema_file:
                schema = json.load(schema_file)
            validator_class = validators.validator_for(schema)
            validator_class.check_schema(schema)
            self.schema = schema
            self.schema_load_error = None
            logger.info("Schema loaded: %s", self.schema_path)
        except (OSError, json.JSONDecodeError, jsonschema_exceptions.SchemaError) as exc:
            logger.error("Error loading schema %s: %s", self.schema_path, exc)
            self.schema = None
            self.schema_load_error = exc

    def load_config(self):
        """Load configuration, preserving the last valid value on failure."""
        logger.debug("Loading config: %s", self.config_fp)
        previous_config = self.config
        try:
            with open(self.config_fp, "r", encoding="utf-8") as config_file:
                candidate = json.load(config_file)
            if not isinstance(candidate, dict):
                raise ValueError("configuration root must be a JSON object")
            if self.schema and self.validate_on_load:
                self.validate_config_schema(candidate)
            self.config = candidate
            self.last_load_error = None
            logger.info("Config loaded: %s", self.config_fp)
            return True
        except (
            OSError,
            json.JSONDecodeError,
            jsonschema_exceptions.ValidationError,
            ValueError,
        ) as exc:
            self.last_load_error = exc
            self.config = previous_config
            logger.error("Unable to load config %s: %s", self.config_fp, exc)
            return False

    def validate_config_schema(self, cfg_data):
        if not self.schema:
            if self.schema_path:
                logger.warning("Schema '%s' not loaded; validation skipped.", self.schema_path)
            return
        validator_class = validators.validator_for(self.schema)
        validator_class(self.schema).validate(cfg_data)
        logger.info("Config schema validation OK.")

    def require_valid(self, require_schema=True):
        """Raise a useful error when configuration is unsafe to use."""
        if self.last_load_error is not None:
            raise ValueError(
                f"Failed to load configuration '{self.config_fp}': {self.last_load_error}"
            ) from self.last_load_error
        if require_schema and self.schema_path and self.schema is None:
            error = self.schema_load_error or FileNotFoundError(self.schema_path)
            raise ValueError(f"Failed to load schema '{self.schema_path}': {error}") from error
        return True

    def get_config(self):
        return copy.deepcopy(self.config)

    def get_section(self, section_name, default=None):
        value = self.config.get(section_name, default if default is not None else {})
        return copy.deepcopy(value)

    def reload_config(self):
        logger.info("Reloading config: %s", self.config_fp)
        old_config = copy.deepcopy(self.config)
        loaded = self.load_config()
        self.last_reload_succeeded = loaded
        if not loaded:
            logger.error("Config reload rejected; continuing with the last valid configuration.")
            return self.get_config()
        if old_config != self.config:
            logger.info("Config changed after reload.")
        else:
            logger.info("Config unchanged after reload.")
        return self.get_config()
