# --- RachisQL Версия: 0.3.2 ---



import time
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from . import wren_client
from .auth import require_auth
from .chart_builder import build_chart_option
from .chart_client import ChartRenderError, render_png
from .config import MAX_ROWS
from .db import check_db_connection, close_pool, init_pool, run_readonly_query
from .llm_client import generate_sql
from .logging_config import logger
from .models import AskRequest, AskResponse, ErrorResponse
from .rate_limit import RateLimitExceeded, check_rate_limit
from .schema_context import get_schema_context
from .sql_guard import UnsafeSQLError, validate_and_sanitize


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
    await close_pool()


app = FastAPI(title="RachisQL", version="0.2.0", lifespan=lifespan)

ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Нет или невалиден Bearer-токен"},
    422: {"model": ErrorResponse, "description": "SQL отклонён guard'ом или Wren"},
    429: {"model": ErrorResponse, "description": "Превышен лимит запросов"},
    500: {"model": ErrorResponse, "description": "Ошибка выполнения запроса"},
    503: {"model": ErrorResponse, "description": "Ollama или Postgres временно недоступны"},
}


def _error(detail: str, generated_sql: str | None = None) -> dict:
    return ErrorResponse(detail=detail, generated_sql=generated_sql).model_dump()


async def authenticated_consumer(consumer: str = Depends(require_auth)) -> str:
    try:
        check_rate_limit(consumer)
    except RateLimitExceeded as e:
        logger.warning("Rate limit превышен для '%s'", consumer)
        raise HTTPException(429, detail=_error(str(e)))
    return consumer


@app.get("/health")
async def health():
    db_ok = await check_db_connection()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "postgres": db_ok,
        "wren_configured": wren_client.is_configured(),
    }
    return JSONResponse(status_code=200 if db_ok else 503, content=payload)


async def _generate_and_validate_sql(question: str) -> str:
    try:
        schema_context = await get_schema_context(question)
        raw_sql = await generate_sql(question, schema_context)
    except Exception as e:
        logger.error("Не удалось получить схему или сгенерировать SQL: %s", e)
        raise HTTPException(503, detail=_error(f"LLM или база данных временно недоступны: {e}"))

    try:
        safe_sql = validate_and_sanitize(raw_sql)
    except UnsafeSQLError as e:
        logger.warning("Небезопасный SQL отклонён: %s | причина: %s", raw_sql, e)
        raise HTTPException(
            422,
            detail=_error(f"Сгенерированный SQL отклонён guard'ом: {e}", generated_sql=raw_sql),
        )

    if wren_client.is_configured():
        try:
            await wren_client.dry_run(safe_sql)
        except wren_client.WrenValidationError as e:
            logger.warning("Wren dry-run отклонил SQL: %s | причина: %s", safe_sql, e)
            raise HTTPException(
                422,
                detail=_error(f"Запрос не соответствует модели данных (Wren): {e}", generated_sql=safe_sql),
            )
        except wren_client.WrenExecutionError as e:
            logger.warning("Wren dry-run недоступен (%s), продолжаю без семантической валидации", e)

    return safe_sql


async def _execute_sql(sql: str) -> list[dict]:
    if wren_client.is_configured():
        try:
            return await wren_client.execute(sql, limit=MAX_ROWS)
        except wren_client.WrenExecutionError as e:
            logger.warning("Выполнение через wren query не удалось (%s), fallback на прямой Postgres", e)

    return await run_readonly_query(sql)


@app.post("/ask", response_model=AskResponse, responses=ERROR_RESPONSES)
async def ask(request: AskRequest, consumer: str = Depends(authenticated_consumer)):
    started_at = time.monotonic()
    logger.info("Новый вопрос от '%s': %s", consumer, request.question)

    safe_sql = await _generate_and_validate_sql(request.question)

    try:
        rows = await _execute_sql(safe_sql)
    except Exception as e:
        logger.error("Ошибка выполнения SQL '%s': %s", safe_sql, e)
        raise HTTPException(500, detail=_error(f"Ошибка выполнения запроса: {e}", generated_sql=safe_sql))

    elapsed = time.monotonic() - started_at
    logger.info("Вопрос обработан за %.2fс, строк: %d", elapsed, len(rows))

    return AskResponse(
        question=request.question,
        generated_sql=safe_sql,
        rows=rows,
        row_count=len(rows),
    )


@app.post(
    "/ask/image",
    responses={
        200: {"content": {"image/png": {}}, "description": "PNG с графиком"},
        **ERROR_RESPONSES,
        502: {"model": ErrorResponse, "description": "chart_renderer недоступен"},
    },
)
async def ask_image(request: AskRequest, consumer: str = Depends(authenticated_consumer)):
    logger.info("Новый запрос графика от '%s': %s", consumer, request.question)
    safe_sql = await _generate_and_validate_sql(request.question)

    try:
        rows = await _execute_sql(safe_sql)
    except Exception as e:
        raise HTTPException(500, detail=_error(f"Ошибка выполнения запроса: {e}", generated_sql=safe_sql))

    option = build_chart_option(rows, request.question)
    if option is None:
        raise HTTPException(
            422,
            detail=_error(
                "Результат запроса не подходит для визуализации (нет числовых колонок или данных). "
                f"Сырые данные: {rows[:5]}",
                generated_sql=safe_sql,
            ),
        )

    try:
        png_bytes = await render_png(option)
    except ChartRenderError as e:
        raise HTTPException(502, detail=_error(f"Сервис рендера графиков недоступен: {e}", generated_sql=safe_sql))

    return Response(content=png_bytes, media_type="image/png")
