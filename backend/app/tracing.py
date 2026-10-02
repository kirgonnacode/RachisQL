import time
from fastapi import Request
from fastapi.responses import JSONResponse
from .logging_config import logger
from .request_context import new_request_id, request_id

TRACED_PATHS = {"/ask", "/ask/image"}

_RULE = "═" * 70


async def trace_request(request: Request, call_next):
    if request.url.path not in TRACED_PATHS:
        return await call_next(request)

    query_id = new_request_id()
    token = request_id.set(query_id)
    started_at = time.monotonic()
    logger.info(_RULE)
    logger.info("▶ НАЧАЛО %s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
        response.headers["X-Query-Id"] = query_id
        mark = "✔" if response.status_code < 400 else "✘"
        logger.info("%s КОНЕЦ: HTTP %d за %.2fс", mark, response.status_code, time.monotonic() - started_at)
        return response
    except Exception:
        logger.exception("✘ КОНЕЦ: необработанная ошибка за %.2fс", time.monotonic() - started_at)
        return JSONResponse(
            status_code=500,
            content={"detail": "Внутренняя ошибка сервера", "generated_sql": None, "query_id": query_id},
            headers={"X-Query-Id": query_id},
        )
    finally:
        logger.info(_RULE)
        request_id.reset(token)