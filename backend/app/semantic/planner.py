import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from typing import Literal
from ..dates import Period, parse_period
from ..llm_client import generate_structured
from .catalog import Catalog, Metric, MonthlyCount


@dataclass(frozen=True)
class MetricPlan:
    metric_id: str
    period: Period
    shape: Literal["value", "dynamics"]


_SYSTEM = """
Выбери одну метрику каталога, если она полностью отвечает на вопрос.
Учитывай описание, фиксированные фильтры, колонки результата и период.
monthly_count считает строки по месяцам с указанными фильтрами и группировкой.
latest_snapshot_per_month возвращает колонки всех строк последнего среза
каждого месяца. Запрос распределения за год подходит, если именно такой
помесячный результат предусмотрен описанием выбранной метрики.

Верни JSON с полями metric_id и shape. value означает результат за один
месяц, dynamics — ряд по месяцам. Единый итог за несколько месяцев,
новый фильтр, иной показатель или другая группировка требуют другого расчёта.
Если ни одна метрика полностью не подходит или выбор неоднозначен,
верни {"metric_id": null, "shape": "unsupported"}.
""".strip()


def _metric_card(metric: Metric) -> dict:
    card = metric.model_dump(include={"id", "name", "description", "synonyms", "calculation"})
    if isinstance(metric, MonthlyCount):
        card["filters"] = metric.filters
        card["group_by"] = [item.label for item in metric.group_by]
        card["output"] = [metric.period_label, *card["group_by"], metric.value_label]
    else:
        card["output"] = [metric.period_label, *(item.label for item in metric.columns)]
    return card


async def plan_metric(question: str, catalog: Catalog, *, today: date) -> MetricPlan | None:
    period = parse_period(question, today)
    if period is None or period.as_of:
        return None
    if period.start.day != 1 or period.end.day != monthrange(period.end.year, period.end.month)[1]:
        return None

    metric_ids = [metric.id for metric in catalog.metrics]
    schema = {
        "type": "object",
        "properties": {
            "metric_id": {"enum": [*metric_ids, None]},
            "shape": {"type": "string", "enum": ["value", "dynamics", "unsupported"]},
        },
        "required": ["metric_id", "shape"],
        "additionalProperties": False,
    }
    prompt = json.dumps(
        {
            "module": catalog.module,
            "metrics": [_metric_card(metric) for metric in catalog.metrics],
            "period_start": period.start.isoformat(),
            "period_end": period.end.isoformat(),
            "question": question,
        },
        ensure_ascii=False,
    )
    raw = await generate_structured(system=_SYSTEM, prompt=prompt, schema=schema)
    result = json.loads(raw)
    if not isinstance(result, dict) or set(result) != {"metric_id", "shape"}:
        raise ValueError("Invalid metric decision fields")

    metric_id = result["metric_id"]
    shape = result["shape"]
    if metric_id is None and shape == "unsupported":
        return None
    if metric_id not in metric_ids or shape not in ("value", "dynamics"):
        raise ValueError("Invalid metric id or shape")
    if shape == "value" and (period.start.year, period.start.month) != (period.end.year, period.end.month):
        return None
    return MetricPlan(metric_id, period, shape)