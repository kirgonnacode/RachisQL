# --- RachisQL Версия: 0.8.8 ---



import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from . import category_cache, wren_client, query_cache, wren_memory
from .auth import require_auth
from .chart_builder import add_period_caption, build_chart_option
from .dates import parse_period
from .period_response import describe_period
from .chart_client import ChartRenderError, render_png
from .config import CHART_TIMEZONE_OFFSET_HOURS, MAX_ROWS, SQL_MAX_ATTEMPTS
from .db import check_db_connection, close_pool, init_pool
from .llm_client import generate_sql, warm_up_ollama
from .logging_config import logger
from .models import AnswerPeriod, AskRequest, AskResponse, ErrorResponse, FeedbackRequest
from .rate_limit import RateLimitExceeded, check_rate_limit
from .schema_context import NoRelevantModelsError, get_schema_context_with_module
from .sql_guard import UnsafeSQLError, sql_retry_key, validate_and_sanitize
from .value_resolver import resolve_dictionary_values
import asyncio
from .request_context import request_id
from .tracing import trace_request
import re
from .semantic.shadow import load_shadow_catalogs, run_shadow

_background_tasks: set[asyncio.Task] = set()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()

    app.state.shadow_catalogs = await load_shadow_catalogs()

    for coro in (warm_up_ollama(), wren_memory.warm_up(), category_cache.refresh_loop()):
        task = asyncio.create_task(coro)
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    yield

    for task in list(_background_tasks):
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    wren_memory.shutdown()
    await close_pool()


app = FastAPI(title="RachisQL", version="0.8.8", lifespan=lifespan)

app.middleware("http")(trace_request)

ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Нет или невалиден Bearer-токен"},
    422: {"model": ErrorResponse, "description": "Не найдены таблицы или не удалось получить корректный SQL"},
    429: {"model": ErrorResponse, "description": "Превышен лимит запросов"},
    500: {"model": ErrorResponse, "description": "Ошибка выполнения запроса"},
    503: {"model": ErrorResponse, "description": "Схема данных, Wren или Ollama временно недоступны"},
}


_UNKNOWN_COLUMN = re.compile(
    r'\bcolumn\b[^\n]*?\b(?:does not exist|not found)\b'
    r'|\b(?:unknown column|no field named|no such column)\b'
    r'|\b(?:столбец|колонка)\b[^\n]*?\bне существует\b',
    re.IGNORECASE,
)


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


def _column_hint(columns_by_model: dict[str, list[str]]) -> str:
    lines = []
    for table, columns in columns_by_model.items():
        if columns:
            quoted = ['"' + name.replace('"', '""') + '"' for name in columns]
            lines.append(f"{table}: {', '.join(quoted)}")
    if not lines:
        return ""
    return "Точные имена колонок выбранных таблиц:\n" + "\n".join(lines)


def split_reported_period(rows: list[dict]) -> tuple[list[dict], str | None]:
    if not rows or not any("__report_period" in row for row in rows):
        return rows, None

    values = [row.get("__report_period") for row in rows]
    first = values[0]
    reported = None
    if isinstance(first, str) and first.strip() and all(value == first for value in values):
        reported = first.strip()

    clean_rows = [
        {key: value for key, value in row.items() if key != "__report_period"}
        for row in rows
    ]
    return clean_rows, reported


def _finish_attempt(
    sql: str,
    error_detail: str | None,
    period: AnswerPeriod | None,
) -> tuple[str, list[dict], AnswerPeriod | None]:
    if error_detail is not None:
        raise HTTPException(422, detail=_error(error_detail, generated_sql=sql))
    return sql, [], period


