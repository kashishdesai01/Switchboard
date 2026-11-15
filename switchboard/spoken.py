"""Parse and render dates and times for US clinic calls."""

import re
from calendar import month_abbr, month_name
from datetime import date, time

MONTHS = {
    label.casefold(): index
    for names in (month_name, month_abbr)
    for index, label in enumerate(names)
    if index
}
MONTH_PATTERN = "|".join(sorted(MONTHS, key=len, reverse=True))

DAY_ORDINALS = {
    word: day
    for day, word in enumerate(
        (
            "first",
            "second",
            "third",
            "fourth",
            "fifth",
            "sixth",
            "seventh",
            "eighth",
            "ninth",
            "tenth",
            "eleventh",
            "twelfth",
            "thirteenth",
            "fourteenth",
            "fifteenth",
            "sixteenth",
            "seventeenth",
            "eighteenth",
            "nineteenth",
            "twentieth",
            "twenty first",
            "twenty second",
            "twenty third",
            "twenty fourth",
            "twenty fifth",
            "twenty sixth",
            "twenty seventh",
            "twenty eighth",
            "twenty ninth",
            "thirtieth",
            "thirty first",
        ),
        start=1,
    )
}
DAY_ORDINAL_PATTERN = "|".join(
    word.replace(" ", r"[-\s]+") for word in sorted(DAY_ORDINALS, key=len, reverse=True)
)

YEAR_ONES = {
    word: number
    for number, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen".split()
    )
}
YEAR_TENS = dict(
    zip(
        "twenty thirty forty fifty sixty seventy eighty ninety".split(),
        range(20, 100, 10),
        strict=True,
    )
)
YEAR_TAIL = (
    rf"(?:oh (?:{'|'.join(list(YEAR_ONES)[1:10])})|{'|'.join(YEAR_ONES)}|"
    rf"(?:{'|'.join(YEAR_TENS)})(?:[ -](?:{'|'.join(list(YEAR_ONES)[1:10])}))?)"
)
SPOKEN_YEAR = re.compile(
    rf"\b(?:(?P<thousand>two thousand)(?: (?:and )?(?P<remainder>{YEAR_TAIL}))?"
    rf"|(?P<century>nineteen|twenty) (?P<tail>{YEAR_TAIL}|hundred))\b"
)


def _spoken_year(match: re.Match[str]) -> str:
    base = 2000 if match["thousand"] else 100 * YEAR_ONES.get(match["century"], 20)
    tail = match["remainder"] if match["thousand"] else match["tail"]
    if tail is None or tail == "hundred":
        return str(base)
    return str(
        base
        + sum(
            YEAR_ONES.get(word, YEAR_TENS.get(word, 0)) for word in tail.replace("-", " ").split()
        )
    )


def date_words(text: str) -> str:
    text = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", text.casefold())
    text = " ".join(re.sub(r"[.,]", " ", text).split())
    # Convert ordinal days first so “twenty sixth” cannot be mistaken for a year.
    text = re.sub(
        rf"\b(?:{DAY_ORDINAL_PATTERN})\b",
        lambda match: str(DAY_ORDINALS[" ".join(match[0].replace("-", " ").split())]),
        text,
    )
    text = re.sub(
        r"\b(19|20)[ -](\d{2})\b(?![-/]\d)",
        lambda match: "".join(match.groups()),
        text,
    )
    cardinal = rf"(?:{YEAR_TAIL}|thirty[ -]one)"
    year = rf"(?:\d{{4}}\b|{SPOKEN_YEAR.pattern})"
    text = re.sub(
        rf"\b(?P<month>{MONTH_PATTERN}) (?P<day>{cardinal})(?= {year})",
        lambda match: f"{match['month']} {_spoken_number(match['day'])}",
        text,
    )
    text = re.sub(
        rf"\b(?P<day>{cardinal}) (?:of )?(?P<month>{MONTH_PATTERN})(?= {year})",
        lambda match: f"{_spoken_number(match['day'])} {match['month']}",
        text,
    )
    return SPOKEN_YEAR.sub(_spoken_year, text)


