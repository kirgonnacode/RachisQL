from dataclasses import dataclass
from datetime import date
from typing import Literal
from .dates import Period


@dataclass(frozen=True)
class Coverage:
    status: Literal["outside", "partial", "within_bounds"]
    available_from: date
    available_to: date


def compare_bounds(
    requested: Period,
    available_from: date | None,
    available_to: date | None,
    *,
    use_prior_snapshot: bool,
) -> Coverage | None:
    if available_from is None or available_to is None or available_from > available_to:
        return None

    if requested.as_of and use_prior_snapshot:
        status = "outside" if requested.end < available_from else "within_bounds"
    elif requested.end < available_from or requested.start > available_to:
        status = "outside"
    elif requested.start < available_from or requested.end > available_to:
        status = "partial"
    else:
        status = "within_bounds"

    return Coverage(status, available_from, available_to)


def partial_period_caption(requested: Period, coverage: Coverage) -> str | None:
    if coverage.status != "partial":
        return None

    start = max(requested.start, coverage.available_from)
    end = min(requested.end, coverage.available_to)

    return (
        f"Запрошено {requested.start:%d.%m.%Y}–{requested.end:%d.%m.%Y}"
        f" · В границах данных {start:%d.%m.%Y}–{end:%d.%m.%Y}"
    )