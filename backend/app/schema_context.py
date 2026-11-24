from . import wren_client
from .db import run_internal_query
from .logging_config import logger
from pathlib import Path
import yaml
from .config import WREN_PROJECT_DIR, SAMPLE_ROWS_ENABLED, SAMPLE_ROWS_COUNT, RECALL_MAX_DISTANCE
import asyncio
import time
from sqlglot import exp, parse
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import traverse_scope

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


def _example_models(sql: str) -> set[str]:
    try:
        statements = parse(sql, read="postgres")
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            return set()

        query = normalize_identifiers(statements[0], dialect="postgres")
        names: set[str] = set()

        for scope in traverse_scope(query):
            for _, source in scope.selected_sources.values():
                if isinstance(source, exp.Table):
                    if (
                        not isinstance(source.this, exp.Identifier)
                        or source.db not in ("", "public")
                        or source.catalog not in ("", "wren")
                    ):
                        return set()
                    names.add(source.name)

        return names
    except SqlglotError:
        return set()


async def _build_models_context(question: str) -> tuple[str, set[str]]:
    if not wren_client.is_configured():
        logger.info("Wren не настроен или недоступен, fallback на Postgres напрямую")
        return await _introspect_postgres(), set()

    try:
        model_names = await wren_client.fetch_relevant_models(question)
    except (wren_client.WrenExecutionError, wren_client.WrenNotConfiguredError) as e:
        logger.warning("wren memory fetch не сработал (%s), fallback на Postgres напрямую", e)
        return await _introspect_postgres(), set()

    if not model_names:
        logger.warning("wren search не нашёл релевантных таблиц, fallback на Postgres напрямую")
        return await _introspect_postgres(), set()

    descriptions = [_describe_model(name) for name in model_names]
    sample_blocks = await asyncio.gather(*(_fetch_sample_rows(name) for name in model_names))

    blocks = []
    included_models: set[str] = set()

    for name, desc, samples in zip(model_names, descriptions, sample_blocks):
        if desc:
            blocks.append(desc)
            included_models.add(name)
        if samples:
            blocks.append(samples)

    sample_count = sum(1 for samples in sample_blocks if samples)
    logger.info(
        "Схема собрана через wren search: %s (sample rows получены: %d/%d)",
        [name for name in model_names if name in included_models], sample_count, len(model_names),
    )
    return "\n\n".join(blocks), included_models


async def _recall_candidates(question: str) -> list[dict]:
    try:
        return await wren_client.recall_examples(question)
    except (wren_client.WrenExecutionError, wren_client.WrenNotConfiguredError) as e:
        logger.warning("wren memory recall не сработал (%s), пропускаю", e)
        return []


def _format_recall_examples(examples: list[dict], included_models: set[str]) -> str:
    if not examples or not included_models:
        return ""

    kept_labels: list[str] = []
    kept_texts: list[str] = []
    filtered_out: list[str] = []

    for ex in examples:
        nl = ex.get("nl_query", "")
        sql = ex.get("sql_query", "")
        if not nl or not sql:
            continue

        tags = (ex.get("tags") or "").split()
        if "source:user" not in tags:
            filtered_out.append(f"{nl!r} (теги: {' '.join(tags) or 'без тегов'})")
            continue

        distance = ex.get("_distance")
        if RECALL_MAX_DISTANCE is not None and distance is not None and distance > RECALL_MAX_DISTANCE:
            filtered_out.append(f"{nl!r} (distance={distance:.3f})")
            continue

        models = _example_models(sql)
        if not models:
            filtered_out.append(f"{nl!r} (таблицы не определены)")
            continue

        outside = models - included_models
        if outside:
            filtered_out.append(f"{nl!r} (вне контекста: {', '.join(sorted(outside))})")
            continue

        label = f"{distance:.3f}" if distance is not None else f"score={ex.get('score')}"
        kept_labels.append(f"{nl!r} ({label})")
        kept_texts.append(f'Вопрос: "{nl}"\nSQL: {sql}')

    logger.info(
        "wren recall нашёл примеров: %d %s | отфильтровано: %s",
        len(kept_texts), kept_labels, filtered_out,
    )
    
    if not kept_texts:
        return ""

    lines = ["Подтверждённые примеры похожих вопросов (используй как образец):"]
    lines.extend(kept_texts)
    return "\n\n".join(lines)


async def get_schema_context(question: str) -> str:
    started_at = time.monotonic()

    if wren_client.is_configured():
        (context, included_models), candidates = await asyncio.gather(
            _build_models_context(question),
            _recall_candidates(question),
        )
        examples = _format_recall_examples(candidates, included_models)
    else:
        context, _ = await _build_models_context(question)
        examples = ""

    if examples:
        context = f"{context}\n\n### {examples}"

    logger.info("Контекст для LLM собран за %.2fс", time.monotonic() - started_at)
    return context