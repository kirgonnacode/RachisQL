import asyncio
from calendar import monthrange
from datetime import date
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


def _axis_unit(node, alias, model, axis, kind, label):
    node = node.unnest()
    if isinstance(node, exp.Extract) and node.this.name.upper() == "YEAR":
        if _axis_unit(node.expression, alias, model, axis, kind, label) == "day":
            return "year"
    if isinstance(node, exp.StrToDate) and kind == "mixed":
        fmt = node.args.get("format")
        col = node.this
        if (label == "Последние 12 недель" and isinstance(fmt, exp.Literal)
                and fmt.this == "%d.%m.%Y" and isinstance(col, exp.Column)
                and col.name == axis and col.table in ("", alias)):
            return "day"
    if isinstance(node, exp.Cast) and node.to.sql() == "DATE":
        node = node.this.unnest()
    if not isinstance(node, exp.Column) or node.table not in ("", alias):
        return None
    if model == "svn_ii" and node.name == "period_key":
        return {"Последние 12 лет": "year_key", "Последние 12 месяцев": "month_key"}.get(label)
    if node.name != axis or kind == "mixed":
        return None
    return "year" if model == "osg_voice_for" and axis == "year" else "day"


def _literal(node, unit, requested):
    node = node.unnest()
    if isinstance(node, exp.Cast) and node.to.sql() == "DATE":
        node = node.this
    if isinstance(node, exp.CurrentDate) and requested.as_of and unit == "day":
        return requested.end.toordinal()
    if not isinstance(node, exp.Literal):
        return None
    value = node.this
    if unit == "day":
        try:
            return date.fromisoformat(value).toordinal() if len(value) == 10 else None
        except ValueError:
            return None
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 6 else None


def _expected(unit, requested):
    start, end = requested.start, requested.end
    if unit == "day":
        return start.toordinal(), end.toordinal(), end.toordinal() + 1
    if unit == "month_key":
        if start.day != 1 or end.day != monthrange(end.year, end.month)[1]:
            return None
        last = end.year * 100 + end.month
        following = last + 1 if end.month < 12 else (end.year + 1) * 100 + 1
        return start.year * 100 + start.month, last, following
    if (start.month, start.day, end.month, end.day) != (1, 1, 12, 31):
        return None
    scale = 100 if unit == "year_key" else 1
    return start.year * scale, end.year * scale, (end.year + 1) * scale


def _matches_period(terms, alias, model, axis, kind, label, requested):
    lower = upper = False
    axes = {axis, "period_key"} if model == "svn_ii" else {axis}
    for term in terms:
        if not any(c.name in axes for c in term.find_all(exp.Column)):
            continue
        if not isinstance(term, (exp.EQ, exp.GTE, exp.LT, exp.LTE, exp.Between)):
            return False
        unit = _axis_unit(term.this, alias, model, axis, kind, label)
        bounds = _expected(unit, requested) if unit else None
        if not bounds:
            return False
        start, end, following = bounds
        if isinstance(term, exp.Between):
            if (_literal(term.args["low"], unit, requested),
                _literal(term.args["high"], unit, requested)) != (start, end):
                return False
            lower = upper = True
            continue
        value = _literal(term.expression, unit, requested)
        if isinstance(term, exp.EQ) and value == start == end:
            lower = upper = True
        elif isinstance(term, exp.GTE) and value == start:
            lower = True
        elif isinstance(term, exp.LTE) and value == end:
            upper = True
        elif isinstance(term, exp.LT) and value == following:
            upper = True
        else:
            return False
    if requested.as_of and kind in {"snapshot", "version"}:
        return upper and not lower
    return lower and upper


def _scope_filters(sql, model, axis, label_column, kind, requested):
    tree = normalize_identifiers(parse_one(sql, read="postgres"), dialect="postgres")
    labels = set()
    for scope in traverse_scope(tree):
        sources = scope.selected_sources
        direct = [alias for alias, (_, src) in sources.items()
                  if isinstance(src, exp.Table) and src.name == model]
        if not direct:
            continue
        where = scope.expression.args.get("where")
        if where is None or len(sources) != 1:
            return False, None
        predicate = where.this.unnest()
        terms = [t.unnest() for t in predicate.flatten()] if isinstance(predicate, exp.And) else [predicate]
        alias = direct[0]
        group = set()
        if label_column:
            for t in terms:
                if isinstance(t, exp.EQ):
                    for col, val in ((t.left, t.right), (t.right, t.left)):
                        if (isinstance(col, exp.Column) and col.name == label_column
                                and col.table in ("", alias) and isinstance(val, exp.Literal)
                                and val.is_string):
                            group.add(val.this)
            if len(group) != 1:
                return False, None
        label = next(iter(group), None)
        if not _matches_period(terms, alias, model, axis, kind, label, requested):
            return False, None
        labels.add(label)
    return (True, labels.pop()) if len(labels) == 1 else (False, None)


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
        supported, label = _scope_filters(sql, model, axis, label_column, kind, requested)
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
