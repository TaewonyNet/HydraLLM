import logging
import sys
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler

from src.core.config import settings

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def setup_logging(log_level: str | None = None) -> None:
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    log_level = log_level or settings.log_level
    if settings.debug:
        log_level = "DEBUG"

    # 한글 Windows(cp949) 환경에서도 안전하도록 stdout 을 UTF-8 로 재설정.
    # Python 3.7+ 에서는 reconfigure 지원. 실패해도 치명적이지 않으니 silent fallback.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    file_handler = RotatingFileHandler(
        "gateway.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handlers.append(file_handler)

    rid_filter = RequestIDFilter()
    for h in handlers:
        h.addFilter(rid_filter)

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(request_id)s] - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )

    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info(f"Logging configured at level: {log_level} (Writing to gateway.log)")


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    return logger


setup_logging()
