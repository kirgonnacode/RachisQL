# --- RachisQL Версия: 0.7.6 ---



import time
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from . import wren_client, query_cache, wren_memory
from .auth import require_auth
from .chart_builder import build_chart_option
from .chart_client import ChartRenderError, render_png
from .config import MAX_ROWS, SQL_MAX_ATTEMPTS
from .db import check_db_connection, close_pool, init_pool, run_readonly_query
from .llm_client import generate_sql, warm_up_ollama
from .logging_config import logger
from .models import AskRequest, AskResponse, ErrorResponse, FeedbackRequest
from .rate_limit import RateLimitExceeded, check_rate_limit
from .schema_context import get_schema_context
from .sql_guard import UnsafeSQLError, validate_and_sanitize
from .value_resolver import resolve_dictionary_values
import asyncio
from .request_context import request_id
from .tracing import trace_request

_background_tasks: set[asyncio.Task] = set()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()

    for coro in (warm_up_ollama(), wren_memory.warm_up()):
        task = asyncio.create_task(coro)
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    yield

    for task in list(_background_tasks):
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    wren_memory.shutdown()
    await close_pool()


app = FastAPI(title="RachisQL", version="0.7.6", lifespan=lifespan)

app.middleware("http")(trace_request)

ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Нет или невалиден Bearer-токен"},
    422: {"model": ErrorResponse, "description": "SQL отклонён guard'ом или Wren"},
    429: {"model": ErrorResponse, "description": "Превышен лимит запросов"},
    500: {"model": ErrorResponse, "description": "Ошибка выполнения запроса"},
    503: {"model": ErrorResponse, "description": "Ollama или Postgres временно недоступны"},
}


def _error(detail: str, generated_sql: str | None = None) -> dict:
    # ID вопроса автоматически попадает в тело любой ошибки.
    # Вне трассируемых эндпоинтов (например 401 на /feedback) будет "-" -> None
    current_id = request_id.get()
    return ErrorResponse(
        detail=detail,
        generated_sql=generated_sql,
        query_id=current_id if current_id != "-" else None,
    ).model_dump()


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


@app.get("/live")
async def live():
    return {"status": "alive"}


async def _generate_and_execute_sql(question: str) -> tuple[str, list[dict]]:
    try:
        schema_context = await get_schema_context(question)
    except Exception as e:
        logger.error("Не удалось получить схему: %s", e)
        raise HTTPException(503, detail=_error(f"База данных временно недоступна: {e}"))

    previous_sql: str | None = None
    previous_error: str | None = None
    last_raw_sql = ""
    last_error_detail = ""
    last_safe_sql = ""

    for attempt in range(1, SQL_MAX_ATTEMPTS + 1):
        try:
            raw_sql = await generate_sql(question, schema_context, previous_sql, previous_error)
        except Exception as e:
            logger.error("Не удалось сгенерировать SQL (попытка %d): %s", attempt, e)
            raise HTTPException(503, detail=_error(f"LLM временно недоступна: {e}"))

        last_raw_sql = raw_sql

        try:
            safe_sql = validate_and_sanitize(raw_sql)
        except UnsafeSQLError as e:
            logger.warning("Попытка %d: guard отклонил SQL: %s | причина: %s", attempt, raw_sql, e)
            previous_sql, previous_error = raw_sql, str(e)
            last_error_detail = f"Сгенерированный SQL отклонён guard'ом: {e}"
            last_safe_sql = ""
            continue

        safe_sql = resolve_dictionary_values(safe_sql)

        if wren_client.is_configured():
            try:
                await wren_client.dry_run(safe_sql)
            except wren_client.WrenValidationError as e:
                logger.warning("Попытка %d: Wren dry-run отклонил SQL: %s | причина: %s", attempt, safe_sql, e)
                previous_sql, previous_error = safe_sql, str(e)
                last_error_detail = f"Запрос не соответствует модели данных (Wren): {e}"
                last_safe_sql = ""
                continue
            except wren_client.WrenExecutionError as e:
                logger.warning("Wren dry-run недоступен (%s), продолжаю без семантической валидации", e)

        try:
            rows = await _execute_sql(safe_sql)
        except Exception as e:
            logger.warning("Попытка %d: ошибка выполнения SQL '%s': %s", attempt, safe_sql, e)
            previous_sql, previous_error = safe_sql, str(e)
            last_error_detail = f"Ошибка выполнения запроса: {e}"
            last_safe_sql = ""
            continue

        if rows:
            if attempt > 1:
                logger.info("SQL успешно исправлен с попытки %d/%d", attempt, SQL_MAX_ATTEMPTS)
            return safe_sql, rows

        logger.warning("Попытка %d: запрос выполнился, но вернул 0 строк", attempt)
        last_safe_sql = safe_sql
        last_error_detail = "Запрос выполнился, но не вернул данных"
        previous_sql = safe_sql
        previous_error = (
            "Запрос выполнился успешно, но вернул 0 строк. Проверь точное "
            "написание значений в WHERE (регистр, формат даты, опечатки) и "
            "правильную ли таблицу/колонку используешь. Если данные "
            "действительно могут отсутствовать за этот период - оставь запрос как есть."
        )

    if last_safe_sql:
        logger.info("После %d попыток запрос стабильно возвращает 0 строк - отдаю как валидный пустой результат", SQL_MAX_ATTEMPTS)
        return last_safe_sql, []

    raise HTTPException(422, detail=_error(last_error_detail, generated_sql=last_raw_sql))


