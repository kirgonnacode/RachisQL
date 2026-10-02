import datetime
from decimal import Decimal
from typing import Any
from .config import CHART_TIMEZONE_OFFSET_HOURS
import re
import math
import textwrap

_MS_TIMESTAMP_MIN = 946684800000   # 2000-01-01
_MS_TIMESTAMP_MAX = 4102444800000  # 2100-01-01

_MONTH_ABBR_RU = {
    1: "Янв", 2: "Фев", 3: "Мар", 4: "Апр", 5: "Май", 6: "Июн",
    7: "Июл", 8: "Авг", 9: "Сен", 10: "Окт", 11: "Ноя", 12: "Дек",
}

_PROPORTION_PATTERNS = [
    re.compile(r"\bдол[яию]\b", re.IGNORECASE),
    re.compile(r"\bдолей\b", re.IGNORECASE),
    re.compile(r"\bпроцент", re.IGNORECASE),
    re.compile(r"\bраспредел", re.IGNORECASE),
    re.compile(r"\bструктур", re.IGNORECASE),
    re.compile(r"\bсоотношени", re.IGNORECASE),
    re.compile(r"\bудельный\s+вес", re.IGNORECASE),
]

_COLORS = ['#3D75E4', '#57A003', '#7537F2', '#4FC731', '#F7BA59', '#D62525', '#779EEC', '#BC64DF', '#64C8DF', '#89BD4F']

_PIE_COLORS = ['#3D75E4', '#57A003', '#7537F2', '#4FC731', '#F7BA59', '#D62525']

_PG_DEFAULT_COLUMN_NAMES = {
    "sum", "count", "avg", "min", "max", "round", "coalesce", "nullif", "abs",
    "ceil", "floor", "case", "date_trunc", "date_part", "extract", "to_char",
}

_FMT_NUMBER = "__fmt:number"
_FMT_PIE_LABEL = "__fmt:pie_label"

_TEXT_CARD_WIDTH = 720

_PLOT_WIDTH = 640
_AXIS_LABEL_FONT = 12
_CHAR_WIDTH_RATIO = 0.62
_ROTATED_LABEL_MAX_CHARS = 22
_PIE_LABEL_MAX_CHARS = 24

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _to_json_number(value: Any) -> int | float:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _is_numeric_column(col: str, rows: list[dict]) -> bool:
    non_null_values = [row.get(col) for row in rows if row.get(col) is not None]
    if not non_null_values:
        return False
    return all(_is_number(v) for v in non_null_values)


def _looks_like_ms_timestamp_column(col: str, rows: list[dict]) -> bool:
    non_null_values = [row.get(col) for row in rows if row.get(col) is not None]
    if not non_null_values:
        return False
    return all(
        isinstance(v, int) and not isinstance(v, bool) and _MS_TIMESTAMP_MIN <= v <= _MS_TIMESTAMP_MAX
        for v in non_null_values
    )

