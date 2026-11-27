from dataclasses import dataclass
from datetime import date
from ..dates import Period
from .catalog import Metric


class UnsupportedQuery(ValueError):
    pass


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    params: tuple[date | str, ...]


_OPS_CLOSED_SQL = """
SELECT
    date_trunc('month', otchet_data) AS "Дата",
    status_ops AS "Статус ОПС",
    count(*) AS "Количество ОПС"
FROM public.mail_v2
WHERE otchet_data >= $1::date
  AND otchet_data < ($2::date + 1)
  AND status_ops = $3
GROUP BY date_trunc('month', otchet_data), status_ops
ORDER BY 1, 2
""".strip()


def compile_monthly_ops_closed(metric: Metric, period: Period) -> CompiledQuery:
    if metric.id != "ops_closed":
        raise UnsupportedQuery(f"Ожидалась метрика ops_closed, получена {metric.id!r}")

    dataset = metric.dataset
    if (
        dataset.name != "mail_v2"
        or dataset.time != "otchet_data"
        or dataset.kind != "snapshot"
        or dataset.grain != "month"
        or metric.value != "count(*)"
    ):
        raise UnsupportedQuery("Описание ops_closed не соответствует месячному расчёту")

    filters = dict(metric.filters)
    if filters.keys() != {"status_ops"}:
        raise UnsupportedQuery("Для ops_closed ожидается фильтр status_ops")

    if period.as_of or period.start > period.end:
        raise UnsupportedQuery("Для месячного расчёта нужен календарный интервал")

    return CompiledQuery(
        sql=_OPS_CLOSED_SQL,
        params=(period.start, period.end, filters["status_ops"]),
    )