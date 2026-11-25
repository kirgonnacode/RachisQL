
import asyncio
import collections
import json
import os
from .config import WREN_CONNECTION_INFO, WREN_PROJECT_DIR, WREN_TIMEOUT_SECONDS, WREN_SEARCH_LIMIT, WREN_SEARCH_MAX_DISTANCE, RECALL_EXAMPLES_LIMIT, WREN_SEARCH_MAX_MODELS, WREN_LEXICAL_WEIGHT
from .logging_config import logger
import time
from . import wren_memory
from . import lexical_index

_RRF_K = 10   # сглаживание рангов в гибридном поиске: разница между 1-м и 2-м местом важнее, чем между 30-м и 31-м
_MODULE_MIN_GAP = 0.15

def _connection_flags() -> list[str]:
    if not WREN_CONNECTION_INFO:
        return []
    return ["--connection-info", WREN_CONNECTION_INFO]


class WrenNotConfiguredError(Exception):
    """MDL ещё не собран (`wren context build` не запускался)"""


class WrenValidationError(Exception):
    """Wren dry-run отклонил SQL - запрос не соответствует модели данных"""


class WrenExecutionError(Exception):
    """Wren успешно провалидировал, но выполнение завершилось ошибкой (возможно таймаут БД)"""


def _is_configured() -> bool:
    return os.path.exists(os.path.join(WREN_PROJECT_DIR, "target", "mdl.json"))


async def _run_wren(*args: str) -> tuple[int, str, str]:
    started_at = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "wren",
            *args,
            cwd=WREN_PROJECT_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise WrenExecutionError(f"Не удалось запустить wren CLI: {e}")

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=WREN_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise WrenExecutionError(f"wren {args[0]} превысил таймаут {WREN_TIMEOUT_SECONDS}с")

    command = " ".join(args[:2]) if args[0] == "memory" else args[0]
    logger.info("wren %s: %.2fс (код %s)", command, time.monotonic() - started_at, proc.returncode)    

    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def _select_tables_by_module(
    ranked: list[str],
    scores: dict[str, float],
    modules: dict[str, str],
    limit: int,
) -> list[str]:
    baseline = ranked[:limit]
    if not ranked:
        return baseline

    if any(name not in modules for name in ranked):
        logger.info("Выбор модуля пропущен: есть таблицы без разметки")
        return baseline

    leader = modules[ranked[0]]
    runner_up = next((name for name in ranked if modules[name] != leader), None)
    if runner_up is None:
        logger.info("Поиск таблиц: единственный модуль %s", leader)
        return baseline

    top_score = scores[ranked[0]]
    if top_score <= 0:
        return baseline

    gap = (top_score - scores[runner_up]) / top_score
    if gap < _MODULE_MIN_GAP:
        logger.info(
            "Выбор модуля неоднозначен: %s / %s, отрыв %.2f%%",
            leader, modules[runner_up], gap * 100,
        )
        return baseline

    selected = [name for name in ranked if modules[name] == leader][:limit]
    logger.info("Выбран модуль %s, отрыв %.2f%%, таблицы: %s", leader, gap * 100, selected)
    return selected