async def _generate_and_execute_sql(
    question: str,
    *,
    background_tasks: BackgroundTasks | None = None,
) -> tuple[str, list[dict], AnswerPeriod | None]:
    project_tz = timezone(timedelta(hours=CHART_TIMEZONE_OFFSET_HOURS))
    request_time = datetime.now(project_tz)

    try:
        schema_context, columns, selected_module = await get_schema_context_with_module(question)
    except NoRelevantModelsError as e:
        logger.info("Поиск таблиц: %s", e)
        raise HTTPException(422, detail=_error(str(e))) from e
    except Exception as e:
        logger.exception("Не удалось загрузить схему Wren")
        raise HTTPException(
            503,
            detail=_error("Не удалось загрузить схему данных. Попробуйте позже."),
        ) from e

    catalog = app.state.shadow_catalogs.get(selected_module)
    if background_tasks is not None and catalog is not None:
        background_tasks.add_task(
            run_shadow,
            catalog,
            question,
            request_time.date(),
            request_id.get(),
        )

    requested = parse_period(question, request_time.date())
    periods: dict[str, AnswerPeriod | None] = {}    
    history: list[tuple[str, str]] = []
    outcomes: dict[tuple[bool, tuple[str, ...]], tuple[str, str | None]] = {}
    hint = ""
    outcome: tuple[str, str | None] = ("", "Попытки генерации SQL исчерпаны")

    for attempt in range(1, SQL_MAX_ATTEMPTS + 1):
        try:
            raw_sql = await generate_sql(
                question, schema_context, history, hint,
                request_time=request_time,
            )
        except Exception as e:
            logger.error("Не удалось сгенерировать SQL (попытка %d): %s", attempt, e)
            raise HTTPException(503, detail=_error(f"LLM временно недоступна: {e}"))

        error: Exception | None = None
        try:
            sql = validate_and_sanitize(raw_sql)
        except UnsafeSQLError as e:
            sql = raw_sql
            error = e
        else:
            sql = resolve_dictionary_values(sql)

        passed_guard = error is None
        key = (passed_guard, sql_retry_key(sql))
        if key in outcomes:
            logger.info("Попытка %d: SQL повторился, завершаю цикл", attempt)
            prior_sql, prior_error = outcomes[key]
            return _finish_attempt(prior_sql, prior_error, periods.get(prior_sql))

        if error is not None:
            logger.warning("Попытка %d: guard отклонил SQL: %s | причина: %s", attempt, sql, error)
            detail = f"Сгенерированный SQL отклонён guard'ом: {error}"
        else:
            try:
                await wren_client.dry_run(sql)
            except wren_client.WrenValidationError as e:
                error = e
                detail = f"Запрос не соответствует модели данных (Wren): {e}"
                logger.warning("Попытка %d: Wren dry-run отклонил SQL: %s | причина: %s", attempt, sql, e)
            except (wren_client.WrenExecutionError, wren_client.WrenNotConfiguredError) as e:
                logger.warning("Wren dry-run недоступен: %s", e)
                raise HTTPException(
                    503,
                    detail=_error("Проверка SQL временно недоступна. Попробуйте позже."),
                ) from e

        if error is None:
            try:
                rows = await wren_client.execute(sql, limit=MAX_ROWS)
            except wren_client.WrenNotConfiguredError as e:
                logger.warning("Wren недоступен при выполнении SQL: %s", e)
                raise HTTPException(
                    503,
                    detail=_error("Сервис временно недоступен. Попробуйте позже."),
                ) from e
            except Exception as e:
                error = e
                detail = f"Ошибка выполнения запроса: {e}"
                logger.warning("Попытка %d: ошибка выполнения SQL '%s': %s", attempt, sql, e)
            else:
                period = await describe_period(sql, requested, columns)
                periods[sql] = period

                if rows or (period is not None and period.status == "outside"):
                    if attempt > 1:
                        logger.info(
                            "SQL успешно исправлен с попытки %d/%d",
                            attempt, SQL_MAX_ATTEMPTS,
                        )
                    return sql, rows, period

        if error is None:
            logger.warning("Попытка %d: запрос выполнился, но вернул 0 строк", attempt)
            reason = (
                "Запрос выполнился успешно, но вернул 0 строк. Проверь значения WHERE, "
                "даты, таблицы и колонки. Если данных действительно нет, оставь запрос как есть."
            )
            outcome = (sql, None)
        else:
            reason = str(error)
            outcome = (raw_sql, detail)
            if passed_guard and not hint:
                if getattr(error, "sqlstate", None) == "42703" or _UNKNOWN_COLUMN.search(reason):
                    hint = _column_hint(columns)

        history.append((sql, reason))
        outcomes[key] = outcome

    return _finish_attempt(*outcome, periods.get(outcome[0]))


@app.post("/ask", response_model=AskResponse, responses=ERROR_RESPONSES)
async def ask(request: AskRequest, background_tasks: BackgroundTasks, consumer: str = Depends(authenticated_consumer)):
    logger.info("Новый вопрос от '%s': %s", consumer, request.question)

    safe_sql, rows, period = await _generate_and_execute_sql(
        request.question,
        background_tasks=background_tasks,
    )
    rows, reported_period = split_reported_period(rows)    
    logger.info("Вопрос обработан, строк: %d", len(rows))

    return AskResponse(
        question=request.question,
        generated_sql=safe_sql,
        rows=rows,
        row_count=len(rows),
        query_id=request_id.get(),
        period=period,
        reported_period=reported_period,        
    )


@app.post(
    "/ask/image",
    response_class=Response,
    responses={
        200: {
            "description": "График или карточка с доступным периодом",
            "content": {
                "image/png": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
        **ERROR_RESPONSES,
        502: {"model": ErrorResponse, "description": "chart-renderer недоступен"},
    },
)

async def ask_image(request: AskRequest, background_tasks: BackgroundTasks, consumer: str = Depends(authenticated_consumer)):
    logger.info("Новый запрос графика от '%s': %s", consumer, request.question)

    safe_sql, rows, period = await _generate_and_execute_sql(
        request.question,
        background_tasks=background_tasks,
    )
    rows, reported_period = split_reported_period(rows)

    if not rows and period is not None and period.status == "outside" and period.message:
        option = build_chart_option([{"Период": period.message}], request.question)
    else:
        option = build_chart_option(rows, request.question)
    if option is None:
        raise HTTPException(
            422,
            detail=_error(
                "Результат запроса не подходит для визуализации (нет числовых колонок или данных).",
                generated_sql=safe_sql,
            ),
        )
    
    if rows and period is not None and period.message:
        add_period_caption(option, period.message)
    elif rows and reported_period:
        add_period_caption(option, f"Период ответа: {reported_period}")
        
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

    memory_errors = (wren_client.WrenExecutionError, wren_client.WrenNotConfiguredError)

    if request.rating == "up":
        try:
            result = await wren_client.store_example(question, sql)
            action = "обновлён" if result.get("updated") else "сохранён"
            logger.info("Пример %s (👍 от '%s', %s): %s", action, consumer, result.get("file"), question)
        except memory_errors as e:
            logger.warning("Не удалось сохранить пример в Wren memory: %s", e)
    else:
        logger.warning("Плохая оценка (👎 от '%s') | вопрос: %s | SQL: %s", consumer, question, sql)
        try:
            result = await wren_client.forget_example(question, sql)
            if result["action"] == "removed":
                logger.info(
                    "Пример удалён по 👎 от '%s' (файлы: %s, строк индекса: %d): %s",
                    consumer, result["files"], result["rows"], question,
                )
            elif result["action"] == "sql_mismatch":
                logger.info(
                    "👎 на SQL, отличный от сохранённого: подтверждённый пример по вопросу оставлен: %s",
                    question,
                )
        except memory_errors as e:
            logger.warning("Не удалось удалить пример из Wren memory: %s", e)

    return {"status": "ok"}