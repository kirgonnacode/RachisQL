import asyncio
import asyncpg
from asyncpg.exceptions import QueryCanceledError
from .config import DSN, POSTGRES_POOL_MAX_SIZE, POSTGRES_POOL_MIN_SIZE, QUERY_TIMEOUT_SECONDS
from .logging_config import logger


_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    _pool = await asyncpg.create_pool(
        dsn=DSN,
        min_size=POSTGRES_POOL_MIN_SIZE,
        max_size=POSTGRES_POOL_MAX_SIZE,
        server_settings={"statement_timeout": str(QUERY_TIMEOUT_SECONDS * 1000)},
    )
    logger.info(
        "Postgres pool создан (min=%d, max=%d)", POSTGRES_POOL_MIN_SIZE, POSTGRES_POOL_MAX_SIZE
    )


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Postgres pool закрыт")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError(
            "Postgres pool не инициализирован - init_pool() должен быть вызван при старте приложения"
        )
    return _pool


async def run_readonly_query(sql: str) -> list[dict]:
    pool = _get_pool()
    try:
        async with asyncio.timeout(QUERY_TIMEOUT_SECONDS + 2):
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql)
    except QueryCanceledError:
        logger.warning("Postgres отменил запрос по statement_timeout: %s", sql)
        raise
    except TimeoutError:
        logger.warning("Клиентский таймаут ожидания запроса: %s", sql)
        raise

    return [dict(row) for row in rows]


async def run_internal_query(sql: str) -> list[dict]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [dict(row) for row in rows]


async def check_db_connection() -> bool:
    try:
        pool = _get_pool()
    except RuntimeError:
        return False

    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception as e:
        logger.error("Postgres недоступен: %s", e)
        return False
