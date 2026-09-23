from . import wren_client
from .db import run_internal_query
from .logging_config import logger
from pathlib import Path
import yaml
from .config import WREN_PROJECT_DIR

async def _introspect_postgres() -> str:
    rows = await run_internal_query(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """
    )

    tables: dict[str, list[str]] = {}
    for row in rows:
        tables.setdefault(row["table_name"], []).append(
            f"{row['column_name']} ({row['data_type']})"
        )

    lines = [f"Таблица {table}: " + ", ".join(cols) for table, cols in tables.items()]
    return "\n".join(lines)


def _describe_model(model_name: str) -> str:
    metadata_file = Path(WREN_PROJECT_DIR) / "models" / model_name / "metadata.yml"
    if not metadata_file.is_file():
        return ""
    try:
        data = yaml.safe_load(metadata_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        logger.warning("Не удалось прочитать %s: %s", metadata_file, e)
        return ""

    table_desc = (data.get("properties") or {}).get("description", "")
    header = f"Таблица '{model_name}'" + (f" — {table_desc}" if table_desc else "") + ":"
    lines = [header]
    for col in data.get("columns", []) or []:
        col_name = col.get("name", "?")
        col_type = col.get("type", "")
        col_desc = (col.get("properties") or {}).get("description", "")
        pk_marker = " [PRIMARY KEY]" if col.get("is_primary_key") else ""
        line = f"  - {col_name} ({col_type}){pk_marker}"
        if col_desc:
            line += f": {col_desc}"
        lines.append(line)

    return "\n".join(lines)


async def get_schema_context(question: str) -> str:
    if wren_client.is_configured():
        try:
            model_names = await wren_client.fetch_relevant_models(question)
            if model_names:
                descriptions = [_describe_model(name) for name in model_names]
                context = "\n\n".join(d for d in descriptions if d)
                logger.info("Схема собрана через wren search: %s", model_names)
            else:
                logger.warning("wren search не нашёл релевантных таблиц, fallback на Postgres напрямую")
                context = await _introspect_postgres()
        except wren_client.WrenExecutionError as e:
            logger.warning("wren memory fetch не сработал (%s), fallback на Postgres напрямую", e)
            context = await _introspect_postgres()
    else:
        logger.info("Wren не настроен или недоступен, fallback на Postgres напрямую")
        context = await _introspect_postgres()

    return context