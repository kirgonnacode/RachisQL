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


def _load_dictionaries() -> dict[str, dict[str, str]]:
    dictionaries: dict[str, dict[str, str]] = {}
    dict_dir = Path(WREN_PROJECT_DIR) / "knowledge" / "dictionaries"
    if not dict_dir.is_dir():
        return dictionaries

    for yml_file in sorted(dict_dir.glob("*.yml")):
        try:
            mapping = yaml.safe_load(yml_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            logger.warning("Не удалось прочитать словарь %s: %s", yml_file, e)
            continue
        if isinstance(mapping, dict):
            dictionaries[yml_file.stem] = {str(k): str(v) for k, v in mapping.items()}

    return dictionaries


_column_dictionary_map = _load_column_dictionary_map()
_dictionaries = _load_dictionaries()
logger.info(
    "Загружено сопоставлений колонка->словарь: %d, словарей: %d",
    len(_column_dictionary_map), len(_dictionaries),
)


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

        best = process.extractOne(core, dictionary.keys(), scorer=fuzz.token_set_ratio)
        if best is None or best[1] < DICTIONARY_MATCH_THRESHOLD:
            logger.warning(
                "Не найдено уверенное совпадение для '%s' в словаре '%s' (лучший результат: %s), SQL исполнится без изменений",
                raw_value, dict_name, best,
            )
            return match.group(0)

        canonical = dictionary[best[0]]
        logger.info("Значение '%s' -> '%s' (словарь '%s', score=%.1f)", raw_value, canonical, dict_name, best[1])
        new_value = f"{leading}{canonical}{trailing}"
        quoted_column = match.group("column")
        return f"{quoted_column} {op} '{new_value}'"

    return _VALUE_PATTERN.sub(_replace, sql)