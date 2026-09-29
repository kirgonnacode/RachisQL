import datetime
from decimal import Decimal
from typing import Any
from .config import CHART_TIMEZONE_OFFSET_HOURS
import re

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


def _build_text_card_option(question: str, rows: list[dict]) -> dict:
    row = rows[0]
    lines = []
    for col, value in row.items():
        display = _to_json_number(value) if _is_number(value) else value
        lines.append(f"{col}: {display}")

    return {
        "title": {"text": _truncate(question, 60), "left": "center", "top": 20, "textStyle": {"fontSize": 14}},
        "graphic": {
            "type": "text",
            "left": "center",
            "top": "middle",
            "style": {
                "text": "\n".join(lines),
                "fontSize": 32,
                "fontWeight": "bold",
                "fill": "#181818",
                "textAlign": "center",
                "lineHeight": 44,
            },
        },
    }

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
                "name": metric_name,
                "type": "pie",
                "radius": "60%",
                "center": ["50%", "48%"],
                "data": data,
                "label": {"formatter": "{b}\n{d}%"},
            }
        ],
    }


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
        categories = [_truncate(str(row.get(category_col)), 20) for row in rows]

    if _should_use_pie(category_col, category_is_timestamp, numeric_cols, len(rows), question):
        return _build_pie_option(question, categories, numeric_cols[0], rows)        

    chart_type = "line" if len(categories) > 15 else "bar"

    series_raw_values = [[_to_json_number(row.get(col)) or 0 for row in rows] for col in numeric_cols]
    dual_axis = _needs_dual_axis(series_raw_values)

    series = []
    for i, col in enumerate(numeric_cols):
        raw_values = series_raw_values[i]
        data_points = [
            {"value": v, "label": {"show": False}} if v == 0 else v
            for v in raw_values
        ]
        entry = {
            "name": col,
            "type": chart_type,
            "data": data_points,
            "label": {"show": True, "position": "top"},
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
            {
                "type": "value", "min": 0,
                "max": max(vals) * 1.15 if max(vals) > 0 else None,
                "position": "left" if i == 0 else "right",
                "splitLine": {"show": i == 0},
            }
            for i, vals in enumerate(series_raw_values)
        ]
    else:
        all_values = [v for vals in series_raw_values for v in vals]
        y_max = max(all_values) * 1.15 if all_values and max(all_values) > 0 else None
        y_axis = {"type": "value", "min": 0, "max": y_max}        

    option = {
        "title": {"text": _truncate(question, 60), "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "axis"},
        "color": _COLORS,
        "grid": {"top": 70, "left": 50, "right": 30 if not dual_axis else 60, "bottom": 60, "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": categories,
            "axisLabel": {"rotate": 30 if len(categories) > 8 and "\n" not in "".join(categories) else 0},
        },
        "yAxis": y_axis,
        "series": series,
    }

    if len(numeric_cols) > 1:
        option["legend"] = {"icon": "circle", "data": numeric_cols}
        if len(numeric_cols) > 6:
            option["legend"]["bottom"] = 0
            option["grid"]["bottom"] = 90
        else:
            option["legend"]["top"] = 28

    return option
