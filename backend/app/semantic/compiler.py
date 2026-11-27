from dataclasses import dataclass
from datetime import date
from ..dates import Period
from .catalog import Catalog, Dataset, LatestSnapshot, MonthlyCount


class UnsupportedQuery(ValueError):
    pass


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    params: tuple[date | str, ...]


def _ident(name: str) -> str:
    if not name or "\x00" in name or len(name.encode("utf-8")) > 63:
        raise UnsupportedQuery(f"Invalid PostgreSQL identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


def _table(dataset: Dataset) -> str:
    return f"{_ident(dataset.schema_name)}.{_ident(dataset.table)}"


def _in_period(column: str) -> str:
    return f"{column} >= $1::date AND {column} < ($2::date + 1)"


def _monthly_count(dataset: Dataset, metric: MonthlyCount, period: Period) -> CompiledQuery:
    time_column = _ident(dataset.time_column)
    month = f"date_trunc('month', {time_column})"
    fields = [f"{month} AS {_ident(metric.period_label)}"]
    for item in metric.group_by:
        fields.append(f"{_ident(item.column)} AS {_ident(item.label)}")

    positions = ", ".join(str(i) for i in range(1, len(fields) + 1))
    fields.append(f"count(*) AS {_ident(metric.value_label)}")

    params = [period.start, period.end]
    conditions = [_in_period(time_column)]
    for column, value in metric.filters.items():
        params.append(value)
        conditions.append(f"{_ident(column)}::text = ${len(params)}::text")

    sql = (
        f"SELECT {', '.join(fields)}\n"
        f"FROM {_table(dataset)}\n"
        f"WHERE {' AND '.join(conditions)}\n"
        f"GROUP BY {positions}\n"
        f"ORDER BY {positions}"
    )
    return CompiledQuery(sql, tuple(params))


def _latest_snapshot(dataset: Dataset, metric: LatestSnapshot, period: Period) -> CompiledQuery:
    table = _table(dataset)
    time_column = _ident(dataset.time_column)
    fields = [f"{time_column} AS {_ident(metric.period_label)}"]
    for item in metric.columns:
        fields.append(f"{_ident(item.column)} AS {_ident(item.label)}")

    sql = (
        f"SELECT {', '.join(fields)}\n"
        f"FROM {table}\n"
        f"WHERE {time_column} IN (\n"
        f"    SELECT max({time_column})\n"
        f"    FROM {table}\n"
        f"    WHERE {_in_period(time_column)}\n"
        f"    GROUP BY date_trunc('month', {time_column})\n"
        f")\n"
        f"ORDER BY 1"
    )
    return CompiledQuery(sql, (period.start, period.end))


def compile_metric(catalog: Catalog, metric_id: str, period: Period) -> CompiledQuery:
    if period.as_of or period.start > period.end:
        raise UnsupportedQuery("Expected a calendar interval")

    metric = next((item for item in catalog.metrics if item.id == metric_id), None)
    if metric is None:
        raise UnsupportedQuery(f"Unknown metric: {metric_id!r}")

    dataset = catalog.datasets[metric.dataset]
    if isinstance(metric, MonthlyCount):
        return _monthly_count(dataset, metric, period)
    if isinstance(metric, LatestSnapshot):
        return _latest_snapshot(dataset, metric, period)
    raise UnsupportedQuery(f"Unsupported calculation: {metric.calculation!r}")
