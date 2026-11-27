from dataclasses import dataclass
from pathlib import Path
import re
import yaml


_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Dataset:
    name: str
    kind: str
    time: str
    grain: str


@dataclass(frozen=True)
class Metric:
    id: str
    name: str
    synonyms: tuple[str, ...]
    dataset: Dataset
    value: str
    filters: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Catalog:
    module: str
    metrics: tuple[Metric, ...]


def _mapping(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise CatalogError(f"{field}: ожидается словарь")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{field}: ожидается непустая строка")
    return value.strip()


def _identifier(value: object, field: str) -> str:
    name = _text(value, field)
    if not _IDENTIFIER.fullmatch(name):
        raise CatalogError(f"{field}: недопустимый идентификатор {name!r}")
    return name


def _read_dataset(name: object, raw: object) -> Dataset:
    name = _identifier(name, "datasets")
    data = _mapping(raw, f"datasets.{name}")
    return Dataset(
        name=name,
        kind=_text(data.get("kind"), f"{name}.kind"),
        time=_identifier(data.get("time"), f"{name}.time"),
        grain=_text(data.get("grain"), f"{name}.grain"),
    )


def _read_metric(raw: object, datasets: dict[str, Dataset]) -> Metric:
    data = _mapping(raw, "metrics[]")
    metric_id = _identifier(data.get("id"), "metrics[].id")
    dataset_name = _identifier(data.get("dataset"), f"{metric_id}.dataset")
    if dataset_name not in datasets:
        raise CatalogError(f"{metric_id}: неизвестный dataset {dataset_name!r}")

    value = _text(data.get("value"), f"{metric_id}.value")
    if value != "count(*)":
        raise CatalogError(f"{metric_id}: неподдерживаемая агрегация {value!r}")

    synonyms = data.get("synonyms", [])
    if not isinstance(synonyms, list):
        raise CatalogError(f"{metric_id}.synonyms: ожидается список")

    filters = _mapping(data.get("filters", {}), f"{metric_id}.filters")
    return Metric(
        id=metric_id,
        name=_text(data.get("name"), f"{metric_id}.name"),
        synonyms=tuple(_text(item, f"{metric_id}.synonyms") for item in synonyms),
        dataset=datasets[dataset_name],
        value=value,
        filters=tuple(
            (
                _identifier(column, f"{metric_id}.filters"),
                _text(expected, f"{metric_id}.filters.{column}"),
            )
            for column, expected in filters.items()
        ),
    )


def load_catalog(path: Path) -> Catalog:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        data = _mapping(raw, "catalog")
        module = _text(data.get("module"), "module")

        raw_datasets = _mapping(data.get("datasets"), "datasets")
        datasets = {}
        for name, entry in raw_datasets.items():
            dataset = _read_dataset(name, entry)
            datasets[dataset.name] = dataset

        entries = data.get("metrics")
        if not isinstance(entries, list) or not entries:
            raise CatalogError("metrics: ожидается непустой список")

        metrics = {}
        for entry in entries:
            metric = _read_metric(entry, datasets)
            if metric.id in metrics:
                raise CatalogError(f"повторяется id метрики {metric.id!r}")
            metrics[metric.id] = metric

        return Catalog(module=module, metrics=tuple(metrics.values()))
    except (OSError, UnicodeError, yaml.YAMLError, CatalogError) as exc:
        raise CatalogError(f"{path}: {exc}") from exc