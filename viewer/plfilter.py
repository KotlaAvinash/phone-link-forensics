"""
plfilter.py - Excel-style sorting and filtering for the viewer's tables (no Tk; standard library only).

Everything here works on the RAW cell values (what the database copy holds); only `visible` changes text, and only
for display. A cell is blank when it is None or empty; blanks always sort last and are never dropped by a condition -
only by unticking "(Blanks)".

    detect_type(values, name)            -> TEXT | NUMBER | DATE
    sort_indices(idx, rows, col, type, descending)
    make_condition(type, op, a, b)       -> Condition (raises FilterError with a message for the examiner)
    ColumnFilter(type, values, blanks, cond).match(value) / .describe(col)
    filter_indices(rows, [(col, ColumnFilter)], text, cols) / shown_indices(...)
    distinct(values, type), Checklist(items, blanks, ticked, keep_blanks, all_keys), group_days(keys)
    preset_range(values, preset)         -> (from, to) relative to the NEWEST value, never to now
    visible(text)                        -> invisible and control characters shown as <U+200E>
"""
import calendar
import re
import unicodedata
from datetime import datetime, timedelta

TEXT, NUMBER, DATE = "text", "number", "date"
SHOWN_CAP = 10_000          # like Excel: the value list shows at most this many values; the search covers all
SAMPLE = 2_000              # type detection looks at this many non-blank cells, spread over the column
TIME_NAME = re.compile(r"(UTC|Local|Time)$")

OPS = {
    TEXT: ["contains", "does not contain", "equals", "begins with", "ends with"],
    NUMBER: ["equals", "does not equal", "greater than", "less than", "between"],
    DATE: ["on", "before", "after", "between"],
}
PRESETS = {"Last 24 h of data": timedelta(hours=24), "Last 7 days of data": timedelta(days=7),
           "Last 30 days of data": timedelta(days=30)}
DATE_HELP = "use YYYY-MM-DD or YYYY-MM-DD HH:MM[:SS]"


class FilterError(ValueError):
    """Input the examiner typed that cannot be used; the message says why."""


# ---------------------------------------------------------------- cells
def is_blank(v):
    """Blank = None or empty (text or bytes). A space or an invisible character is NOT blank."""
    return v is None or (isinstance(v, (str, bytes, bytearray)) and len(v) == 0)


def cell_text(v):
    """The raw text of a cell: '' for None, '<N bytes>' for binary, str() otherwise."""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    return v if isinstance(v, str) else str(v)


# Letters that render as nothing although Unicode calls them letters (Hangul fillers, Braille blank).
_BLANK_LETTERS = {0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800}
_BLANK_RE = re.compile("[ᅟᅠㅤﾠ⠀]")


def _marked(ch, keep_breaks):
    cp = ord(ch)
    if keep_breaks and ch in "\n\t":
        return False
    if cp in _BLANK_LETTERS:
        return True
    return unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp", "Co", "Cn", "Cs")


def visible(text, keep_breaks=False):
    """text with every invisible, format or control character written as <U+XXXX> (U+27E8/U+27E9 brackets), so a
    name that is only U+200E no longer looks blank. keep_breaks keeps line breaks and tabs (for the detail box)."""
    if not text or (text.isprintable() and not _BLANK_RE.search(text)):
        return text
    return "".join(f"⟨U+{ord(c):04X}⟩" if _marked(c, keep_breaks) else c for c in text)


# ---------------------------------------------------------------- numbers and times
_NUM = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[+-]?\.\d+")
_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?)?"
                   r"\s*(?:Z|UTC|[+-]\d{2}:?\d{2})?\s*(?:\([^()]*\))?")