def date_candidates(text: str, *, reference: date | None = None) -> tuple[date, ...]:
    words = date_words(text)
    month = rf"(?P<month>{MONTH_PATTERN})"
    year, day = r"(?P<year>\d{4})", r"(?P<day>\d{1,2})"
    patterns = [
        rf"{day}\s+(?:of\s+)?{month}\s+{year}",
        rf"{month}\s+(?:the\s+)?{day}\s+{year}",
        rf"{year}\s+{month}\s+{day}",
        rf"{year}\s+{day}\s+(?:of\s+)?{month}",
        rf"{year}[-/ ](?P<month>\d{{1,2}})[-/ ]{day}",
        rf"{year}\s+(?P<month>\d{{2}})(?P<day>\d{{2}})",
        rf"(?P<month>\d{{1,2}})/{day}/{year}",
    ]
    if reference:
        patterns.extend([rf"{month}\s+(?:the\s+)?{day}", rf"{day}\s+(?:of\s+)?{month}"])
    found = {}
    spans = []
    for pattern in patterns:
        for match in re.finditer(rf"\b{pattern}\b", words):
            start, end = match.span()
            if any(start < right and end > left for left, right in spans):
                continue
            spans.append((start, end))
            raw_month = match["month"]
            month_number = int(raw_month) if raw_month.isdecimal() else MONTHS[raw_month]
            day_number = int(match["day"])
            year_number = match.groupdict().get("year")
            if year_number is None:
                assert reference is not None
                year_number = reference.year + (
                    (month_number, day_number) < (reference.month, reference.day)
                )
            try:
                found[start] = date(int(year_number), month_number, day_number)
            except ValueError:
                continue
    return tuple(dict.fromkeys(found[key] for key in sorted(found)))


# Slash dates and spoken digits follow the clinic's US month/day order.
# A two-digit year denotes its most recent occurrence on or before today.
DIGIT_WORDS = "|".join(["oh", *list(YEAR_ONES)[:10]])
SPOKEN_BIRTH_DATE = re.compile(
    rf"\b(?P<month>(?:oh |zero )?(?:{DIGIT_WORDS}|ten|eleven|twelve)) "
    rf"(?P<day>(?:oh |zero )?(?:thirty[ -]one|(?:twenty|thirty)[ -](?:{DIGIT_WORDS})|{YEAR_TAIL})) "
    rf"(?P<year>\d{{4}}|{SPOKEN_YEAR.pattern}|(?:{DIGIT_WORDS}) (?:{DIGIT_WORDS})|{YEAR_TAIL})"
    rf"(?!\w|[- ](?:{DIGIT_WORDS})\b)"
)


def _spoken_number(value: str) -> int:
    if value.isdecimal():
        return int(value)
    parts = value.replace("-", " ").split()
    if len(parts) == 2 and all(part == "oh" or YEAR_ONES.get(part, 10) < 10 for part in parts):
        return 10 * YEAR_ONES.get(parts[0], 0) + YEAR_ONES.get(parts[1], 0)
    return sum(YEAR_ONES.get(part, YEAR_TENS.get(part, 0)) for part in parts)


