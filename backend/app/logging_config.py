import logging
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
        file_handler = logging.FileHandler("/var/log/RachisQL/app.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except (FileNotFoundError, PermissionError):
        logger.warning("Не удалось открыть файл лога, пишу только в stdout")

    return logger


logger = setup_logging()