def parse_number(v):
    """A number from a cell: ints, floats, and text such as '1,234', '-5', '3.14'. None when it is not one."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v and v not in (float("inf"), float("-inf")) else None
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not _NUM.fullmatch(s):
        return None
    return float(s.replace(",", ""))


def _time_parts(v):
    """(datetime as written, precision) from a time cell or typed input; precision is the unit of the last field
    given (timedelta), or None when it is not a time."""
    if not isinstance(v, str):
        return None, None
    m = _TIME.fullmatch(v.strip())
    if not m:
        return None, None
    y, mo, d, hh, mi, ss, frac = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0),
                      int((frac or "0")[:6].ljust(6, "0")))
    except ValueError:
        return None, None
    unit = timedelta(days=1) if hh is None else timedelta(minutes=1) if ss is None else \
        timedelta(seconds=1) if frac is None else timedelta(microseconds=1)
    return dt, unit


def parse_time(v):
    """A datetime from a time cell, AS WRITTEN: 'YYYY-MM-DD[ HH:MM[:SS[.fff]]]', a T separator, a trailing Z, UTC
    or +hh:mm offset, and a note in brackets such as '(no zone)'. The offset is NOT applied: a column holds one
    zone (its name says which), and the examiner compares what the cell shows. None when it is not a time."""
    return _time_parts(v)[0]


def parse_when(text):
    """(start, end) of what the examiner typed: a day covers the whole day, a minute the whole minute.
    end is exclusive. None when the text is not a date."""
    dt, unit = _time_parts(text)
    return None if dt is None else (dt, dt + unit)


def detect_type(values, name=""):
    """DATE when the non-blank cells (a spread sample) are times - at least 90%, or at least half for a column
    whose name ends in UTC, Local or Time; NUMBER when at least 90% are numbers; TEXT otherwise. An empty column
    is DATE when its name says it is a time."""
    vals = [v for v in values if not is_blank(v)]
    if len(vals) > SAMPLE:
        step = len(vals) / SAMPLE
        vals = [vals[int(i * step)] for i in range(SAMPLE)]
    named = bool(TIME_NAME.search(name or ""))
    if not vals:
        return DATE if named else TEXT
    times = sum(parse_time(v) is not None for v in vals)
    if times >= 0.9 * len(vals) or (named and times >= 0.5 * len(vals)):
        return DATE
    if sum(parse_number(v) is not None for v in vals) >= 0.9 * len(vals):
        return NUMBER
    return TEXT


def sort_labels(ctype):
    """The two sort commands in Excel's words for a column of this type."""
    return {NUMBER: ("Sort Smallest → Largest", "Sort Largest → Smallest"),
            DATE: ("Sort Oldest → Newest", "Sort Newest → Oldest")}.get(
        ctype, ("Sort A → Z", "Sort Z → A"))


# ---------------------------------------------------------------- sorting
_DIGITS = re.compile(r"(\d+)")


def natural_key(s):
    """Case-insensitive natural order: 'img2' before 'img10'. Text and number parts alternate, so they never mix."""
    parts = _DIGITS.split(s.casefold())
    return tuple(int(p) if i % 2 else p for i, p in enumerate(parts))


def sort_key(v, ctype):
    """Key of a NON-blank cell: typed values first in their own order, cells that are not of the column's type
    after them (in text order)."""
    if ctype == NUMBER:
        n = parse_number(v)
        if n is not None:
            return (0, n, ())
    elif ctype == DATE:
        t = parse_time(v)
        if t is not None:
            return (0, t, ())
    else:
        return (0, 0, natural_key(cell_text(v)))
    return (1, 0, natural_key(cell_text(v)))


def sort_indices(indices, rows, col, ctype, descending=False):
    """Row indices sorted on col by type; stable; blanks last in BOTH directions (in their existing order)."""
    filled, blank = [], []
    for i in indices:
        (blank if is_blank(rows[i].get(col)) else filled).append(i)
    if ctype == DATE or ctype == NUMBER:
        keys = {i: sort_key(rows[i].get(col), ctype) for i in filled}
        filled.sort(key=keys.__getitem__, reverse=descending)
    else:
        filled.sort(key=lambda i: natural_key(cell_text(rows[i].get(col))), reverse=descending)
    return filled + blank


# ---------------------------------------------------------------- conditions
class Condition:
    """One typed condition on a column. test(v) -> True / False, or None for a date condition on a cell that is
    not a time (undated: kept or dropped with the blanks, never decided by the condition)."""

    def __init__(self, ctype, op, a, b=""):
        self.ctype, self.op, self.a, self.b = ctype, op, a, b
        if ctype == TEXT:
            self._a = a.casefold()
        elif ctype == NUMBER:
            self._a, self._b = parse_number(a), parse_number(b) if b else None
        else:
            self._a, self._b = parse_when(a) if a else None, parse_when(b) if b else None

    def test(self, v):
        op = self.op
        if self.ctype == TEXT:
            s = cell_text(v).casefold()
            return {"contains": lambda: self._a in s, "does not contain": lambda: self._a not in s,
                    "equals": lambda: s == self._a, "begins with": lambda: s.startswith(self._a),
                    "ends with": lambda: s.endswith(self._a)}[op]()
        if self.ctype == NUMBER:
            n = parse_number(v)
            if n is None:
                return op == "does not equal"
            return {"equals": lambda: n == self._a, "does not equal": lambda: n != self._a,
                    "greater than": lambda: n > self._a, "less than": lambda: n < self._a,
                    "between": lambda: self._a <= n <= self._b}[op]()
        t = parse_time(v)
        if t is None:
            return None
        if op == "on":
            return self._a[0] <= t < self._a[1]
        if op == "before":
            return t < self._a[0]
        if op == "after":
            return t >= self._a[1]
        return (self._a is None or t >= self._a[0]) and (self._b is None or t < self._b[1])

    def describe(self, col):
        if self.ctype == TEXT:
            return f"{col} {self.op} '{visible(self.a)}'"
        if self.op == "between":
            if self.ctype == DATE and not self.b:
                return f"{col} on or after {self.a}"
            if self.ctype == DATE and not self.a:
                return f"{col} on or before {self.b}"
            return f"{col} between {self.a} and {self.b}"
        return f"{col} {self.op} {self.a}"


