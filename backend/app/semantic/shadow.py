import asyncio
import json
from datetime import date
from pathlib import Path
from ..config import SEMANTIC_MODE, WREN_PROJECT_DIR
from ..db import run_internal_query
from ..logging_config import logger
from .catalog import Catalog, CatalogError, MonthlyCount, load_catalogs
from .compiler import compile_metric
from .planner import plan_metric


_SHADOW_LOG = Path("/var/log/RachisQL/shadow.jsonl")


async def load_shadow_catalogs() -> dict[str, Catalog]:
    if SEMANTIC_MODE == "off":
        return {}
    if SEMANTIC_MODE != "shadow":
        logger.warning("Unknown SEMANTIC_MODE=%r", SEMANTIC_MODE)
        return {}

    directory = Path(WREN_PROJECT_DIR) / "knowledge" / "metrics"
    try:
        catalogs = load_catalogs(directory)
    except (CatalogError, OSError):
        logger.exception("Could not load metric catalogs from %s", directory)
        return {}

    active = {}
    for module, catalog in catalogs.items():
        valid = []
        for dataset_id, dataset in catalog.datasets.items():
            metrics = [item for item in catalog.metrics if item.dataset == dataset_id]
            if not metrics:
                continue
            try:
                rows = await run_internal_query(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = $1 AND table_name = $2",
                    dataset.schema_name,
                    dataset.table,
                )
            except Exception:
                logger.exception("Could not check dataset %s in module %s", dataset_id, module)
                continue
            available = {row["column_name"] for row in rows}
            for metric in metrics:
                required = {dataset.time_column}
                if isinstance(metric, MonthlyCount):
                    required.update(metric.filters)
                    required.update(item.column for item in metric.group_by)
                else:
                    required.update(item.column for item in metric.columns)
                missing = required - available
                if missing:
                    logger.warning("Skipping %s/%s: missing columns %s", module, metric.id, sorted(missing))
                else:
                    valid.append(metric)
        if valid:
            active[module] = catalog.model_copy(update={"metrics": valid})

    logger.info("Shadow catalogs active: %s", {name: len(c.metrics) for name, c in active.items()})
    return active


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
        plan = await plan_metric(question, catalog, today=today)
        if plan is None:
            record["status"] = "unsupported"
        else:
            query = compile_metric(catalog, plan.metric_id, plan.period)
            rows = await run_internal_query(query.sql, *query.params)
            record.update(
                status="ok",
                metric=plan.metric_id,
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