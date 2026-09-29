
import secrets
from collections import OrderedDict
from .config import QUERY_CACHE_MAX_SIZE

_cache: "OrderedDict[str, dict]" = OrderedDict()


def store(question: str, sql: str) -> str:
    query_id = secrets.token_hex(6)
    _cache[query_id] = {"question": question, "sql": sql}
    _cache.move_to_end(query_id)
    while len(_cache) > QUERY_CACHE_MAX_SIZE:
        _cache.popitem(last=False)
    return query_id


def get(query_id: str) -> dict | None:
    return _cache.get(query_id)