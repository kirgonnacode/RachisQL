
import asyncio
import json
import os
from .config import WREN_CONNECTION_INFO, WREN_PROJECT_DIR, WREN_TIMEOUT_SECONDS
from .logging_config import logger


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

    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


async def fetch_schema_context(question: str, limit: int = 8) -> str:
    if not _is_configured():
        raise WrenNotConfiguredError("target/mdl.json не найден, wren context build не выполнялся")

    code, stdout, stderr = await _run_wren(
        "memory", "fetch", "-q", question, "-l", str(limit), "--output", "json"
    )
    if code != 0:
        logger.warning("wren memory fetch завершился с ошибкой: %s", stderr)
        raise WrenExecutionError(stderr or "wren memory fetch failed")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    return json.dumps(data, ensure_ascii=False, indent=2)


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

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise WrenExecutionError(f"Не удалось распарсить JSON от wren query: {e}")

    if not isinstance(data, list):
        raise WrenExecutionError(
            f"wren query вернул не список строк, а {type(data).__name__} - "
            f"похоже, поменялся формат вывода CLI, нужно проверить версию Wren"
        )
    return data


def is_configured() -> bool:
    return _is_configured()