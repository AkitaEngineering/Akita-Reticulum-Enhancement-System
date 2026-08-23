import logging
import logging.handlers
import os
import sys

ARES_LOGGER_NAME = "ARES"
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _coerce_log_level(level):
    normalized = str(level).upper()
    if normalized not in VALID_LOG_LEVELS:
        raise ValueError(f"Invalid log level: {level!r}")
    return normalized, getattr(logging, normalized)


def setup_logging(
    level="INFO",
    log_file="ares.log",
    max_bytes=10 * 1024 * 1024,
    backup_count=5,
    console_output=True,
    module_levels=None,
):
    normalized_level, log_level = _coerce_log_level(level)
    if int(max_bytes) <= 0:
        raise ValueError("max_bytes must be greater than zero")
    if int(backup_count) < 0:
        raise ValueError("backup_count cannot be negative")

    root_logger = logging.getLogger(ARES_LOGGER_NAME)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.propagate = False
    root_logger.setLevel(log_level)
    for logger_name, logger_object in logging.Logger.manager.loggerDict.items():
        if logger_name.startswith(f"{ARES_LOGGER_NAME}.") and isinstance(
            logger_object, logging.Logger
        ):
            logger_object.setLevel(logging.NOTSET)
    formatter = logging.Formatter(
        "%(asctime)s-%(name)s-%(levelname)s-%(module)s:%(lineno)d-%(message)s"
    )

    if console_output:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    if log_file:
        try:
            absolute_file = os.path.abspath(os.path.expanduser(os.fspath(log_file)))
            log_file_existed = os.path.exists(absolute_file)
            log_directory = os.path.dirname(absolute_file)
            if log_directory:
                os.makedirs(log_directory, mode=0o750, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                absolute_file,
                maxBytes=int(max_bytes),
                backupCount=int(backup_count),
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
            if not log_file_existed:
                os.chmod(absolute_file, 0o640)
        except OSError as exc:
            print(f"CRITICAL: Failed file logger {log_file}: {exc}.", file=sys.stderr)
            if not root_logger.handlers:
                fallback_handler = logging.StreamHandler(sys.stderr)
                fallback_handler.setFormatter(formatter)
                root_logger.addHandler(fallback_handler)
            raise

    if not root_logger.handlers:
        root_logger.addHandler(logging.NullHandler())
    update_module_log_levels(module_levels)
    root_logger.info("ARES logging initialized. Root level: %s.", normalized_level)


def update_module_log_levels(module_levels_dict):
    if not module_levels_dict:
        return
    root_logger = logging.getLogger(ARES_LOGGER_NAME)
    for name, level_text in module_levels_dict.items():
        try:
            normalized_level, level = _coerce_log_level(level_text)
            logger_name = (
                name if name.startswith(f"{ARES_LOGGER_NAME}.") else f"{ARES_LOGGER_NAME}.{name}"
            )
            logging.getLogger(logger_name).setLevel(level)
            root_logger.info("Set level for '%s' to %s", logger_name, normalized_level)
        except (TypeError, ValueError) as exc:
            root_logger.error("Unable to set module log level for %r: %s", name, exc)


def get_logger(name=None):
    return (
        logging.getLogger(f"{ARES_LOGGER_NAME}.{name}")
        if name
        else logging.getLogger(ARES_LOGGER_NAME)
    )


if not logging.getLogger(ARES_LOGGER_NAME).handlers:
    _default_handler = logging.StreamHandler(sys.stdout)
    _default_handler.setFormatter(
        logging.Formatter("%(asctime)s-%(name)s-%(levelname)s-%(message)s")
    )
    logging.getLogger(ARES_LOGGER_NAME).addHandler(_default_handler)
    logging.getLogger(ARES_LOGGER_NAME).setLevel(logging.WARNING)
    logging.getLogger(ARES_LOGGER_NAME).propagate = False
