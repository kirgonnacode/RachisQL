from pathlib import Path
from typing import Annotated, Literal, Self
import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator
from yaml.constructor import ConstructorError


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Key = Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]*$")]


class CatalogError(ValueError):
    pass


class CatalogLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node)
                if not isinstance(key, str) or key in seen:
                    raise ConstructorError(None, None, f"Invalid or duplicate key: {key!r}", key_node.start_mark)
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Dataset(Spec):
    schema_name: Text = Field(alias="schema")
    table: Text
    time_column: Text


class Column(Spec):
    column: Text
    label: Text


class DictionaryDimension(Column):
    dictionary: Text
    display_labels: dict[Text, Text] = Field(default_factory=dict)
    

class MetricBase(Spec):
    id: Key
    name: Text
    dataset: Key
    description: Text
    synonyms: list[Text] = Field(default_factory=list)
    period_label: Text


class MonthlyCount(MetricBase):
    calculation: Literal["monthly_count"]
    filters: dict[Text, str] = Field(default_factory=dict)
    group_by: list[Column] = Field(default_factory=list)
    value_label: Text


class LatestSnapshot(MetricBase):
    calculation: Literal["latest_snapshot_per_month"]
    columns: list[Column] = Field(min_length=1)


class NumericAggregate(MetricBase):
    calculation: Literal["numeric_aggregate"]
    operation: Literal["sum", "avg"]
    measure: Column
    dimension: Key


Metric = Annotated[
    MonthlyCount | LatestSnapshot | NumericAggregate,
    Field(discriminator="calculation"),
]


class Catalog(Spec):
    module: Text
    datasets: dict[Key, Dataset]
    dimensions: dict[Key, DictionaryDimension] = Field(default_factory=dict)    
    metrics: list[Metric] = Field(min_length=1)

    @model_validator(mode="after")
    def check_metrics(self) -> Self:
        seen = set()
        for metric in self.metrics:
            if metric.id in seen:
                raise ValueError(f"Duplicate metric id: {metric.id}")
            seen.add(metric.id)
            if metric.dataset not in self.datasets:
                raise ValueError(f"{metric.id}: unknown dataset {metric.dataset}")
            labels = [metric.period_label]
            if isinstance(metric, MonthlyCount):
                labels.extend(item.label for item in metric.group_by)
                labels.append(metric.value_label)
            elif isinstance(metric, NumericAggregate):
                if metric.dimension not in self.dimensions:
                    raise ValueError(f"{metric.id}: unknown dimension {metric.dimension}")
                labels.extend([self.dimensions[metric.dimension].label, metric.measure.label])                
            else:
                labels.extend(item.label for item in metric.columns)
            if len(labels) != len(set(labels)):
                raise ValueError(f"{metric.id}: duplicate output label")
            if any(len(label.encode("utf-8")) > 63 or "\x00" in label for label in labels):
                raise ValueError(f"{metric.id}: invalid PostgreSQL output label")
        return self


def load_catalog(path: Path) -> Catalog:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=CatalogLoader)
        return Catalog.model_validate(raw)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise CatalogError(f"{path}: {exc}") from exc


def load_catalogs(directory: Path) -> dict[str, Catalog]:
    by_module = {}
    sources = {}
    for path in sorted(directory.glob("*")):
        if not path.is_file() or path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        catalog = load_catalog(path)
        if catalog.module in sources:
            raise CatalogError(f"Duplicate module {catalog.module!r}: {sources[catalog.module]} and {path}")
        by_module[catalog.module] = catalog
        sources[catalog.module] = path
    return by_module
