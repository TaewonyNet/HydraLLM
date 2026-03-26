import logging
import sys
from logging.handlers import RotatingFileHandler

from src.core.config import settings


def setup_logging(log_level: str | None = None) -> None:
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    log_level = log_level or settings.log_level
    if settings.debug:
        log_level = "DEBUG"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    file_handler = RotatingFileHandler(
        "gateway.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
    handlers.append(file_handler)

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )

    logger = logging.getLogger(__name__)
    logger.info(f"Logging configured at level: {log_level} (Writing to gateway.log)")


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    return logger


setup_logging()