def birth_dates(text: str, today: date | None = None) -> tuple[date, ...]:
    found = list(date_candidates(text))
    today = today or date.today()
    words = " ".join(re.sub(r"[.,]", " ", text.casefold()).split())
    for pattern in (
        re.compile(r"\b(?P<month>\d{1,2})\s*/\s*(?P<day>\d{1,2})\s*/\s*(?P<year>\d{2})\b"),
        re.compile(r"\b(?P<month>\d{1,2})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})\b"),
    ):
        for match in pattern.finditer(words):
            y = (
                int(_spoken_year(spoken_year))
                if (spoken_year := SPOKEN_YEAR.fullmatch(match["year"]))
                else _spoken_number(match["year"])
            )
            m, d = _spoken_number(match["month"]), _spoken_number(match["day"])
            if y < 100:
                y += today.year // 100 * 100
                if (y, m, d) > (today.year, today.month, today.day):
                    y -= 100
            try:
                found.append(date(y, m, d))
            except ValueError:
                pass
    # A named month anchors the date. A substring of its year can look
    # like an independent all-spoken month/day/year sequence.
    for clause in re.split(r"[;!?]|\bor\b", words):
        if re.search(rf"\b(?:{MONTH_PATTERN})\b", clause):
            continue
        for match in SPOKEN_BIRTH_DATE.finditer(clause):
            year = match["year"]
            spoken_year = SPOKEN_YEAR.fullmatch(year)
            y = int(_spoken_year(spoken_year)) if spoken_year else _spoken_number(year)
            m, d = _spoken_number(match["month"]), _spoken_number(match["day"])
            if y < 100:
                y += today.year // 100 * 100
                if (y, m, d) > (today.year, today.month, today.day):
                    y -= 100
            try:
                found.append(date(y, m, d))
            except ValueError:
                pass
    for match in re.finditer(r"\b(\d{2})[/.-](\d{1,2})[/.-](\d{4})\b", text):
        day, month, year = map(int, match.groups())
        if day > 12:
            try:
                found.append(date(year, month, day))
            except ValueError:
                pass
    return tuple(dict.fromkeys(found))


def spoken_time(value: time) -> str:
    if (value.hour, value.minute) == (12, 0):
        return "noon"
    if (value.hour, value.minute) == (0, 0):
        return "midnight"
    ones = list(YEAR_ONES)
    hour = ones[value.hour % 12 or 12]
    minutes = value.minute
    if not minutes:
        spoken_minutes = ""
    elif minutes < 10:
        spoken_minutes = f" oh {ones[minutes]}"
    elif minutes < 20:
        spoken_minutes = f" {ones[minutes]}"
    else:
        tens, units = divmod(minutes, 10)
        spoken_minutes = f" {list(YEAR_TENS)[tens - 2]}" + (f" {ones[units]}" if units else "")
    return f"{hour}{spoken_minutes} {'A M' if value.hour < 12 else 'P M'}"


def parse_date(text: str, *, reference: date | None = None) -> date | None:
    dates = date_candidates(text, reference=reference)
    return dates[0] if len(dates) == 1 else None


def requested_days(text: str, today: date) -> tuple[date, ...]:
    words = date_words(text)
    month = rf"(?P<month>{MONTH_PATTERN})"
    for pattern in (
        rf"(?P<first>\d{{1,2}}) (?:or|and) (?P<second>\d{{1,2}}) (?:of )?{month}",
        rf"{month} (?P<first>\d{{1,2}}) (?:or|and) (?P<second>\d{{1,2}})",
    ):
        match = re.search(rf"\b{pattern}(?: (?P<year>\d{{4}}))?\b", words)
        if match:
            suffix = f" {match['year']}" if match["year"] else ""
            return tuple(
                dict.fromkeys(
                    day
                    for key in ("first", "second")
                    if (
                        day := parse_date(f"{match['month']} {match[key]}{suffix}", reference=today)
                    )
                )
            )
    return date_candidates(words, reference=today)


def parse_time(text: str, *, clinic_hours: bool = False) -> time | None:
    match = re.search(r"\b(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?\s*m\b", text, re.I)
    if match:
        hour = int(match[1])
        if 1 <= hour <= 12:
            return time(hour % 12 + (12 if match[3].lower() == "p" else 0), int(match[2] or 0))
        return None
    match = re.search(r"\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if match:
        return time(int(match[1]), int(match[2]))
    if re.search(r"\b(?:noon|midday)\b", text):
        return time(12)
    if clinic_hours:
        spoken = dict(
            zip(
                (
                    "one",
                    "two",
                    "three",
                    "four",
                    "five",
                    "six",
                    "seven",
                    "eight",
                    "nine",
                    "ten",
                    "eleven",
                    "twelve",
                ),
                range(1, 13),
                strict=True,
            )
        )
        token = text.split(maxsplit=1)[0]
        hour = int(token) if token.isdecimal() and len(token) <= 2 else spoken.get(token)
        if hour is not None and 1 <= hour <= 12:
            return time(hour + 12 if hour <= 7 else hour)
    return None
