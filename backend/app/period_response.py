import asyncio
from pathlib import Path
import yaml
from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import traverse_scope
from .config import WREN_PROJECT_DIR
from .dates import Period
from .models import AnswerPeriod
from .period_bounds import get_bounds
from .period_coverage import compare_bounds, partial_period_caption
from .schema_context import referenced_models


def _scope_filters(sql: str, model: str, axis: str, label_column: str | None):
    tree = normalize_identifiers(parse_one(sql, read="postgres"), dialect="postgres")
    labels = set()
    found_axis = False
    for scope in traverse_scope(tree):
        direct = [(alias, src) for alias, (_, src) in scope.selected_sources.items()
                  if isinstance(src, exp.Table) and src.name == model]
        if not direct:
            continue
        where = scope.expression.args.get("where")
        if not where or len(scope.selected_sources) != 1:
            return False, None
        alias, _ = direct[0]
        def local(col):
            return col.table in ("", alias) and col.find_ancestor(exp.Select) is scope.expression

        terms = list(where.this.unnest().flatten()) if isinstance(where.this.unnest(), exp.And) else [where.this]
        allowed = {axis}
        if model == "svn_ii":
            allowed.add("period_key")
        has_axis = False
        group = set()
        for term in terms:
            term = term.unnest()
            if isinstance(term, (exp.EQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Between, exp.In)):
                if any(local(col) and col.name in allowed for col in term.find_all(exp.Column)):
                    has_axis = True
            if isinstance(term, exp.EQ):
                for col, val in ((term.left, term.right), (term.right, term.left)):
                    if (isinstance(col, exp.Column) and local(col) and col.name == label_column
                            and isinstance(val, exp.Literal) and val.is_string):
                        group.add(val.this)
        if label_column and len(group) != 1:
            return False, None
        labels.update(group)
        found_axis |= has_axis
    return found_axis and len(labels) <= 1, next(iter(labels), None)


async def describe_period(sql: str, requested: Period | None, models: dict) -> AnswerPeriod | None:
    if requested is None:
        return None
    names = referenced_models(sql)
    if len(names) != 1:
        return None
    model = next(iter(names))
    if model not in models:
        return None
    path = Path(WREN_PROJECT_DIR) / "models" / model / "metadata.yml"
    try:
        data = yaml.safe_load(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        props = data["properties"]
        kind, axis = props["period_kind"], props.get("period_column")
        if not axis:
            return None
        label_column = props.get("period_label_column")
    except (OSError, KeyError, TypeError, AttributeError, ValueError, yaml.YAMLError):
        return None
    try:
        supported, label = _scope_filters(sql, model, axis, label_column)
    except SqlglotError:
        return None
    if not supported:
        return None
    bounds = await get_bounds(model, label)
    if not bounds:
        return None
    if requested.as_of and kind not in {"snapshot", "version", "event"}:
        return None
    coverage = compare_bounds(requested, bounds.first, bounds.last,
                              use_prior_snapshot=kind in {"snapshot", "version"})
    if coverage is None:
        return None
    message = partial_period_caption(requested, coverage)
    if coverage.status == "outside":
        message = (f"Запрошено {requested.start:%d.%m.%Y}–{requested.end:%d.%m.%Y}. "
                   f"Отчётные даты таблицы: {bounds.first:%d.%m.%Y}–{bounds.last:%d.%m.%Y}.")
    return AnswerPeriod(model=model, label=label, status=coverage.status,
                        requested_from=requested.start, requested_to=requested.end,
                        available_from=bounds.first, available_to=bounds.last, message=message)
