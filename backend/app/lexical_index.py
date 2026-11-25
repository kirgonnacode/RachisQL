
#Лексический поиск таблиц (BM25) по metadata.yml

import collections
import math
import re
import threading
from pathlib import Path
import yaml
from .config import WREN_PROJECT_DIR
from .logging_config import logger

_K1 = 1.2           # насыщение частоты слова: 10 повторов не в 10 раз важнее одного
_B = 0.75           # нормировка на длину документа: таблица с множеством колонок не выигрывает объёмом
_HEAD_WORDS = 30    # шапка = первые 30 слов описания(предел модели paraphrase-multilingual-MiniLM-L12-v2)

_lock = threading.Lock()
_index = None


def _stems(text: str) -> list[str]:
    out = []
    for word in re.findall(r"[a-zа-яё0-9]+", text.casefold().replace("ё", "е")):
        if len(word) >= 2:
            out.append(word[:5] if len(word) >= 6 else word[:4] if len(word) >= 4 else word)
    return out


def _models_dir() -> Path:
    return Path(WREN_PROJECT_DIR) / "models"


def _signature() -> tuple:
    """Список файлов и время их изменения: если поменялся хоть один metadata.yml значит индекс устарел."""
    return tuple(sorted((str(p), p.stat().st_mtime_ns) for p in _models_dir().glob("*/metadata.yml")))


def _build(signature: tuple):
    docs: dict[str, collections.Counter] = {}
    modules: dict[str, str] = {}
    for meta in sorted(_models_dir().glob("*/metadata.yml")):
        try:
            data = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            logger.warning("Лексический индекс: не удалось прочитать %s: %s", meta, e)
            continue
        name = data.get("name") or meta.parent.name
        properties = data.get("properties") or {}
        module = properties.get("module")
        if isinstance(module, str) and module.strip():
            modules[name] = module.strip()
        description = str(properties.get("description") or "")
        head = " ".join(description.split()[:_HEAD_WORDS])   # шапка: первые N слов
        columns = " ".join(
            str((col.get("properties") or {}).get("description") or "") for col in data.get("columns") or []
        )
        docs[name] = collections.Counter(_stems(f"{head} {head} {columns}"))

    n_docs = len(docs) or 1
    avg_len = (sum(sum(c.values()) for c in docs.values()) / n_docs) or 1.0
    df = collections.Counter(stem for counts in docs.values() for stem in counts)
    idf = {stem: math.log(1 + (n_docs - n + 0.5) / (n + 0.5)) for stem, n in df.items()}
    logger.info("Лексический индекс построен: %d таблиц, %d уникальных слов", len(docs), len(idf))
    return signature, docs, idf, avg_len, modules


def rank_with_modules(question: str) -> tuple[list[tuple[str, float]], dict[str, str]]:
    """Возвращает BM25-рейтинг и модули таблиц из одного снимка индекса."""
    global _index
    with _lock:
        signature = _signature()
        if _index is None or _index[0] != signature:
            _index = _build(signature)
        _, docs, idf, avg_len, modules = _index
 
    query = set(_stems(question))
    scores: dict[str, float] = {}
    for name, counts in docs.items():
        length = sum(counts.values())
        score = sum(
            idf[t] * counts[t] * (_K1 + 1) / (counts[t] + _K1 * (1 - _B + _B * length / avg_len))
            for t in query & counts.keys()
        )
        if score > 0:
            scores[name] = score
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return ranked, modules


def rank(question: str) -> list[tuple[str, float]]:
    """Возвращает таблицы по убыванию BM25-оценки."""
    ranked, _ = rank_with_modules(question)
    return ranked
