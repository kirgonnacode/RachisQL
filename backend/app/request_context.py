"""
ID текущего запроса, доступный из любого места кода без передачи параметром.
"""
import secrets
from contextvars import ContextVar

# "-" для строк вне запросов пользователей: старт, прогрев, /live
request_id: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return secrets.token_hex(6)