async def fetch_relevant_models(question: str) -> list[str]:
    if not _is_configured():
        raise WrenNotConfiguredError("target/mdl.json не найден, wren context build не выполнялся")

    try:
        results = await wren_memory.search_schema(question)
    except wren_memory.WrenMemoryError as e:
        raise WrenExecutionError(str(e))

    if not isinstance(results, list):
        raise WrenExecutionError(
            f"wren memory fetch вернул неожиданный формат results: {type(results).__name__}"
        )

    emb_ranked: list[str] = []
    emb_distance: dict[str, float | None] = {}
    filtered_out: list[str] = []
    for item in results:
        name = item.get("model_name")
        if not name or name in emb_distance:
            continue
        distance = item.get("_distance")
        if WREN_SEARCH_MAX_DISTANCE is not None and distance is not None and distance > WREN_SEARCH_MAX_DISTANCE:
            filtered_out.append(f"{name} ({distance:.3f})")
            emb_distance[name] = distance
            continue
        emb_distance[name] = distance
        emb_ranked.append(name)

    try:
        lexical, modules = lexical_index.rank_with_modules(question)
        lex_ranked = [name for name, _ in lexical]
    except Exception as e:
        logger.warning("Лексический поиск недоступен (%s), используется только поиск по смыслу", e)
        lex_ranked = []
        modules = {}

    emb_pos = {name: i + 1 for i, name in enumerate(emb_ranked)}
    lex_pos = {name: i + 1 for i, name in enumerate(lex_ranked)}
    scores: dict[str, float] = collections.defaultdict(float)
    for name, pos in emb_pos.items():
        scores[name] += 1.0 / (_RRF_K + pos)
    for name, pos in lex_pos.items():
        scores[name] += WREN_LEXICAL_WEIGHT / (_RRF_K + pos)
    ranked = sorted(scores, key=lambda n: (-scores[n], emb_pos.get(n, 10**6)))
    model_names = _select_tables_by_module(ranked, scores, modules, WREN_SEARCH_MAX_MODELS)

    def _label(name: str) -> str:
        distance = emb_distance.get(name)
        by_meaning = f"#{emb_pos[name]} {distance:.3f}" if name in emb_pos and distance is not None else "—"
        return f"{name} (смысл {by_meaning}, слова #{lex_pos.get(name, '—')})"

    logger.info(
        "Поиск таблиц (гибрид): %s%s",
        [_label(n) for n in model_names],
        f" | отфильтровано по дистанции: {filtered_out}" if filtered_out else "",
    )
    return model_names


async def recall_examples(question: str) -> list[dict]:
    if not _is_configured():
        raise WrenNotConfiguredError("target/mdl.json не найден")

    try:
        data = await wren_memory.recall(question)
    except wren_memory.WrenMemoryError as e:
        raise WrenExecutionError(str(e))

    if not isinstance(data, list):
        raise WrenExecutionError(f"wren memory recall вернул не список: {type(data).__name__}")

    return data


async def dry_run(sql: str) -> None:
    if not _is_configured():
        raise WrenNotConfiguredError("target/mdl.json не найден")

    code, stdout, stderr = await _run_wren("dry-run", "--sql", sql, *_connection_flags())
    combined = (stdout + stderr).strip()
    if code != 0 or combined.lower().startswith("error"):
        raise WrenValidationError(combined or "wren dry-run отклонил запрос")
    logger.info("wren dry-run: OK")


async def execute(sql: str, limit: int) -> list[dict]:
    if not _is_configured():
        raise WrenNotConfiguredError("target/mdl.json не найден")

    code, stdout, stderr = await _run_wren(
        "query", "--sql", sql, "--limit", str(limit), "--output", "json", *_connection_flags()
    )
    if code != 0:
        raise WrenExecutionError(stderr or "wren query failed")

    rows = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise WrenExecutionError(f"Не удалось распарсить строку JSON от wren query: {e}")
        if not isinstance(row, dict):
            raise WrenExecutionError(
                f"Строка от wren query - не JSON-объект, а {type(row).__name__} - "
                f"похоже, поменялся формат вывода CLI, нужно проверить версию Wren"
            )
        rows.append(row)
    return rows


async def store_example(question: str, sql: str) -> dict:
    if not _is_configured():
        raise WrenNotConfiguredError("target/mdl.json не найден")

    try:
        return await wren_memory.store_example(question, sql)
    except wren_memory.WrenMemoryError as e:
        raise WrenExecutionError(str(e))

    
async def forget_example(question: str, sql: str) -> dict:
    if not _is_configured():
        raise WrenNotConfiguredError("target/mdl.json не найден")

    try:
        return await wren_memory.forget_example(question, sql)
    except wren_memory.WrenMemoryError as e:
        raise WrenExecutionError(str(e))

def is_configured() -> bool:
    return _is_configured()