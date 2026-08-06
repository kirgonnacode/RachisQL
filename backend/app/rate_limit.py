
import time
from collections import defaultdict
from .config import RATE_LIMIT_PER_MINUTE

_WINDOW_SECONDS = 60
_request_log: dict[str, list[float]] = defaultdict(list)


class RateLimitExceeded(Exception):
    def __init__(self, label: str):
        self.label = label
        super().__init__(f"Превышен лимит запросов ({RATE_LIMIT_PER_MINUTE}/мин) для '{label}'")


def check_rate_limit(label: str) -> None:
    now = time.monotonic()
    window_start = now - _WINDOW_SECONDS
    timestamps = _request_log[label]

    while timestamps and timestamps[0] < window_start:
        timestamps.pop(0)

    if len(timestamps) >= RATE_LIMIT_PER_MINUTE:
        raise RateLimitExceeded(label)

    timestamps.append(now)
