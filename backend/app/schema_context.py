from . import wren_client
from .db import run_internal_query
from .logging_config import logger


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


async def get_schema_context(question: str) -> str:
    if wren_client.is_configured():
        try:
            context = await wren_client.fetch_schema_context(question)
            logger.info("Схема получена через wren memory fetch")
            return context
        except wren_client.WrenExecutionError as e:
            logger.warning("wren memory fetch не сработал (%s), fallback на Postgres напрямую", e)

    logger.info("Wren не настроен или недоступен, fallback на Postgres напрямую")
    return await _introspect_postgres()