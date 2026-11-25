import asyncio
from collections import defaultdict
import fcntl
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
import yaml
from .config import CATEGORY_CACHE_DAYS, CATEGORY_CACHE_TOP_N, WREN_PROJECT_DIR
from .db import run_internal_query
from .logging_config import logger


ROOT = Path(WREN_PROJECT_DIR)
SNAPSHOT = ROOT / "knowledge" / "category_cache" / "snapshot.json"
_CATALOG = """
SELECT n.nspname AS schema_name, c.relname AS table_name, a.attname AS column_name
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
WHERE n.nspname = ANY($1::text[]) AND c.relname = ANY($2::text[])
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND a.attnum > 0 AND NOT a.attisdropped
  AND COALESCE(NULLIF(t.typbasetype, 0), t.oid) IN
      ('text'::regtype, 'varchar'::regtype, 'bpchar'::regtype)
"""


def snapshot_version():
    try:
        info = SNAPSHOT.stat()
    except FileNotFoundError:
        return None
    return info.st_ino, info.st_mtime_ns


def read_snapshot():
    try:
        snapshot = json.loads(SNAPSHOT.read_bytes())
        if (not isinstance(snapshot, dict) or snapshot.get("format") != 1
                or not isinstance(snapshot.get("models"), dict)
                or not isinstance(snapshot.get("inputs"), dict)):
            raise ValueError("неизвестный формат")
        return snapshot
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Не удалось прочитать снимок категорий: %s", exc)
        return {}


def _inputs():
    return {p.parent.name: p.stat().st_mtime_ns
            for p in sorted((ROOT / "models").glob("*/metadata.yml"))}


def _sources(inputs):
    sources = defaultdict(list)
    for model in inputs:
        path = ROOT / "models" / model / "metadata.yml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ref = data.get("table_reference") or {}
        if (data.get("properties") or {}).get("no_sample_rows") or not ref.get("table"):
            continue
        for col in data.get("columns") or []:
            if col.get("name") and not col.get("is_calculated") and not col.get("expression"):
                key = (ref.get("schema") or "public", ref["table"], col["name"])
                sources[key].append(model)
    return sources


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


async def _collect(sources):
    schemas = sorted({schema for schema, _, _ in sources})
    tables = sorted({table for _, table, _ in sources})
    rows = await run_internal_query(_CATALOG, schemas, tables)
    available = {(r["schema_name"], r["table_name"], r["column_name"]) for r in rows}
    keys = sorted(sources.keys() & available)
    models = defaultdict(dict)
    for schema, table, column in keys:
        value = f"btrim({_quote(column)}::text)"
        sql = (f"SELECT {value} AS value FROM {_quote(schema)}.{_quote(table)} "
               f"WHERE {value} <> '' GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT $1")
        try:
            rows = await run_internal_query(sql, CATEGORY_CACHE_TOP_N)
        except Exception as exc:
            raise RuntimeError(f"Ошибка чтения {schema}.{table}.{column}") from exc
        values = [r["value"] for r in rows]
        for model in sources[schema, table, column]:
            models[model][column] = values
    return models, len(keys)


def _publish(snapshot):
    with NamedTemporaryFile(dir=SNAPSHOT.parent, mode="w", encoding="utf-8", delete=False) as tmp:
        path = Path(tmp.name)
        try:
            json.dump(snapshot, tmp, ensure_ascii=False, separators=(",", ":"))
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(path, SNAPSHOT)
        finally:
            path.unlink(missing_ok=True)


async def _refresh():
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    with (SNAPSHOT.parent / "refresh.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 60
        inputs = _inputs()
        old = await asyncio.to_thread(read_snapshot)
        ttl = CATEGORY_CACHE_DAYS * 86400
        remaining = old.get("built_at", 0) + ttl - time.time()
        if (old.get("limit") == CATEGORY_CACHE_TOP_N and old.get("inputs") == inputs
                and 0 < remaining <= ttl):
            return remaining
        started = time.monotonic()
        sources = await asyncio.to_thread(_sources, inputs)
        models, count = await _collect(sources)
        if _inputs() != inputs:
            return 60
        snapshot = dict(format=1, built_at=time.time(), limit=CATEGORY_CACHE_TOP_N,
                        inputs=inputs, models=models)
        _publish(snapshot)
        logger.info("Снимок категорий обновлён: колонок %d, моделей %d, %.2f с",
                    count, len(models), time.monotonic() - started)
        return ttl


async def refresh_loop():
    while True:
        try:
            delay = await _refresh()
        except Exception:
            logger.exception("Сбор категорий прерван; повтор через 5 минут")
            delay = 300
        await asyncio.sleep(delay)
