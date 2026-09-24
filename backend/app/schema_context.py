from . import wren_client
from .db import run_internal_query
from .logging_config import logger
from pathlib import Path
import yaml
from .config import WREN_PROJECT_DIR, SAMPLE_ROWS_ENABLED, SAMPLE_ROWS_COUNT
import asyncio

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
                sample_blocks = await asyncio.gather(*(_fetch_sample_rows(name) for name in model_names))
                blocks = []
                for desc, samples in zip(descriptions, sample_blocks):
                    if desc:
                        blocks.append(desc)
                    if samples:
                        blocks.append(samples)
                context = "\n\n".join(blocks)
                sample_count = sum(1 for s in sample_blocks if s)
                logger.info(
                    "Схема собрана через wren search: %s (sample rows получены: %d/%d)",
                    model_names, sample_count, len(model_names),
                )
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


def _quote_ident(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


async def _fetch_sample_rows(model_name: str) -> str:
    """
    ВАЖНО: кладёт реальные значения из БД в промпт LLM. Для таблиц с
    чувствительными/персональными данными отключается явно через
    properties.no_sample_rows: true в metadata.yml конкретной модели.
    """
    if not SAMPLE_ROWS_ENABLED:
        return ""

    metadata_file = Path(WREN_PROJECT_DIR) / "models" / model_name / "metadata.yml"
    if not metadata_file.is_file():
        return ""
    try:
        data = yaml.safe_load(metadata_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return ""

    if (data.get("properties") or {}).get("no_sample_rows"):
        return ""

    table_ref = data.get("table_reference") or {}
    schema = table_ref.get("schema", "public")
    table = table_ref.get("table")
    if not table:
        return ""

    query = f"SELECT * FROM {_quote_ident(schema)}.{_quote_ident(table)} LIMIT {SAMPLE_ROWS_COUNT}"

    try:
        rows = await run_internal_query(query)
    except Exception as e:
        logger.warning("Не удалось получить sample rows для '%s': %s", model_name, e)
        return ""

    if not rows:
        return ""

    columns = list(rows[0].keys())
    lines = [f"Пример данных из '{model_name}' ({len(rows)} строк):", " | ".join(columns)]
    for row in rows:
        lines.append(" | ".join(str(row.get(c)) for c in columns))

    return "\n".join(lines)