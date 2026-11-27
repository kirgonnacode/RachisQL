import re
from pathlib import Path
import yaml
from rapidfuzz import fuzz, process
from .config import DICTIONARY_MATCH_THRESHOLD, WREN_PROJECT_DIR
from .logging_config import logger

_VALUE_PATTERN = re.compile(
    r'(?P<column>"[^"]+"|\b\w+\b)\s*(?P<op>=|ILIKE|LIKE)\s*\'(?P<value>[^\']*)\'',
    re.IGNORECASE,
)


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen: set = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:
                continue
            if duplicate:
                logger.warning(
                    "Словарь %s: ключ '%s' указан несколько раз (строка %d), используется последнее значение",
                    getattr(self, "source_name", "?"), key, key_node.start_mark.line + 1,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _norm(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


def _load_column_dictionary_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    models_dir = Path(WREN_PROJECT_DIR) / "models"
    if not models_dir.is_dir():
        return mapping

    for metadata_file in sorted(models_dir.glob("*/metadata.yml")):
        try:
            data = yaml.safe_load(metadata_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            logger.warning("Не удалось прочитать %s: %s", metadata_file, e)
            continue
        for col in data.get("columns", []) or []:
            dict_name = (col.get("properties") or {}).get("dictionary")
            if dict_name:
                mapping[col["name"]] = dict_name

    return mapping


def _build_index(dictionary: dict[str, str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for variant, canonical in dictionary.items():
        index.setdefault(_norm(variant), canonical)
    for canonical in dictionary.values():
        index.setdefault(_norm(canonical), canonical)
    return index


def _load_dictionaries() -> dict[str, dict[str, str]]:
    """Возвращает словари уже в виде нормализованных индексов."""
    dictionaries: dict[str, dict[str, str]] = {}
    dict_dir = Path(WREN_PROJECT_DIR) / "knowledge" / "dictionaries"
    if not dict_dir.is_dir():
        return dictionaries

    for yml_file in sorted(dict_dir.glob("*.yml")):
        try:
            loader = _UniqueKeyLoader(yml_file.read_text(encoding="utf-8"))
            loader.source_name = yml_file.name
            try:
                mapping = loader.get_single_data() or {}
            finally:
                loader.dispose()
        except yaml.YAMLError as e:
            logger.warning("Не удалось прочитать словарь %s: %s", yml_file, e)
            continue
        if isinstance(mapping, dict):
            dictionaries[yml_file.stem] = _build_index({str(k): str(v) for k, v in mapping.items()})

    return dictionaries


def _best_match(value: str, index: dict[str, str]) -> tuple[str, float, str] | None:
    """
    Поиск канонического значения в два шага.
    1) Точное совпадение после нормализации: регистр и лишние пробелы не важны.
    2) Fuzzy (token_set_ratio). Если слова одного значения
       целиком входят в другое, score = 100 у обоих, поэтому среди кандидатов с одинаковым
       лучшим score выбираем ближайший целиком (fuzz.ratio).
    Возвращает (каноническое значение, score, способ) или None.
    """
    query = _norm(value)
    if query in index:
        return index[query], 100.0, "точно"

    candidates = process.extract(query, index.keys(), scorer=fuzz.token_set_ratio, limit=10)
    if not candidates:
        return None
    top_score = candidates[0][1]
    tied = [(key, score) for key, score, _ in candidates if score >= top_score - 0.5]
    best_key, best_score = max(tied, key=lambda item: fuzz.ratio(query, item[0]))
    return index[best_key], best_score, "fuzzy"


_column_dictionary_map = _load_column_dictionary_map()
_dictionaries = _load_dictionaries()
logger.info(
    "Загружено сопоставлений колонка->словарь: %d, словарей: %d",
    len(_column_dictionary_map), len(_dictionaries),
)


def resolve_dictionary_value(dictionary_name: str, value: str) -> str | None:
    index = _dictionaries.get(dictionary_name)
    if not index or not value.strip():
        return None

    match = _best_match(value, index)
    if match is None:
        return None

    canonical, score, _ = match
    if score < DICTIONARY_MATCH_THRESHOLD:
        return None

    return canonical


def _strip_wildcards(value: str) -> tuple[str, str, str]:
    leading = "%" if value.startswith("%") else ""
    trailing = "%" if value.endswith("%") else ""
    core = value[len(leading): len(value) - len(trailing)] if trailing else value[len(leading):]
    return leading, core, trailing


def resolve_dictionary_values(sql: str) -> str:
    if not _column_dictionary_map:
        return sql

    def _replace(match: re.Match) -> str:
        column = match.group("column").strip('"')
        op = match.group("op")
        raw_value = match.group("value")

        dict_name = _column_dictionary_map.get(column)
        if not dict_name:
            return match.group(0)

        dictionary = _dictionaries.get(dict_name)
        if not dictionary:
            logger.warning("Колонка '%s' ссылается на несуществующий словарь '%s'", column, dict_name)
            return match.group(0)

        leading, core, trailing = _strip_wildcards(raw_value)
        if not core.strip():
            return match.group(0)

        best = _best_match(core, dictionary)
        if best is None or best[1] < DICTIONARY_MATCH_THRESHOLD:
            logger.warning(
                "Не найдено уверенное совпадение для '%s' в словаре '%s' (лучший результат: %s), SQL исполнится без изменений",
                raw_value, dict_name, best,
            )
            return match.group(0)

        canonical, score, how = best
        logger.info("Значение '%s' -> '%s' (словарь '%s', %s, score=%.1f)", raw_value, canonical, dict_name, how, score)
        new_value = f"{leading}{canonical}{trailing}".replace("'", "''")
        quoted_column = match.group("column")
        return f"{quoted_column} {op} '{new_value}'"

    return _VALUE_PATTERN.sub(_replace, sql)