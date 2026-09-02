"""
Принцип работы:
- Пустой результат -> None (main.py вернёт текстовый ответ без картинки).
- Первая колонка не-числового типа (строка/дата) -> ось X (категории).
- Остальные числовые колонки -> серии.
- Если категорий много (>15) и есть одна числовая серия -> line chart, иначе bar chart.
- Если данные вообще не подходят под категория+числа (например, один
  агрегат SELECT COUNT(*) -> одна строка/одна колонка) -> одиночный bar.

"""

import datetime
from decimal import Decimal
from typing import Any
from .config import CHART_TIMEZONE_OFFSET_HOURS

_MS_TIMESTAMP_MIN = 946684800000   # 2000-01-01
_MS_TIMESTAMP_MAX = 4102444800000  # 2100-01-01


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


def _ms_timestamp_to_label(value: int) -> str:
    tz = datetime.timezone(datetime.timedelta(hours=CHART_TIMEZONE_OFFSET_HOURS))
    dt = datetime.datetime.fromtimestamp(value / 1000, tz=tz)
    return dt.strftime("%Y-%m-%d")


def build_chart_option(rows: list[dict], question: str) -> dict | None:
    if not rows:
        return None

    columns = list(rows[0].keys())
    if not columns:
        return None

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

    numeric_cols = [c for c in columns if c != category_col and _is_numeric_column(c, rows)]

    if not numeric_cols:
        return None

    if category_col is None:
        categories = [str(i + 1) for i in range(len(rows))]
    elif category_is_timestamp:
        categories = [_ms_timestamp_to_label(row.get(category_col)) for row in rows]
    else:
        categories = [str(row.get(category_col)) for row in rows]

    chart_type = "line" if len(categories) > 15 else "bar"

    series = [
        {
            "name": col,
            "type": chart_type,
            "data": [_to_json_number(row.get(col)) or 0 for row in rows],
            "label": {"show": True, "position": "top"},
            "itemStyle": {"color": '#3D75E4', "borderRadius": [10, 10, 0, 0]},
        }
        for col in numeric_cols
    ]

    option = {
        "title": {"text": question[:60], "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "axis"},
        "grid": {"top": 70, "left": 50, "right": 30, "bottom": 60, "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": categories,
            "axisLabel": {"rotate": 30 if len(categories) > 8 else 0},
        },
        "yAxis": {"type": "value"},
        "series": series,
    }

    if len(numeric_cols) > 1:
        option["legend"] = {"top": 28, "data": numeric_cols}

    return option
