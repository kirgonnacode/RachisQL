import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from typing import Literal
from ..dates import Period, parse_period
from ..llm_client import generate_structured
from .catalog import Catalog, Metric


@dataclass(frozen=True)
class MonthlyPlan:
    metric: Metric
    period: Period
    shape: Literal["value", "dynamics"]


_SYSTEM = """
Определи, можно ли полностью ответить на вопрос одной доступной метрикой.
Метрика считает отделения в заданном статусе за каждый календарный месяц.
Ответь JSON-объектом с полем decision.

value — пользователь просит количество за один месяц.
dynamics — пользователь просит показать количество отдельно по месяцам.
unsupported — для полного ответа нужны другие данные или другой расчёт.

Выбирай unsupported, если вопрос содержит дополнительное условие по району,
причине закрытия или другому признаку, просит оба статуса, долю, сравнение,
рейтинг, список отделений или единый итог за несколько месяцев.
Различай состояние «закрыто» и событие «закрылось за месяц».
Если доступная метрика отвечает только на часть вопроса, выбирай unsupported.
""".strip()

_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["value", "dynamics", "unsupported"],
        }
    },
    "required": ["decision"],
    "additionalProperties": False,
}


async def plan_monthly_ops(
    question: str,
    catalog: Catalog,
    *,
    today: date,
) -> MonthlyPlan | None:
    metric = next((item for item in catalog.metrics if item.id == "ops_closed"), None)
    if metric is None:
        return None

    period = parse_period(question, today)
    if period is None or period.as_of:
        return None

    last_day = monthrange(period.end.year, period.end.month)[1]
    if period.start.day != 1 or period.end.day != last_day:
        return None

    prompt = json.dumps(
        {
            "module": catalog.module,
            "metric": metric.name,
            "synonyms": metric.synonyms,
            "filters": dict(metric.filters),
            "period_start": period.start.isoformat(),
            "period_end": period.end.isoformat(),
            "question": question,
        },
        ensure_ascii=False,
    )
    raw = await generate_structured(
        system=_SYSTEM,
        prompt=prompt,
        schema=_DECISION_SCHEMA,
    )
    result = json.loads(raw)
    if not isinstance(result, dict) or set(result) != {"decision"}:
        raise ValueError("Ollama вернула неожиданную структуру решения")

    decision = result["decision"]
    if decision == "unsupported":
        return None
    if decision == "dynamics":
        return MonthlyPlan(metric, period, "dynamics")
    if decision == "value":
        if (period.start.year, period.start.month) != (period.end.year, period.end.month):
            return None
        return MonthlyPlan(metric, period, "value")

    raise ValueError(f"Неизвестное решение планировщика: {decision!r}")