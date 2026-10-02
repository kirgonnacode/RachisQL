import logging
from logging.handlers import RotatingFileHandler
import sys
from .config import LOG_LEVEL
from .request_context import request_id


class RequestIdFilter(logging.Filter):

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        return True


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("RachisQL")
    logger.setLevel(getattr(logging, LOG_LEVEL.upper()))

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | [%(request_id)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    request_id_filter = RequestIdFilter()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(request_id_filter)
    logger.addHandler(stream_handler)

    try:
        file_handler = RotatingFileHandler(
            "/var/log/RachisQL/app.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(request_id_filter)
        logger.addHandler(file_handler)
    except (FileNotFoundError, PermissionError):
        logger.warning("Не удалось открыть файл лога, пишу только в stdout")

    return logger


logger = setup_logging()