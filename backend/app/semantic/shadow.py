import asyncio
import json
from datetime import date
from pathlib import Path
from ..config import SEMANTIC_MODE, WREN_PROJECT_DIR
from ..db import run_internal_query
from ..logging_config import logger
from .catalog import Catalog, CatalogError, load_catalog
from .compiler import compile_monthly_ops_closed
from .planner import plan_monthly_ops


_SHADOW_LOG = Path("/var/log/RachisQL/shadow.jsonl")


async def load_shadow_catalog() -> Catalog | None:
    if SEMANTIC_MODE == "off":
        return None
    if SEMANTIC_MODE != "shadow":
        logger.warning("Неизвестный SEMANTIC_MODE=%r, теневой режим выключен", SEMANTIC_MODE)
        return None

    path = Path(WREN_PROJECT_DIR) / "knowledge" / "metrics" / "mail.yml"
    try:
        catalog = load_catalog(path)
        if catalog.module != "Почта":
            raise CatalogError(f"Ожидался модуль Почта, получен {catalog.module!r}")

        metric = next((item for item in catalog.metrics if item.id == "ops_closed"), None)
        if metric is None:
            raise CatalogError("В каталоге Почты отсутствует ops_closed")

        rows = await run_internal_query(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1
            """,
            metric.dataset.name,
        )
        required = {metric.dataset.time, *dict(metric.filters)}
        missing = required - {row["column_name"] for row in rows}
        if missing:
            raise CatalogError(f"В {metric.dataset.name} отсутствуют колонки: {sorted(missing)}")
    except Exception:
        logger.exception("Теневой режим Почты недоступен")
        return None

    logger.info("Теневой режим Почты включён, каталог: %s", path)
    return catalog


def _append_record(record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _SHADOW_LOG.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


async def run_shadow(
    catalog: Catalog,
    question: str,
    today: date,
    query_id: str,
) -> None:
    record = {"query_id": query_id, "module": catalog.module, "question": question}
    try:
        plan = await plan_monthly_ops(question, catalog, today=today)
        if plan is None:
            record["status"] = "unsupported"
        else:
            query = compile_monthly_ops_closed(plan.metric, plan.period)
            rows = await run_internal_query(query.sql, *query.params)
            record.update(
                status="ok",
                metric=plan.metric.id,
                shape=plan.shape,
                sql=query.sql,
                params=query.params,
                row_count=len(rows),
                rows=rows,
            )
    except Exception as exc:
        record.update(status="error", error_type=type(exc).__name__, error=str(exc))
        logger.exception("Ошибка теневого расчёта, query_id=%s", query_id)

    try:
        await asyncio.to_thread(_append_record, record)
    except OSError:
        logger.exception("Не удалось записать shadow.jsonl, query_id=%s", query_id)