def make_condition(ctype, op, a, b=""):
    """A Condition from what the examiner typed; raises FilterError (with a plain message) on bad input."""
    a, b = (a or "").strip(), (b or "").strip()
    if op not in OPS.get(ctype, ()):
        raise FilterError(f"'{op}' is not a {ctype} condition")
    if ctype == TEXT:
        if not a:
            raise FilterError("type the text to look for")
    elif ctype == NUMBER:
        if parse_number(a) is None:
            raise FilterError(f"'{a}' is not a number" if a else "type a number")
        if op == "between":
            if parse_number(b) is None:
                raise FilterError(f"'{b}' is not a number" if b else "between needs two numbers")
            if parse_number(a) > parse_number(b):
                raise FilterError("the first number is larger than the second")
    else:
        if op == "between":
            if not a and not b:
                raise FilterError("give From, To or both")
        elif not a:
            raise FilterError(f"type a date ({DATE_HELP})")
        for x, what in ((a, "From" if op == "between" else "date"), (b, "To")):
            if x and parse_when(x) is None:
                raise FilterError(f"{what} '{x}' is not a date - {DATE_HELP}")
        if op == "between" and a and b and parse_when(a)[0] >= parse_when(b)[1]:
            raise FilterError("From is after To")
    return Condition(ctype, op, a, b if op == "between" else "")


def newest(values):
    """The newest time among values, or None."""
    best = None
    for v in values:
        t = parse_time(v)
        if t is not None and (best is None or t > best):
            best = t
    return best


def preset_range(values, preset):
    """(From, To) text for a quick choice such as 'Last 7 days of data': ending at the NEWEST value in the data
    (evidence is often old - never relative to now). None when there is no time in values."""
    top = newest(values)
    if top is None:
        return None
    top = top.replace(microsecond=0)
    fmt = "%Y-%m-%d %H:%M:%S"
    return (top - PRESETS[preset]).strftime(fmt), top.strftime(fmt)


# ---------------------------------------------------------------- column filters
def value_key(v, ctype):
    """The value-list entry a cell belongs to: its day ('YYYY-MM-DD') in a date column, else its raw text."""
    if ctype == DATE:
        t = parse_time(v)
        if t is not None:
            return t.strftime("%Y-%m-%d")
    return cell_text(v)


class ColumnFilter:
    """values: the ticked value-list entries (None = all); blanks: keep blank cells (and, for a date condition,
    cells that are not times); cond: a Condition or None. All parts must hold (AND)."""

    def __init__(self, ctype, values=None, blanks=True, cond=None):
        self.ctype = ctype
        self.values = None if values is None else frozenset(values)
        self.blanks = blanks
        self.cond = cond

    @property
    def active(self):
        return self.values is not None or not self.blanks or self.cond is not None

    def match(self, v):
        if is_blank(v):
            return self.blanks
        if self.values is not None and value_key(v, self.ctype) not in self.values:
            return False
        if self.cond is not None:
            r = self.cond.test(v)
            return self.blanks if r is None else r
        return True

    def describe(self, col):
        parts = []
        if self.cond is not None:
            parts.append(self.cond.describe(col))
        if self.values is not None:
            vals = sorted(self.values, key=natural_key)
            if len(vals) <= 5:
                shown = ", ".join(f"'{visible(x)}'" for x in vals) or "nothing"
                parts.append(f"{'' if parts else col + ' '}is one of {shown}")
            else:
                parts.append(f"{'' if parts else col + ' '}is one of {len(vals)} ticked values")
        text = " and ".join(parts)
        if self.cond is not None or not self.blanks:
            note = "blanks kept" if self.blanks else "blanks removed"
            text = f"{text} ({note})" if text else f"{col}: {note}"
        return text


def filter_indices(rows, checks, text="", cols=()):
    """Indices of rows that pass every (col, ColumnFilter) in checks (AND) and, when text is given, contain it
    (case-insensitive) in any of cols."""
    checks = [(c, f) for c, f in checks if f is not None and f.active]
    needle = text.strip().casefold() if text else ""
    out = []
    for i, r in enumerate(rows):
        if all(f.match(r.get(c)) for c, f in checks) and \
                (not needle or any(needle in cell_text(r.get(c)).casefold() for c in cols)):
            out.append(i)
    return out


