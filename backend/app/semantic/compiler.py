from dataclasses import dataclass
from datetime import date
from ..dates import Period
from typing import Literal
from .catalog import (
    Catalog,
    Dataset,
    DictionaryDimension,
    LatestSnapshot,
    MonthlyCount,
    NumericAggregate,
)


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


def _numeric_aggregate(
    dataset: Dataset,
    metric: NumericAggregate,
    dimension: DictionaryDimension,
    period: Period,
    shape: Literal["value", "dynamics"],
    dimension_values: tuple[str, ...],
) -> CompiledQuery:
    values = tuple(dict.fromkeys(dimension_values))
    if not values or any(not value.strip() for value in values):
        raise UnsupportedQuery("Expected at least one resolved dimension value")

    if shape == "value":
        if len(values) != 1:
            raise UnsupportedQuery("Expected one dimension value for a scalar result")
        if (period.start.year, period.start.month) != (period.end.year, period.end.month):
            raise UnsupportedQuery("Expected one month for a scalar result")

    time_column = _ident(dataset.time_column)
    dimension_column = _ident(dimension.column)
    measure_column = _ident(metric.measure.column)
    operation = {"sum": "SUM", "avg": "AVG"}[metric.operation]
    aggregate = f"{operation}({measure_column})"

    fields: list[str] = []
    if shape == "dynamics":
        month = f"date_trunc('month', {time_column})::date"
        fields.append(f"{month} AS {_ident(metric.period_label)}")

    placeholders = [f"${i}::text" for i in range(3, 3 + len(values))]
    if len(values) == 1:
        fields.append(f"{aggregate} AS {_ident(metric.measure.label)}")
    else:
        labels = [dimension.display_labels.get(value, value) for value in values]
        if len({metric.period_label, *labels}) != len(labels) + 1:
            raise UnsupportedQuery("Duplicate output label in comparison")

        for label, placeholder in zip(labels, placeholders):
            fields.append(
                f"{aggregate} FILTER (WHERE {dimension_column} = {placeholder}) "
                f"AS {_ident(label)}"
            )

    sql = (
        f"SELECT {', '.join(fields)}\n"
        f"FROM {_table(dataset)}\n"
        f"WHERE {_in_period(time_column)}\n"
        f"  AND {dimension_column} IN ({', '.join(placeholders)})"
    )
    if shape == "dynamics":
        sql += "\nGROUP BY 1\nORDER BY 1"

    return CompiledQuery(sql, (period.start, period.end, *values))


def compile_metric(
    catalog: Catalog,
    metric_id: str,
    period: Period,
    *,
    shape: Literal["value", "dynamics"] | None = None,
    dimension_values: tuple[str, ...] = (),
) -> CompiledQuery:
    if period.as_of or period.start > period.end:
        raise UnsupportedQuery("Expected a calendar interval")

    metric = next((item for item in catalog.metrics if item.id == metric_id), None)
    if metric is None:
        raise UnsupportedQuery(f"Unknown metric: {metric_id!r}")

    dataset = catalog.datasets[metric.dataset]
    if isinstance(metric, NumericAggregate):
        if shape not in ("value", "dynamics"):
            raise UnsupportedQuery("Expected a result shape for a numeric aggregate")
        return _numeric_aggregate(
            dataset,
            metric,
            catalog.dimensions[metric.dimension],
            period,
            shape,
            dimension_values,
        )

    if dimension_values:
        raise UnsupportedQuery("This calculation does not support dimension values")
    if isinstance(metric, MonthlyCount):
        return _monthly_count(dataset, metric, period)
    if isinstance(metric, LatestSnapshot):
        return _latest_snapshot(dataset, metric, period)
    raise UnsupportedQuery(f"Unsupported calculation: {metric.calculation!r}")
