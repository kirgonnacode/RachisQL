import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .config import RECALL_EXAMPLES_LIMIT, WREN_PROJECT_DIR, WREN_SEARCH_LIMIT, WREN_TIMEOUT_SECONDS
from .logging_config import logger
from wren.memory.markdown import write_query_markdown

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wren-memory")

_store = None
_manifest_cache: tuple[float, dict] | None = None


class WrenMemoryError(Exception):
    """Поиск по памяти Wren не удался (нет индекса, битый mdl.json, ошибка LanceDB и т.п.)"""


def _mdl_path() -> Path:
    return Path(WREN_PROJECT_DIR) / "target" / "mdl.json"


def _memory_path() -> Path:
    return Path(WREN_PROJECT_DIR) / ".wren" / "memory"


def _get_store():
    global _store
    if _store is None:
        # Импорт внутри функции. Тяжёлые LanceDB/Torch грузятся только при первом обращении, а не при импорте модуля
        from wren.memory.store import MemoryStore

        _store = MemoryStore(path=_memory_path())
        logger.info("Wren MemoryStore открыт: %s", _memory_path())
    return _store


def _load_manifest() -> dict:
    global _manifest_cache
    path = _mdl_path()
    mtime = path.stat().st_mtime
    if _manifest_cache is None or _manifest_cache[0] != mtime:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        _manifest_cache = (mtime, manifest)
        logger.info("Wren MDL (пере)загружен из %s", path)
    return _manifest_cache[1]


def _search_schema_sync(question: str) -> list[dict]:
    result = _get_store().get_context(
        _load_manifest(), question, limit=WREN_SEARCH_LIMIT, threshold=1
    )
    return result.get("results", [])


def _recall_sync(question: str) -> list[dict]:
    return _get_store().recall_queries(question, limit=RECALL_EXAMPLES_LIMIT)


async def _run(label: str, fn, *args):
    """Запускает синхронную функцию в потоке памяти Wren с таймаутом и замером."""
    loop = asyncio.get_running_loop()
    started_at = time.monotonic()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(_executor, fn, *args),
            timeout=WREN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise WrenMemoryError(f"wren memory {label} превысил таймаут {WREN_TIMEOUT_SECONDS}с")
    except Exception as e:
        raise WrenMemoryError(f"wren memory {label}: {type(e).__name__}: {e}") from e

    logger.info("wren memory %s (in-process): %.3fс", label, time.monotonic() - started_at)
    return result


async def search_schema(question: str) -> list[dict]:
    """Аналог `wren memory fetch ... --output json` -> поле results."""
    return await _run("fetch", _search_schema_sync, question)


async def recall(question: str) -> list[dict]:
    """Аналог `wren memory recall ... --output json`."""
    return await _run("recall", _recall_sync, question)


async def warm_up() -> None:
    if not _mdl_path().is_file():
        logger.info("Прогрев Wren memory пропущен: %s не найден", _mdl_path())
        return
    started_at = time.monotonic()
    try:
        await search_schema("прогрев")
        logger.info("Wren memory прогрета за %.1fс (модель эмбеддингов в памяти)", time.monotonic() - started_at)
    except WrenMemoryError as e:
        logger.warning("Прогрев Wren memory не удался (%s), первый запрос будет холодным", e)


def _store_example_sync(question: str, sql: str) -> dict:
    nl = question.strip()
    sql = sql.strip()

    md_path = write_query_markdown(Path(WREN_PROJECT_DIR), nl, sql)
    try:
        result = _get_store().load_queries([{"nl": nl, "sql": sql, "source": "user"}], upsert=True)
    except Exception as e:
        raise RuntimeError(
            f"markdown сохранён ({md_path.name}), но индекс LanceDB не обновлён: {e}. "
            f"Пример попадёт в recall после `wren memory index`"
        ) from e

    return {"file": md_path.name, **result}


async def store_example(question: str, sql: str) -> dict:
    return await _run("store", _store_example_sync, question, sql)


def shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)        