async def _execute_sql(sql: str) -> list[dict]:
    if wren_client.is_configured():
        try:
            return await wren_client.execute(sql, limit=MAX_ROWS)
        except wren_client.WrenExecutionError as e:
            logger.warning("Выполнение через wren query не удалось (%s), fallback на прямой Postgres", e)

    return await run_readonly_query(sql)


@app.post("/ask", response_model=AskResponse, responses=ERROR_RESPONSES)
async def ask(request: AskRequest, consumer: str = Depends(authenticated_consumer)):
    logger.info("Новый вопрос от '%s': %s", consumer, request.question)

    safe_sql, rows = await _generate_and_execute_sql(request.question)
    logger.info("Вопрос обработан, строк: %d", len(rows))

    return AskResponse(
        question=request.question,
        generated_sql=safe_sql,
        rows=rows,
        row_count=len(rows),
        query_id=request_id.get(),
    )


@app.post(
    "/ask/image",
    responses={
        200: {"content": {"image/png": {}}, "description": "PNG с графиком"},
        **ERROR_RESPONSES,
        502: {"model": ErrorResponse, "description": "chart-renderer недоступен"},
    },
)
async def ask_image(request: AskRequest, consumer: str = Depends(authenticated_consumer)):
    logger.info("Новый запрос графика от '%s': %s", consumer, request.question)

    safe_sql, rows = await _generate_and_execute_sql(request.question)

    option = build_chart_option(rows, request.question)
    if option is None:
        raise HTTPException(
            422,
            detail=_error(
                "Результат запроса не подходит для визуализации (нет числовых колонок или данных).",
                generated_sql=safe_sql,
            ),
        )

    try:
        png_bytes = await render_png(option)
    except ChartRenderError as e:
        raise HTTPException(502, detail=_error(f"Сервис рендера графиков недоступен: {e}", generated_sql=safe_sql))

    query_cache.store(request_id.get(), request.question, safe_sql)
    return Response(content=png_bytes, media_type="image/png")


@app.post("/feedback", responses=ERROR_RESPONSES)
async def feedback(request: FeedbackRequest, consumer: str = Depends(authenticated_consumer)):
    request_id.set(request.query_id)

    cached = query_cache.get(request.query_id)
    if cached is None:
        raise HTTPException(
            404,
            detail=_error("Запрос не найден. Возможно запрос устарел или был очищен из памяти при рестарте RachisQL"),
        )

    question, sql = cached["question"], cached["sql"]

    if request.rating == "up":
        try:
            result = await wren_client.store_example(question, sql)
            action = "обновлён" if result.get("updated") else "сохранён"
            logger.info("Пример %s (👍 от '%s', %s): %s", action, consumer, result.get("file"), question)
        except wren_client.WrenExecutionError as e:
            logger.warning("Не удалось сохранить пример в Wren memory: %s", e)
    else:
        logger.warning("Плохая оценка (👎 от '%s') | вопрос: %s | SQL: %s", consumer, question, sql)

    return {"status": "ok"}