
import re
import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import Keyword
from .config import MAX_ROWS

FORBIDDEN_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "truncate",
    "grant", "revoke", "create", "attach", "copy", "call",
    "merge", "replace", "vacuum", "reindex", "execute", "do",
}


class UnsafeSQLError(Exception):
    pass


def _strip_markdown_fences(raw_sql: str) -> str:
    # LLM модели часто заворачивают SQL в ```sql ... ``` - убираем это.
    text = raw_sql.strip()
    text = re.sub(r"^```(sql)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


def _ensure_single_statement(parsed: list[Statement]) -> Statement:
    non_empty = [s for s in parsed if s.token_first(skip_cm=True) is not None]
    if len(non_empty) != 1:
        raise UnsafeSQLError(
            f"Ожидался ровно один SQL-стейтмент, получено {len(non_empty)}"
        )
    return non_empty[0]


def _ensure_select_only(statement: Statement) -> None:
    stmt_type = statement.get_type()
    if stmt_type != "SELECT":
        raise UnsafeSQLError(f"Разрешены только SELECT-запросы, получено: {stmt_type}")

    for token in statement.flatten():
        if token.ttype in Keyword and token.value.lower() in FORBIDDEN_KEYWORDS:
            raise UnsafeSQLError(f"Обнаружено запрещённое ключевое слово: {token.value}")


def _enforce_row_limit(sql: str) -> str:
    if re.search(r"\blimit\s+\d+", sql, flags=re.IGNORECASE):
        return sql
    return f"{sql.rstrip(';')} LIMIT {MAX_ROWS}"


def validate_and_sanitize(raw_sql: str) -> str:
    sql = _strip_markdown_fences(raw_sql)
    if not sql:
        raise UnsafeSQLError("Пустой SQL от модели")

    parsed = sqlparse.parse(sql)
    statement = _ensure_single_statement(parsed)
    _ensure_select_only(statement)

    return _enforce_row_limit(sql)