def _detect_granularity(sorted_ms_values: list[int]) -> str:
    if len(sorted_ms_values) < 2:
        return "date"

    diffs_days = sorted(
        (sorted_ms_values[i + 1] - sorted_ms_values[i]) / 86_400_000
        for i in range(len(sorted_ms_values) - 1)
    )
    median_diff = diffs_days[len(diffs_days) // 2]

    if 27 <= median_diff <= 32:
        return "month"
    if 350 <= median_diff <= 380:
        return "year"
    if 0.5 <= median_diff <= 2:
        return "day"
    return "date"

def _ms_timestamp_to_label(value: int, granularity: str) -> str:
    tz = datetime.timezone(datetime.timedelta(hours=CHART_TIMEZONE_OFFSET_HOURS))
    dt = datetime.datetime.fromtimestamp(value / 1000, tz=tz)

    if granularity == "month":
        return f"{_MONTH_ABBR_RU[dt.month]}\n{dt.year}"
    if granularity == "year":
        return str(dt.year)
    if granularity == "day":
        return f"{dt.day}\n{_MONTH_ABBR_RU[dt.month].lower()}"
    return dt.strftime("%Y-%m-%d")

def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    truncated = text[:max_len].rsplit(" ", 1)[0]
    if not truncated:
        truncated = text[:max_len]
    return truncated + "…"


def _is_unnamed_column(col: str) -> bool:
    return col == "?column?" or col.lower() in _PG_DEFAULT_COLUMN_NAMES


def _series_name(col: str, index: int) -> str:
    if not _is_unnamed_column(col):
        return col
    return "Значение" if index == 0 else f"Значение {index + 1}"


def _format_number(value: int | float | Decimal) -> str:
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return "—"
        if value.is_integer():
            value = int(value)
        else:
            decimals = 2 if abs(value) >= 1 else 4
            text = f"{value:,.{decimals}f}".rstrip("0").rstrip(".")
            if text in ("0", "-0"):
                return "0"
            return text.replace(",", "\u00a0").replace(".", ",")
    return f"{value:,}".replace(",", "\u00a0")


def _format_card_value(col: str, value: Any, row: dict) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if _looks_like_ms_timestamp_column(col, [row]):
        tz = datetime.timezone(datetime.timedelta(hours=CHART_TIMEZONE_OFFSET_HOURS))
        return datetime.datetime.fromtimestamp(value / 1000, tz=tz).strftime("%d.%m.%Y")
    if _is_number(value):
        return _format_number(value)
    return str(value)


def _rich_safe(text: str) -> str:
    return text.replace("{", "(").replace("}", ")")


def _wrap_rich(style: str, text: str, font_size: int) -> list[str]:
    max_chars = max(8, int(_TEXT_CARD_WIDTH / (font_size * _CHAR_WIDTH_RATIO)))
    lines = textwrap.wrap(_rich_safe(text), width=max_chars, break_long_words=True) or [""]
    return ["{" + style + "|" + line + "}" for line in lines]


def _build_text_card_option(question: str, rows: list[dict]) -> dict:
    row = rows[0]
    columns = list(row.keys())

    metric_cols = [
        c for c in columns
        if _is_number(row[c]) and not _looks_like_ms_timestamp_column(c, [row])
    ]

    if not metric_cols:
        metric_cols = columns
    context_cols = [c for c in columns if c not in metric_cols]

    value_size = {1: 56, 2: 44, 3: 34, 4: 34}.get(len(metric_cols), 26)
    label_size = {1: 20, 2: 18, 3: 16, 4: 16}.get(len(metric_cols), 13)

    context_size = 16
    parts: list[str] = []
    if context_cols:
        context = "  ·  ".join(
            _format_card_value(c, row[c], row) if _is_unnamed_column(c)
            else f"{c}: {_format_card_value(c, row[c], row)}"
            for c in context_cols
        )
        parts.extend(_wrap_rich("context", context, context_size))

    for i, col in enumerate(metric_cols):
        if i > 0 or context_cols:
            parts.append("{gap|}")
        parts.extend(_wrap_rich("value", _format_card_value(col, row[col], row), value_size))
        if not _is_unnamed_column(col):
            parts.extend(_wrap_rich("label", col, label_size))

    return {
        "title": {"text": _truncate(question, 60), "left": "center", "top": 20, "textStyle": {"fontSize": 14}},
        "graphic": {
            "type": "text",
            "left": "center",
            "top": "middle",
            "style": {
                "text": "\n".join(parts),
                "align": "center",
                "rich": {
                    "value": {"fontSize": value_size, "fontWeight": "bold", "fill": "#181818",
                              "lineHeight": int(value_size * 1.25), "align": "center"},
                    "label": {"fontSize": label_size, "fill": "#666666",
                              "lineHeight": int(label_size * 1.4), "align": "center"},
                    "context": {"fontSize": context_size, "fill": "#666666", "lineHeight": 24, "align": "center"},
                    "gap": {"fontSize": 8, "lineHeight": 16},
                },
            },
        },
    }


def _category_text(value: Any) -> str:
    return "—" if value is None or value == "" else str(value)

def _drop_common_words(labels: list[str]) -> list[str]:
    word_sets = [{w.lower() for w in label.split()} for label in labels]
    counts: dict[str, int] = {}

    for words in word_sets:
        for w in words:
            counts[w] = counts.get(w, 0) + 1

    common = {w for w, c in counts.items() if c >= 2 and c >= len(labels) * 0.75}

    if not common:
        return labels

    shortened = [" ".join(w for w in label.split() if w.lower() not in common) for label in labels]

    if any(not x for x in shortened):
        return labels
    
    duplicates = {x for x in shortened if shortened.count(x) > 1}

    return [orig if short in duplicates else short for orig, short in zip(labels, shortened)]


def _layout_category_labels(labels: list[str], plot_width: int) -> tuple[list[str], int]:
    if not labels:
        return labels, 0
    slot = plot_width / len(labels)
    char_px = _AXIS_LABEL_FONT * _CHAR_WIDTH_RATIO

    def fits_flat(items: list[str]) -> bool:
        return max(len(x) for x in items) * char_px <= slot * 0.9

    if fits_flat(labels):
        return labels, 0

    shortened = _drop_common_words(labels)
    if fits_flat(shortened):
        return shortened, 0

    if slot >= 30:
        rotate = 30
    elif slot >= 18:
        rotate = 60
    else:
        rotate = 90
    return [_truncate(x, _ROTATED_LABEL_MAX_CHARS) for x in shortened], rotate


def _data_point(value: float, chart_type: str) -> Any:
    if value == 0:
        return {"value": value, "label": {"show": False}}
    if value < 0:
        point: dict[str, Any] = {"value": value, "label": {"position": "bottom"}}
        if chart_type == "bar":
            point["itemStyle"] = {"borderRadius": [0, 0, 10, 10]}
        return point
    return value


def _mentions_proportion(question: str) -> bool:
    return any(p.search(question) for p in _PROPORTION_PATTERNS)


def _should_use_pie(category_col: str | None, category_is_timestamp: bool, numeric_cols: list[str], num_rows: int, question: str) -> bool:
    if category_col is None or category_is_timestamp:
        return False
    if len(numeric_cols) != 1:
        return False
    if not (2 <= num_rows <= 6):
        return False
    return _mentions_proportion(question)


def _build_pie_option(question: str, categories: list[str], metric_name: str, rows: list[dict]) -> dict:
    data = [
        {"value": _to_json_number(row.get(metric_name)) or 0, "name": cat}
        for cat, row in zip(categories, rows)
    ]
    return {
        "title": {"text": _truncate(question, 60), "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
        "color": _PIE_COLORS,
        "legend": {"icon": "circle", "orient": "horizontal", "bottom": 0, "data": categories},
        "series": [
            {
                "name": _series_name(metric_name, 0),
                "type": "pie",
                "radius": "60%",
                "center": ["50%", "48%"],
                "data": data,
                "label": {"formatter": _FMT_PIE_LABEL},
            }
        ],
    }


def _nice_interval(raw: float) -> float:
    magnitude = 10 ** math.floor(math.log10(raw))
    fraction = raw / magnitude
    for nice in (1, 2, 2.5, 5):
        if fraction <= nice:
            return nice * magnitude
    return 10 * magnitude


def _clean_float(value: float) -> int | float:
    value = float(f"{value:.12g}")
    return int(value) if value.is_integer() else value


def _value_axis(values: list[float], **extra: Any) -> dict:
    axis: dict[str, Any] = {"type": "value", "axisLabel": {"formatter": _FMT_NUMBER}, **extra}

    if not values or min(values) < 0:
        return axis

    axis["min"] = 0
    top = max(values)
    if top <= 0:
        return axis

    target = top * 1.1
    interval = _nice_interval(target / 5)
    axis["max"] = _clean_float(math.ceil(target / interval) * interval)
    axis["interval"] = _clean_float(interval)
    return axis


def _needs_dual_axis(series_raw_values: list[list[float]]) -> bool:
    if len(series_raw_values) != 2:
        return False
    maxes = [max((abs(v) for v in vals), default=0) for vals in series_raw_values]
    if maxes[0] == 0 or maxes[1] == 0:
        return False
    return max(maxes) / min(maxes) >= 10


def build_chart_option(rows: list[dict], question: str) -> dict | None:

    if not rows:
        return None

    columns = list(rows[0].keys())

    if not columns:
        return None
    
    if len(rows) == 1:
        return _build_text_card_option(question, rows)

    category_col = None
    category_is_timestamp = False
    for col in columns:
        if _looks_like_ms_timestamp_column(col, rows):
            category_col = col
            category_is_timestamp = True
            break

    if category_col is None:
        for col in columns:
            if not _is_numeric_column(col, rows):
                category_col = col
                break

    if category_is_timestamp:
        rows = sorted(rows, key=lambda r: (r.get(category_col) is None, r.get(category_col) or 0))

    numeric_cols = [c for c in columns if c != category_col and _is_numeric_column(c, rows)]

    if not numeric_cols:
        return None

    if category_col is None:
        categories = [str(i + 1) for i in range(len(rows))]
    elif category_is_timestamp:
        ts_values = [row.get(category_col) for row in rows if row.get(category_col) is not None]
        granularity = _detect_granularity(sorted(ts_values))
        categories = [
            _ms_timestamp_to_label(row.get(category_col), granularity) if row.get(category_col) is not None else ""
            for row in rows
        ]
    else:
        categories = [_category_text(row.get(category_col)) for row in rows]

    if _should_use_pie(category_col, category_is_timestamp, numeric_cols, len(rows), question):
        pie_labels = [_truncate(x, _PIE_LABEL_MAX_CHARS) for x in _drop_common_words(categories)]
        return _build_pie_option(question, pie_labels, numeric_cols[0], rows)   

    chart_type = "line" if category_is_timestamp and len(categories) > 15 else "bar"

    series_raw_values = [[_to_json_number(row.get(col)) or 0 for row in rows] for col in numeric_cols]
    dual_axis = _needs_dual_axis(series_raw_values)

    if category_is_timestamp:
        x_axis_label = {"rotate": 30 if len(categories) > 8 and "\n" not in "".join(categories) else 0}
    else:
        plot_width = _PLOT_WIDTH - (30 if dual_axis else 0)
        categories, label_rotate = _layout_category_labels(categories, plot_width)
        x_axis_label = {"interval": 0, "rotate": label_rotate}

    series = []
    for i, col in enumerate(numeric_cols):
        raw_values = series_raw_values[i]
        data_points = [_data_point(v, chart_type) for v in raw_values]
        entry = {
            "name": _series_name(col, i),
            "type": chart_type,
            "data": data_points,
            "label": {"show": True, "position": "top", "formatter": _FMT_NUMBER},
            "labelLayout": {"hideOverlap": True},
            "itemStyle": {"borderRadius": [10, 10, 0, 0] if chart_type == "bar" else [0, 0, 0, 0]},
        }
        if chart_type == "line":
            entry["smooth"] = True
            entry["areaStyle"] = {"opacity": 0.15}
        if dual_axis:
            entry["yAxisIndex"] = i
        series.append(entry)

    if dual_axis:
        y_axis = [
            _value_axis(
                vals,
                position="left" if i == 0 else "right",
                splitLine={"show": i == 0},
            )
            for i, vals in enumerate(series_raw_values)
        ]
    else:
        all_values = [v for vals in series_raw_values for v in vals]
        y_axis = _value_axis(all_values)   

    option = {
        "title": {"text": _truncate(question, 60), "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "axis"},
        "color": _COLORS,
        "grid": {"top": 70, "left": 50, "right": 30 if not dual_axis else 60, "bottom": 60, "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": categories,
            "axisLabel": x_axis_label,
        },
        "yAxis": y_axis,
        "series": series,
    }

    if len(numeric_cols) > 1:
        option["legend"] = {"icon": "circle", "data": [_series_name(c, i) for i, c in enumerate(numeric_cols)]}
        if len(numeric_cols) > 6:
            option["legend"]["bottom"] = 0
            option["grid"]["bottom"] = 90
        else:
            option["legend"]["top"] = 28

    return option