def shown_indices(rows, cols, types, filters, sort=None, text="", extra=()):
    """The rows a table shows, in order: the extra filters (the case-wide date range) and the column filters
    (all AND), the text search, then the sort (col, descending)."""
    checks = list(extra) + [(c, f) for c, f in filters.items() if c in cols]
    idx = filter_indices(rows, checks, text, cols)
    if sort and sort[0] in cols:
        idx = sort_indices(idx, rows, sort[0], types.get(sort[0], TEXT), sort[1])
    return idx


def describe_all(checks, text="", shown=0, total=0, sort=None):
    """One status line: every active filter, the text search and the counts."""
    parts = [f.describe(c) for c, f in checks if f is not None and f.active]
    if text and text.strip():
        parts.append(f"text search '{visible(text.strip())}'")
    if not parts:
        return f"No filters — {total:,} rows"
    return "; ".join(parts) + f" — {shown:,} of {total:,} rows"


# ---------------------------------------------------------------- the value list
def distinct(values, ctype):
    """([(key, count)] in the column's sort order, number of blank cells)."""
    counts, blanks = {}, 0
    for v in values:
        if is_blank(v):
            blanks += 1
            continue
        k = value_key(v, ctype)
        counts[k] = counts.get(k, 0) + 1
    keys = list(counts)
    keys.sort(key=lambda k: sort_key(k, ctype))
    return [(k, counts[k]) for k in keys], blanks


class Checklist:
    """The ticks of a value list. Every value starts ticked. search() narrows what is shown and what Select All
    and OK act on; result() is (values or None, keep blanks).

    all_keys: every value of the whole column, when the list shows fewer (other filters hide some rows). A value
    that is not listed keeps the tick it had - ticked when the old filter (ticked) had it, or when there was no
    old filter - so reopening a filter and pressing OK never drops values another filter happens to hide.
    changed: True once a tick, Select All or a search was used."""

    def __init__(self, items, blanks=0, ticked=None, keep_blanks=True, all_keys=None):
        self.items = list(items)
        self.keys = [k for k, _ in self.items]
        self.counts = dict(self.items)
        self.blank_count = blanks
        listed = set(self.keys)
        self.all_keys = listed | set(all_keys or ())
        self.hidden = (self.all_keys - listed) if ticked is None else (set(ticked) & self.all_keys) - listed
        self.ticked = set(self.keys) if ticked is None else set(ticked) & listed
        self.blanks = keep_blanks
        self.query = ""
        self._matches = self.keys
        self.changed = False

    def search(self, text):
        self.query = (text or "").strip().casefold()
        self.changed = self.changed or bool(self.query)
        q = self.query
        self._matches = self.keys if not q else [k for k in self.keys if q in k.casefold() or
                                                 q in visible(k).casefold()]

    def matches(self):
        return self._matches

    def shown(self, cap=SHOWN_CAP):
        """(keys to list, capped?, number of matches)."""
        m = self._matches
        return m[:cap], len(m) > cap, len(m)

    def set(self, key, on):
        self.changed = True
        (self.ticked.add if on else self.ticked.discard)(key)

    def toggle(self, key):
        self.set(key, key not in self.ticked)

    def set_all(self, on):
        self.changed = True
        for k in self._matches:
            self.set(k, on)
        if not self.query:
            self.blanks = on

    def group_state(self, keys):
        """'on', 'off' or 'partial' for a group of keys (a year or month of a date list)."""
        n = sum(k in self.ticked for k in keys)
        return "on" if n == len(keys) else "off" if n == 0 else "partial"

    def all_state(self):
        keys = list(self._matches)
        states = [k in self.ticked for k in keys]
        if not self.query and self.blank_count:
            states.append(self.blanks)
        n = sum(states)
        return "on" if n == len(states) else "off" if n == 0 else "partial"

    def result(self):
        sel = self.ticked & set(self._matches) if self.query else set(self.ticked)
        sel |= self.hidden           # values not in the list keep their old tick
        values = None if sel >= self.all_keys else frozenset(sel)
        return values, self.blanks


def month_label(ym):
    return calendar.month_name[int(ym[5:7])]


def group_days(keys):
    """{'years': [(year, [(year-month, [day keys])])], 'other': [keys that are not days]} in the given order."""
    years, other = {}, []
    for k in keys:
        if len(k) == 10 and parse_when(k) is not None and k[4] == "-":
            years.setdefault(k[:4], {}).setdefault(k[:7], []).append(k)
        else:
            other.append(k)
    return {"years": [(y, list(ms.items())) for y, ms in years.items()], "other": other}
