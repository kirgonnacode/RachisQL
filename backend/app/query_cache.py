from collections import OrderedDict
from .config import QUERY_CACHE_MAX_SIZE

_cache: "OrderedDict[str, dict]" = OrderedDict()


def store(query_id: str, question: str, sql: str) -> None:
    _cache[query_id] = {"question": question, "sql": sql}
    _cache.move_to_end(query_id)
    while len(_cache) > QUERY_CACHE_MAX_SIZE:
        _cache.popitem(last=False)


def get(query_id: str) -> dict | None:
    return _cache.get(query_id)