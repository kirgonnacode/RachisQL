"""Кэш границ отчётных периодов таблиц Wren."""

import asyncio
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from time import monotonic
import yaml
from .config import WREN_PROJECT_DIR
from .db import run_internal_query
from .logging_config import logger


_TTL = 3600
_MONTHS = "янв фев мар апр май июн июл авг сен окт ноя дек".split()


@dataclass(frozen=True)
class Bounds:
    first: date
    last: date


_cache: dict[str, tuple[float, dict[str | None, Bounds]]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _month(year: int, month: int) -> Bounds:
    return Bounds(date(year, month, 1), date(year, month, monthrange(year, month)[1]))


def _year(year: int) -> Bounds:
    return Bounds(date(year, 1, 1), date(year, 12, 31))


def _mixed_period(label: str, value: str) -> Bounds:
    text = value.strip()
    if label == "Последние 12 лет":
        return _year(int(text))
    if label in {"Год", "Последние 12 месяцев"}:
        month, year = text.lower().split()
        return _month(int(year), _MONTHS.index(month) + 1)
    if label == "Последние 12 недель":
        day = datetime.strptime(text, "%d.%m.%Y").date()
        return Bounds(day, day)
    raise ValueError(f"неизвестная metka {label!r}")


async def _load(model: str) -> dict[str | None, Bounds]:
    path = Path(WREN_PROJECT_DIR) / "models" / model / "metadata.yml"
    data = yaml.safe_load(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    props = data["properties"]
    kind = props.get("period_kind")
    if not kind or kind == "reference":
        return {}

    physical = {
        col["name"]: col.get("type", "").upper() for col in data["columns"]
        if not col.get("expression") and not col.get("is_calculated")
    }
    column = props["period_column"]
    typ = physical[column]
    ref = data["table_reference"]
    table = _quote(ref.get("schema") or "public") + "." + _quote(ref["table"])
    col = _quote(column)

    if kind == "mixed":
        label_column = props["period_label_column"]
        if label_column not in physical:
            raise ValueError(f"отсутствует физическая колонка {label_column}")
        label = _quote(label_column)
        rows = await run_internal_query(
            f"SELECT DISTINCT {label} AS label, {col} AS value FROM {table} "
            f"WHERE {col} IS NOT NULL AND {label} IS NOT NULL"
        )
        bounds = {}
        invalid = set()
        for row in rows:
            group = row["label"]
            if group in invalid:
                continue
            try:
                span = _mixed_period(group, row["value"])
            except (TypeError, ValueError, AttributeError):
                invalid.add(group)
                bounds.pop(group, None)
                logger.warning("Не удалось определить границы периода %s, metka=%r", model, group)
                continue
            previous = bounds.get(group)
            if previous:
                span = Bounds(min(previous.first, span.first), max(previous.last, span.last))
            bounds[group] = span
        return bounds

    numeric_year = kind == "year" and typ in {"INTEGER", "BIGINT", "SMALLINT"}
    if typ.startswith("TIMESTAMP") or typ == "DATE" or numeric_year:
        expr = col
    elif model == "mbr_population" and typ in {"TEXT", "VARCHAR"}:
        expr = f"CAST({col} AS date)"
    else:
        raise ValueError(f"неподдерживаемый тип оси {column}: {typ}")

    row, = await run_internal_query(f"SELECT MIN({expr}) AS lo, MAX({expr}) AS hi FROM {table}")
    lo, hi = row["lo"], row["hi"]
    if lo is None or hi is None:
        return {}
    if numeric_year:
        return {None: Bounds(_year(lo).first, _year(hi).last)}
    if isinstance(lo, datetime):
        lo, hi = lo.date(), hi.date()
    if kind == "month":
        return {None: Bounds(_month(lo.year, lo.month).first, _month(hi.year, hi.month).last)}
    if kind == "year":
        return {None: Bounds(_year(lo.year).first, _year(hi.year).last)}
    return {None: Bounds(lo, hi)}


async def get_bounds(model: str, label: str | None = None) -> Bounds | None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", model):
        return None
    path = Path(WREN_PROJECT_DIR) / "models" / model / "metadata.yml"
    if model not in _locks and not path.is_file():
        return None
    async with _locks.setdefault(model, asyncio.Lock()):
        entry = _cache.get(model)
        if entry and entry[0] > monotonic():
            return entry[1].get(label)
        try:
            periods = await _load(model)
        except Exception as exc:
            logger.warning("Границы отчётного периода %s недоступны: %s", model, exc)
            return None
        _cache[model] = (monotonic() + _TTL, periods)
        return periods.get(label)
