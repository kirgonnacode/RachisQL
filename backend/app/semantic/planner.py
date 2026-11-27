import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from typing import Literal
from ..dates import Period, parse_period
from ..llm_client import generate_structured
from ..value_resolver import resolve_dictionary_value
from .catalog import Catalog, Metric, MonthlyCount, NumericAggregate


@dataclass(frozen=True)
class MetricPlan:
    metric_id: str
    period: Period
    shape: Literal["value", "dynamics"]
    dimension_values: tuple[str, ...] = ()    


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


_NUMERIC_RULES = """
Для numeric_aggregate выбери метрику, только если её мера и операция
полностью отвечают на вопрос. Измерение dimension задаёт доступный фильтр.
Для value требуется одно значение измерения и один месяц.
Для dynamics укажи все явно запрошенные значения измерения в порядке вопроса.

Добавь в JSON поле dimension_values — массив строк. Названия приводи
в именительном падеже, сохраняй их смысл и уровень детализации.
Не подменяй значения самостоятельно: сопоставление выполняет словарь.
Если нельзя учесть все значения, период, товар или другое условие вопроса,
верни {"metric_id": null, "shape": "unsupported", "dimension_values": []}.
Для метрик других типов передавай пустой массив.
""".strip()


def _metric_card(metric: Metric, catalog: Catalog) -> dict:
    card = metric.model_dump(include={"id", "name", "description", "synonyms", "calculation"})
    if isinstance(metric, MonthlyCount):
        card["filters"] = metric.filters
        card["group_by"] = [item.label for item in metric.group_by]
        card["output"] = [metric.period_label, *card["group_by"], metric.value_label]
    elif isinstance(metric, NumericAggregate):
        dimension = catalog.dimensions[metric.dimension]
        card["operation"] = metric.operation
        card["measure"] = metric.measure.label
        card["dimension"] = dimension.label
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
    has_numeric = any(isinstance(metric, NumericAggregate) for metric in catalog.metrics)
    system = _SYSTEM
    if has_numeric:
        schema["properties"]["dimension_values"] = {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        }
        schema["required"].append("dimension_values")
        system = f"{_SYSTEM}\n\n{_NUMERIC_RULES}"
    prompt = json.dumps(
        {
            "module": catalog.module,
            "metrics": [_metric_card(metric, catalog) for metric in catalog.metrics],
            "period_start": period.start.isoformat(),
            "period_end": period.end.isoformat(),
            "question": question,
        },
        ensure_ascii=False,
    )
    raw = await generate_structured(system=system, prompt=prompt, schema=schema)
    result = json.loads(raw)
    if not isinstance(result, dict) or set(result) != set(schema["required"]):
        raise ValueError("Invalid metric decision fields")

    metric_id = result["metric_id"]
    shape = result["shape"]
    values = result.get("dimension_values", [])
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError("Invalid dimension values")

    if metric_id is None and shape == "unsupported":
        return None
    if metric_id not in metric_ids or shape not in ("value", "dynamics"):
        raise ValueError("Invalid metric id or shape")
    if shape == "value" and (period.start.year, period.start.month) != (period.end.year, period.end.month):
        return None

    metric = next(item for item in catalog.metrics if item.id == metric_id)
    if not isinstance(metric, NumericAggregate):
        if values:
            return None
        return MetricPlan(metric_id, period, shape)

    if not values:
        return None

    dimension = catalog.dimensions[metric.dimension]
    resolved: list[str] = []
    for value in values:
        if not value.strip():
            return None
        canonical = resolve_dictionary_value(dimension.dictionary, value.strip())
        if not canonical:
            return None
        if canonical not in resolved:
            resolved.append(canonical)

    if shape == "value" and len(resolved) != 1:
        return None

    return MetricPlan(metric_id, period, shape, tuple(resolved))