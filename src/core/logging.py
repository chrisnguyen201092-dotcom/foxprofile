import logging
import os
import pathlib
import sys
from datetime import datetime


def setup_logging(
    log_dir: str = "logs",
    log_level: int = logging.INFO,
) -> logging.Logger:
    logger = logging.getLogger("foxprofile")
    logger.setLevel(log_level)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    in_pytest = "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ
    is_default_log_dir = pathlib.Path(log_dir).resolve() == pathlib.Path("logs").resolve()
    if not (in_pytest and is_default_log_dir):
        pathlib.Path(log_dir).mkdir(exist_ok=True, parents=True)
        log_filename = datetime.now().strftime("foxprofile_%Y%m%d.log")
        log_path = os.path.join(log_dir, log_filename)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    if name:
        return logging.getLogger(f"foxprofile.{name}")
    return logging.getLogger("foxprofile")
