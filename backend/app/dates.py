import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date


_MONTH_FORMS = (
    "январь января январе", "февраль февраля феврале", "март марта марте",
    "апрель апреля апреле", "май мая мае", "июнь июня июне",
    "июль июля июле", "август августа августе", "сентябрь сентября сентябре",
    "октябрь октября октябре", "ноябрь ноября ноябре", "декабрь декабря декабре",
)
_MONTHS = {
    form: number
    for number, forms in enumerate(_MONTH_FORMS, 1)
    for form in forms.split()
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_YEAR = r"[12]\d{3}"
_DATE = rf"\d{{1,2}}\.\d{{1,2}}(?:\.{_YEAR})?"
_SEP = r"\s*(?:[-–—]|\bпо\b)\s*"


_YEAR_SUFFIX = rf"(?:\s+(?:(?P<y>{_YEAR})|(?P<relative>прошлого|прошедшего|предыдущего|следующего|будущего|этого|текущего)\s+года))?"
_ACTIVITY_CODE = re.compile(
    r"\b(?:оквэд(?:-?2)?|вид(?:а|у)?\s+деятельности)\s*[:№]?\s*\d{2}(?:\.\d{1,2})+\b"
)


_RULES = {
    "calendar_year": rf"(?:за|в)\s+(?P<y>{_YEAR})\s+год(?:у)?",
    "calendar_day": rf"(?P<d>\d{{1,2}})\s+(?P<m>{_MONTH}){_YEAR_SUFFIX}",
    "numeric": rf"(?:с\s+)?(?P<left>{_DATE}){_SEP}(?P<right>{_DATE})",
    "days": rf"(?:с\s+)?(?P<a>\d{{1,2}}){_SEP}(?P<b>\d{{1,2}})\s+(?P<m>{_MONTH}){_YEAR_SUFFIX}",
    "years": rf"(?P<a>{_YEAR}){_SEP}(?P<b>{_YEAR})",
    "quarter": rf"(?:за\s+|в\s+)?(?P<q>[1-4])(?:-й|-ой)?\s+квартал(?:е)?{_YEAR_SUFFIX}",
    "this_year": r"(?:в\s+этом|за\s+(?:этот|текущий))\s+год(?:у)?",
    "rolling": r"(?:за\s+)?последни(?:е|х|й)\s+(?P<n>\d+)\s+месяц(?:а|ев)?",
    "today": r"(?:на\s+)?сегодня",
    "month": rf"(?:в|за)\s+(?P<m>{_MONTH}){_YEAR_SUFFIX}",
}
_PATTERNS = {
    kind: re.compile(r"(?<!\w)(?:" + rule + r")(?!\w)")
    for kind, rule in _RULES.items()
}


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    as_of: bool = False

    def prompt_hint(self) -> str:
        if self.as_of:
            return (
                f"На дату {self.end.isoformat()}: для состояния выбери последний "
                "доступный срез на эту дату или ранее. Для событий за сегодня "
                "используй календарный день вопроса."
            )
        return f"Период вопроса: с {self.start.isoformat()} по {self.end.isoformat()} включительно."


def _bounds(kind: str, fields: dict, today: date) -> Period:
    year = int(fields.get("y") or today.year)
    relative = fields.get("relative")
    if relative in {"прошлого", "прошедшего", "предыдущего"}:
        year -= 1
    elif relative in {"следующего", "будущего"}:
        year += 1
    if kind == "calendar_year":
        return Period(date(year, 1, 1), date(year, 12, 31))
    if kind == "calendar_day":
        day = date(year, _MONTHS[fields["m"]], int(fields["d"]))
        return Period(day, day)
    if kind == "today":
        return Period(today, today, as_of=True)
    if kind == "numeric":
        left, right = (list(map(int, fields[key].split("."))) for key in ("left", "right"))
        left_year = left[2] if len(left) == 3 else right[2] if len(right) == 3 else year
        right_year = right[2] if len(right) == 3 else left_year
        return Period(date(left_year, left[1], left[0]), date(right_year, right[1], right[0]))
    if kind == "days":
        month = _MONTHS[fields["m"]]
        return Period(date(year, month, int(fields["a"])), date(year, month, int(fields["b"])))
    if kind in ("years", "this_year"):
        return Period(date(int(fields.get("a", year)), 1, 1), date(int(fields.get("b", year)), 12, 31))
    if kind == "rolling":
        count = int(fields["n"])
        if count < 1:
            raise ValueError("Период должен быть положительным")
        y, m = divmod(today.year * 12 + today.month - 1 - count, 12)
        return Period(date(y, m + 1, min(today.day, monthrange(y, m + 1)[1])), today)
    if kind == "quarter":
        first = 3 * int(fields["q"]) - 2
        last = first + 2
    else:
        first = last = _MONTHS[fields["m"]]
    return Period(date(year, first, 1), date(year, last, monthrange(year, last)[1]))


def parse_period(question: str, today: date) -> Period | None:
    text = _ACTIVITY_CODE.sub(" ", question.casefold())
    matches = [(kind, match) for kind, pattern in _PATTERNS.items() for match in pattern.finditer(text)]

    ranges = [match for kind, match in matches if kind == "days"]
    matches = [(kind, match) for kind, match in matches
               if kind != "calendar_day" or not any(
                   span.start() <= match.start() and match.end() <= span.end() for span in ranges)]
    
    if len(matches) != 1:
        return None
    kind, match = matches[0]
    remaining = text[:match.start()] + " " + text[match.end():]
    if re.search(rf"\b(?:{_MONTH}|{_YEAR}|вчера|завтра|сегодня)\b|\d{{1,2}}\.\d{{1,2}}", remaining):
        return None
    if re.search(r"\b(?:до|после|кроме|ранее|позже|раньше|позднее|не|с|по)\s*$", text[:match.start()]):
        return None
    try:
        period = _bounds(kind, match.groupdict(), today)
    except (ValueError, OverflowError):
        return None
    return period if period.start <= period.end else None
