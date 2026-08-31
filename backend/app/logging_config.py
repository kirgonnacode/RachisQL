import logging
from logging.handlers import RotatingFileHandler
import sys


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("RachisQL")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    try:
        file_handler = RotatingFileHandler(
            "/var/log/RachisQL/app.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except (FileNotFoundError, PermissionError):
        logger.warning("Не удалось открыть файл лога, пишу только в stdout")

    return logger


logger = setup_logging()