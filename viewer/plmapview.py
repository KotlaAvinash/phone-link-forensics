"""
plmapview.py - Phone Link relations viewer.

Shows the SQLECmd map output for a Phone Link device folder, every table as an
entity list, and 25 relations between the tables. Select a row to see every row
it links to; double-click a link (or press Enter) to jump to that row in its own table.

Works on COPIES only (staged by plrelate / plparse, hash-verified, read-only).
Standard library only.

    python plmapview.py --input "<...\\LocalCache\\Indexed\\<GUID>\\System\\Database>"
    python plmapview.py --input "%LOCALAPPDATA%\\Packages\\Microsoft.YourPhone_8wekyb3d8bbwe"
    python plmapview.py --demo                  # synthetic data, nothing real
    python plmapview.py --demo --theme dark     # light, dark or system (follow Windows); Ctrl+Shift+L switches
"""
import argparse
import csv
import os
import re
import sys
import tempfile
import traceback

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont
except ImportError:  # pragma: no cover
    sys.exit("tkinter is not available in this Python")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plparse as P  # noqa: E402
import plconnect as C  # noqa: E402
import pldevices as D  # noqa: E402
import plfilter as F  # noqa: E402
import plrelate as R  # noqa: E402
import pltheme as T  # noqa: E402

# Colours and fonts live in pltheme, by role (light and dark). These names are the LIGHT values, kept for code that
# reads them; the window itself always uses the palette in force (Viewer.pal).
LIGHT = T.PALETTES["light"]
BG, PANEL, INK, MUTED = LIGHT["bg"], LIGHT["panel"], LIGHT["ink"], LIGHT["muted"]
KIND = {  # kind -> (heading, light colour (palette role "kind.<kind>"), plain meaning)
    "pc": ("Linked phones (this PC)", LIGHT["kind.pc"],
           "Every phone ever linked to this PC, from the package-level files"),
    "conn": ("Phone connections - USB cable and Bluetooth (this PC)", LIGHT["kind.conn"],
             "Read live, read-only, from THIS computer's registry, setup logs and event logs - not from the evidence "
             "folder"),
    "table": ("Table", LIGHT["kind.table"], "Rows of one table"),
    "in-table": ("Inside one table", LIGHT["kind.in-table"],
                 "Rows of the SAME table that belong together, or data packed inside a row"),
    "in-db": ("Between tables of one database", LIGHT["kind.in-db"],
              "Tables in the SAME database file joined by an ID column"),
    "check": ("Checks", LIGHT["kind.check"], "Rows whose partner is missing, or counts that disagree"),
    "cross": ("Across databases", LIGHT["kind.cross"],
              "Joins between DIFFERENT database files - inferences, hidden by default"),
    "map": ("SQLECmd map output", LIGHT["kind.map"], "Exactly what the SQLECmd maps return"),
    "provenance": ("Provenance", LIGHT["kind.provenance"], "Where the copies came from and their hashes"),
}
APP_TITLE = "Phone Link Relations Viewer"
THEME_LABELS = {"light": "Theme: Light", "dark": "Theme: Dark", "system": "Theme: Follow Windows"}
THEME_POLL_MS = 5000       # Follow Windows: how often the Windows app colour is read again
# ---------------------------------------------------------------- the view list (left column)
# Plain-English names for the view list. The technical name (view code, file, table) is shown in the header
# when a view is opened, so nothing is lost. Views not listed here fall back to a group chosen from their
# database and kind, under their engine title.
GROUPS = [  # id, heading, open when the window opens
    ("notifications", "Notifications", True),
    ("messages", "Text messages (SMS, MMS, chat)", False),
    ("calls", "Calls", False),
    ("contacts", "Contacts", False),
    ("photos", "Photos", False),
    ("apps", "Apps", False),
    ("browser", "Browser, handoff and settings", False),
    ("overview", "Overview and checks", False),
    ("linked", "Phones linked to this PC", False),
    ("conn", "USB and Bluetooth on this PC", False),
    ("advanced", "Advanced", False),
]
SUBGROUPS = {  # sub-heading inside a group; folded away until opened
    "together": "Rows that belong together",
    "links": "Links to other tables",
    "inferred": "Matched from other files (inferred)",
    "between": "How these connect",
    "maps": "SQLECmd map output",
    "hashes": "Copies and hashes",
}
NAV = {  # view -> (group, sub-heading or None, name in the list)
    # notifications.db
    "N_notifications": ("notifications", None, "Notifications (live and recovered)"),
    "N_messages": ("notifications", None, "Chat messages inside notifications"),
    "N_actions": ("notifications", None, "Buttons on notifications"),
    "N_toasts": ("notifications", None, "Windows pop-ups (toasts)"),
    "N_requests": ("notifications", None, "Requests sent from the phone"),
    "NR1": ("notifications", "together", "Same notification group"),
    "NR2": ("notifications", "together", "Same chat conversation"),
    "NR3": ("notifications", "together", "Earlier versions of a notification"),
    "NR5": ("notifications", "together", "Each notification and its messages"),
    "NR6": ("notifications", "together", "Each notification and its buttons"),
    "ND1": ("notifications", "links", "Notification \u2192 Windows pop-up"),
    "ND2": ("notifications", "links", "Phone request \u2192 notification"),
    "ND3": ("notifications", "links", "Deleted rows and their live copy"),
    # phone.db
    "E_threads": ("messages", None, "Conversations"),
    "E_sms": ("messages", None, "Text messages (SMS)"),
    "E_mms": ("messages", None, "Picture messages (MMS)"),
    "E_rcs": ("messages", None, "Chat conversations (RCS)"),
    "E_participants": ("messages", None, "People in conversations"),
    "E_sims": ("messages", None, "SIM cards"),
    "R03": ("messages", "links", "Conversation \u2192 its messages"),
    "R04": ("messages", "links", "Conversation \u2192 its people"),
    "R05": ("messages", "links", "Picture message \u2192 attachments"),
    "R06": ("messages", "links", "Chat \u2192 messages and files"),
    "R07": ("messages", "links", "Chat \u2192 matching SMS conversation"),
    "R08": ("messages", "links", "Pinned, muted or hidden conversations"),
    "R09": ("messages", "links", "Messages waiting to be sent"),
    "R10": ("messages", "links", "Text message \u2192 SIM card"),
    "R15": ("messages", "inferred", "Text message \u2192 contact"),
    "R16": ("messages", "inferred", "Conversation \u2192 contacts"),
    "R17": ("messages", "inferred", "Person in a conversation \u2192 contact"),
    # calling.db
    "E_calls": ("calls", None, "All calls"),
    "R13": ("calls", "inferred", "Call \u2192 contact"),
    "R14": ("calls", "inferred", "Call \u2192 SIM card"),
    # contacts.db
    "E_contacts": ("contacts", None, "All contacts"),
    "R01": ("contacts", "links", "Contact \u2192 numbers, emails and addresses"),
    "R02": ("contacts", "links", "Deleted contact details"),
    # photos.db
    "E_photos": ("photos", None, "All photos"),
    "R20": ("photos", "inferred", "Photos taken during a call"),
    # phoneapps.db
    "E_apps": ("apps", None, "Apps on the phone"),
    "E_recent": ("apps", None, "Recently used apps"),
    "E_pcapp": ("apps", None, "Matching apps on this PC"),
    "R11": ("apps", "links", "App \u2192 recent use and PC match"),
    "R19": ("apps", "inferred", "Notification, setting or web page \u2192 app"),
    # continuity.db, settings.db
    "E_browser": ("browser", None, "Web pages opened on the phone"),
    "E_handoff": ("browser", None, "Apps continued on the PC (handoff)"),
    "E_settings": ("browser", None, "Phone Link settings"),
    # across the whole phone
    "R22": ("overview", None, "Timeline \u2013 everything in time order"),
    "R18": ("overview", None, "People \u2013 calls and texts per person"),
    "R21": ("overview", None, "Things that happened close together"),
    "R24": ("overview", None, "Unknown numbers and quiet contacts"),
    "R23": ("overview", None, "Check: rows whose partner is missing"),
    "R25": ("overview", None, "Check: sync counters (content_sequence)"),
    "NR4": ("overview", None, "Check: notification columns vs stored data"),
    # this PC: linked phones
    "PC_devices": ("linked", None, "Phone pairings"),
    "PC_phones": ("linked", None, "Linked devices (phones and PCs)"),
    "PC_trust": ("linked", None, "Trusted connections"),
    "PC_streamed": ("linked", None, "Apps streamed to this PC"),
    "PR4": ("linked", None, "Pairing history"),
    "PR1": ("linked", "between", "Phone \u2192 trusted connection"),
    "PR2": ("linked", "between", "Pairing \u2192 streamed apps"),
    "PR3": ("linked", "between", "Phone \u2192 pairing folder"),
    # this PC: cable and Bluetooth
    "C_phones": ("conn", None, "Phones connected (summary)"),
    "C_usb": ("conn", None, "USB devices"),
    "C_wpd": ("conn", None, "Portable devices (MTP: phones, cameras, drives)"),
    "C_bt": ("conn", None, "Bluetooth devices"),
    "C_btsvc": ("conn", None, "Bluetooth services"),
    "C_btle": ("conn", None, "Bluetooth Low Energy devices"),
    "C_autoplay": ("conn", None, "AutoPlay devices"),
    "C_install": ("conn", None, "First driver install per device"),
    "C_events": ("conn", None, "Connection events"),
    "CR1": ("conn", None, "Same device seen in several places"),
    # advanced
    "P_copies": ("advanced", "hashes", "Evidence copies and hashes"),
    "PC_copies": ("advanced", "hashes", "Linked-phone copies and hashes"),
}
MAP_NAMES = {  # SQLECmd map BaseFileName -> name in the list
    "CallingDB_CallHistory": "Calls", "ContactsDB": "Contacts", "PhotosDB": "Photos",
    "PhotosDB_Thumbnails": "Photo thumbnails", "PhotosDB_MediaBlobs": "Photo data",
    "PhoneDB_SMSMessages": "Text messages", "PhoneDB_SubscriptionInfo": "SIM cards",
    "NotificationsDB": "Notifications", "PhoneAppsDB_InstalledApps": "Installed apps",
    "PhoneAppsDB_RecentApps": "Recent apps", "PhoneAppsDB_PCAppData": "Matching PC apps",
    "ContinuityDB_BrowserHistory": "Browser history", "ContinuityDB_ContinueAppContext": "Handoff",
    "DeviceDataDB_Wallpaper": "Wallpaper", "SettingsDB": "Settings",
}
GROUP_SHORT = {"messages": "Messages", "browser": "Browser", "overview": "Overview", "linked": "Linked phones",
               "conn": "USB / Bluetooth"}   # for the narrow "Found in" column
DB_GROUP = {"notifications.db": "notifications", "phone.db": "messages", "calling.db": "calls",
            "contacts.db": "contacts", "photos.db": "photos", "phoneapps.db": "apps",
            "continuity.db": "browser", "settings.db": "browser", "deviceData.db": "browser"}
KIND_SUB = {"in-table": "together", "in-db": "links", "cross": "inferred"}
BADGE = {"table": "DATA", "in-table": "ROWS THAT BELONG TOGETHER", "in-db": "LINKED TABLES",
         "cross": "INFERRED MATCH", "check": "CHECK", "map": "SQLECMD MAP", "provenance": "HASHES",
         "pc": "THIS PC", "conn": "THIS PC"}


def nav_place(v, window_min=None):
    """(group, sub-heading, name) for a view - its plain-English place in the view list."""
    if v.vid in NAV:
        group, sub, name = NAV[v.vid]
        if v.vid == "R21" and window_min:
            name = f"Things within {window_min} min of each other"
        return group, sub, name
    if v.kind == "map":
        base = v.vid[2:]
        return "advanced", "maps", MAP_NAMES.get(base, base.replace("_", " "))
    name = re.sub(r"^[A-Z]{1,3}\d+\s+", "", v.title)
    if v.kind == "provenance":
        return "advanced", "hashes", name
    if v.kind in ("pc", "conn"):
        return {"pc": "linked", "conn": "conn"}[v.kind], None, name
    if v.kind == "check":
        return "overview", None, "Check: " + name
    group = DB_GROUP.get(v.db or "", "overview")
    return group, KIND_SUB.get(v.kind) if group != "overview" else None, name


LINK_COLS = (("target", "Linked row", 3), ("where", "Found in", 2), ("how", "How it is linked", 3),
             ("scope", "Scope", 1))   # the linked-rows pane in display order: (column, heading, share of the spare width)
SCOPE_WORDS = {"in-table": "same table", "in-db": "other table, same database", "check": "check",
               "cross": "other database (inferred)"}
WIDE = {"Title", "Text", "BigText", "BigTitle", "SubText", "TickerText", "Message", "Body", "Row", "Notification",
        "Deleted", "LiveCopy", "Payload", "Key", "GroupKey", "What", "Flags", "First", "Second", "Value", "Check"}
# relation-table column -> entity kind, so rows without their own identity can still link
COL_ENTITY = {"CallID": "call", "ContactID": "contact", "ThreadID": "thread", "MediaID": "photo",
              "ParticipantID": "participant", "RCSThreadID": "rcsthread", "MMSID": "mms", "Package": "app"}
# MessageID means an SMS only in these relations (in R06 it is an RCS chat id)
VIEW_COL_ENTITY = {vid: {"MessageID": "sms"} for vid in ("R03", "R09", "R10", "R15")}


PHONE_LINK_PACKAGE = "Microsoft.YourPhone_8wekyb3d8bbwe"
NEARBY_MIN, NEARBY_MAX = 1, 1440     # the Nearby box, in minutes
DEMO_PATH = "(synthetic demo folder)"
WARN_COLOUR, STATUS_COLOUR = LIGHT["warn"], LIGHT["status"]     # light values; roles "warn" and "status"
# ---------------------------------------------------------------- sorting and filtering (logic in plfilter)
DROP_MARK, FILTER_MARK, SORT_UP, SORT_DOWN = "▾", " ●", "▲", "▼"  # heading marks: text, so they
CHECKED, UNCHECKED, PARTIAL = "☑", "☐", "▣"                             # follow the theme's colours
ERR_COLOUR = KIND["check"][1]
FILTER_DELAY_MS = 250      # the text filter waits this long after the last keystroke before it refills the table
NONE_OP = "(none)"
HINTS = {
    "nearby": "Minute window for “Events within N minutes of each other” (Things that happened close "
              "together): 1-1440, default 5. Changing it rebuilds the relations only - the copies are kept.",
    "cross": "Show the cross-database examiner joins: phone numbers matched on their last 10 digits, package names, "
             "and times. These are inferences, not stored links. With it off, the timeline and “People” views "
             "are hidden too.",
    "range": "Applies to every view's main time column (pick it under “Filter on”). Dates as YYYY-MM-DD or "
             "YYYY-MM-DD HH:MM[:SS]; To is inclusive. Quick ranges end at the NEWEST time in the case, not now.",
    "theme": "Light, dark, or follow the Windows app colour (checked every 5 seconds). Ctrl+Shift+L switches. "
             "The choice is remembered for next time (not in --demo or --screenshot runs).",
}


def startup_theme(cli, demo, screenshot):
    """(mode, settings file or None) at start-up: --theme, else the saved choice, else Follow Windows. A demo or
    screenshot run never reads or writes the settings file."""
    if demo or screenshot:
        return cli or "system", None
    path = T.settings_path()
    return cli or T.saved_mode(path) or "system", path


def nav_count_text(v, shown):
    """The Contents count for view v when `shown` of its rows pass the filters: '686', '112 of 686',
    '2 issues / 28' for a check view (v.problems), '69 of 91 linked' for a relation with unmatched rows (v.matched)."""
    total = len(v.rows)
    rows = f"{total:,}" if shown == total else f"{shown:,} of {total:,}"
    problems = getattr(v, "problems", None)
    if problems is not None:
        return f"{problems:,} issue{'' if problems == 1 else 's'} / {rows}"
    matched = getattr(v, "matched", None)
    if matched is not None:
        text = f"{matched:,} of {total:,} linked"
        return text if shown == total else f"{text} ({shown:,} shown)"
    return rows


def sort_words(ctype, descending):
    """'Newest → Oldest' and the like: the sort command without 'Sort '."""
    return F.sort_labels(ctype)[1 if descending else 0][len("Sort "):]


def write_text_atomic(path, text):
    """Write a small text file next to path and move it into place (like write_csv_atomic)."""
    fd, tmp = tempfile.mkstemp(prefix=".export-", suffix=".tmp", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", newline="\r\n", encoding="utf-8-sig") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def is_this_pc_package(path, local_appdata=None):
    """True when path is this PC's own Phone Link package folder, or inside it (not a look-alike prefix)."""
    base = local_appdata or os.path.expandvars("%LOCALAPPDATA%")
    pkg = os.path.normcase(os.path.abspath(os.path.join(base, "Packages", PHONE_LINK_PACKAGE)))
    here = os.path.normcase(os.path.abspath(os.path.expandvars(path)))
    try:
        return os.path.commonpath([here, pkg]) == pkg
    except ValueError:   # a different drive
        return False


def elide_middle(text, n):
    """text shortened to n characters by replacing its middle with an ellipsis (start and end stay readable)."""
    if len(text) <= n:
        return text
    keep = max(n - 1, 2)
    head = (keep + 1) // 2
    return text[:head] + "\u2026" + text[len(text) - (keep - head):]


# A cell starting with + or - stays as it is only when it is exactly a number: -5, -0.5, 3.14, +15550100.
PLAIN_NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?|\+[0-9]+")


def csv_cell(x):
    """One CSV cell that Excel will show as text: no formulas, no NULs, no raw bytes."""
    if x is None:
        return ""
    if isinstance(x, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(x))} bytes>"
    s = str(x).replace("\x00", "")
    if s[:1] in ("=", "+", "-", "@", "\t", "\r") and not PLAIN_NUMBER.fullmatch(s):
        return "'" + s
    return s


def write_csv_atomic(path, header, rows):
    """Write a CSV next to path and move it into place, so a failure never leaves a half-written or old-looking file."""
    fd, tmp = tempfile.mkstemp(prefix=".export-", suffix=".tmp", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([csv_cell(c) for c in header])
            for r in rows:
                w.writerow([csv_cell(c) for c in r])
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _dpi_aware():
    """Crisp text on scaled Windows displays, and window coordinates that match the screen (per monitor)."""
    T.dpi_aware()


class Viewer(tk.Tk):
    def __init__(self, devices, window_min=R.DEFAULT_WINDOW_MIN, initial=None, localcache=None, connections=None,
                 redact=False, this_pc=False, live_source=None, theme="light", theme_reader=None,
                 settings_path=None, title_bar=None):
        """this_pc: the examiner asked for this PC's live USB/Bluetooth views (--this-pc), so they stay whatever
        folder is opened later. live_source: makes the live source when a folder opened later is this PC's own
        Phone Link package (default: plconnect.LiveSource on Windows; tests pass a fake).
        theme: "light", "dark" or "system" (follow Windows, read through theme_reader - default the registry).
        settings_path: where a theme picked in the window is saved (None: never saved). title_bar(window, dark):
        colours the Windows title bar (default pltheme.set_title_bar)."""
        _dpi_aware()
        super().__init__()
        self.theme_reader = theme_reader
        self.settings_path = settings_path
        self.title_bar = title_bar or T.set_title_bar
        self.theme_mode = theme if theme in T.MODES else "light"
        self.theme_name = "light"
        self.pal = T.PALETTES["light"]
        self.themer = T.Themer(self.pal)
        self.fonts = T.make_fonts(self)       # kept: the named fonts live as long as these objects
        self._theme_job = self._title_job = self._nav_fit_job = None    # pending after() jobs
        self.nav_full = {}        # Contents item -> its full name (the list may show it shortened)
        self.devices = devices
        self.localcache = localcache
        self.redact = redact  # demo / screenshots: never show this computer's real paths
        self.connections = C.CachedSource(connections) if connections is not None else None
        self.this_pc = this_pc
        self.live_source = live_source if live_source is not None else (C.LiveSource if os.name == "nt" else None)
        self.conn_note = ""   # why the live USB/Bluetooth views are not shown, when they were dropped
        self.pc = None
        self.leftovers = []
        self._last_min = min(NEARBY_MAX, max(NEARBY_MIN, int(window_min)))   # last valid Nearby value
        self.built_min = self._last_min      # the window the current relations were built with
        self.window_min = tk.StringVar(self, value=str(self._last_min))   # text: the box can hold anything while typing
        self.ev = self.g = self.dev_idx = None
        self.src_full = ""
        self.show_cross = tk.BooleanVar(self, value=False)
        self.cur_vid = None
        self.cur_rows = []
        self.vstate = {}          # view -> {"sort": (column, descending) or None, "filters": {column: ColumnFilter}}
        self.focus_col = {}       # view -> the column Alt+Down opens (the last one clicked)
        self.panel = None         # the open heading filter panel
        self.range_filter = None  # the case-wide date range (a plfilter.ColumnFilter), or None
        self.range_cols = {}      # view -> the time column the case-wide range filters on, when not the default
        self._types = {}          # (view, column) -> (View, detected type)
        self._typing = False      # True while a keystroke in the filter box is being handled (debounced)
        self._filter_job = None
        self.filter_note = ""     # a one-off note for the filter line (a jump that cleared filters)
        self.hints = {}           # widget -> its hover hint
        self.title("Phone Link Relations Viewer")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1600, int(sw * 0.92)), min(960, int(sh * 0.88))
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
        self.basis_wrap = w - self.px(400)
        self._style()
        self._layout()
        self._layout_filters()
        self.apply_theme(self.theme_mode)
        self.bind("<Map>", lambda e: self._apply_title_bar() if e.widget is self else None, add="+")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.load_device(0, initial)

    # ------------------------------------------------------------ layout
    def px(self, n):
        """n pixels at 100 % display scaling, in this display's pixels."""
        return T.px(self, n)

    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview", font="PL.body")
        st.configure("Treeview.Heading", font="PL.bold")
        st.configure("Nav.Treeview", font="PL.body")
        self._row_heights()

    def _row_heights(self):
        """Treeview rows tall enough for their fonts at this display's scaling (a fixed 22 px clips at 150 %)."""
        st = ttk.Style(self)
        st.configure("Treeview", rowheight=T.row_height(self, "PL.body", "PL.bold"))
        st.configure("Nav.Treeview", rowheight=T.row_height(self, "PL.body", "PL.group", "PL.italic"))

    def refit_fonts(self):
        """The display scaling changed: measure the fonts again, then the rows and the Contents labels."""
        T.refit_fonts(self)
        self._row_heights()
        self._fit_count_col()
        self.fit_nav_labels()

    # ------------------------------------------------------------ theme
    def apply_theme(self, mode):
        """Colour the whole window for mode "light", "dark" or "system" (follow Windows): ttk styles, the Combobox
        lists, every classic widget (open filter panel included), the tags, the runtime colours (badge, basis
        line, status) and the title bar. Polls Windows every THEME_POLL_MS while following it."""
        self.theme_mode = mode if mode in T.MODES else "light"
        self.theme_name = T.resolve(self.theme_mode, self.theme_reader)
        self.pal = self.themer.pal = T.PALETTES[self.theme_name]
        T.style_ttk(self, self.pal)
        self._tag_colours()
        self.themer.paint(self)        # runtime colours are registered roles, so they follow too
        self.theme_combo.set(THEME_LABELS[self.theme_mode])
        if self.g is not None and self.cur_vid in self.g.views:
            self._filter_status()
        self._schedule_theme_poll()
        if self._title_job is None:
            self._title_job = self.after_idle(self._apply_title_bar)

    def set_theme_mode(self, mode):
        """A theme picked in the window (toolbar or Ctrl+Shift+L): apply it and remember it, when allowed."""
        self.apply_theme(mode)
        if self.settings_path:
            T.save_mode(self.theme_mode, self.settings_path)

    def cycle_theme(self):
        self.set_theme_mode(T.next_mode(self.theme_mode))
        return "break"

    def _theme_picked(self):
        mode = next((m for m, label in THEME_LABELS.items() if label == self.theme_combo.get()), "light")
        self.theme_combo.selection_clear()
        self.set_theme_mode(mode)

    def _cancel_job(self, attr):
        """Cancel the pending after() job kept in self.<attr> (a direct call must not leave the timer behind)."""
        job = getattr(self, attr, None)
        if job is not None:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
            setattr(self, attr, None)

    def _schedule_theme_poll(self):
        self._cancel_job("_theme_job")
        if self.theme_mode == "system":
            self._theme_job = self.after(THEME_POLL_MS, self._poll_system)

    def _poll_system(self):
        """Follow Windows: re-read the app colour; repaint only when it changed."""
        self._cancel_job("_theme_job")
        if self.theme_mode != "system":
            return
        if T.resolve("system", self.theme_reader) != self.theme_name:
            self.apply_theme("system")
        else:
            self._schedule_theme_poll()

    def _apply_title_bar(self):
        self._cancel_job("_title_job")
        try:
            if self.winfo_exists() and self.winfo_ismapped():
                self.title_bar(self, self.theme_name == "dark")
        except tk.TclError:
            pass

    def _tag_colours(self):
        p = self.pal
        self.nav.tag_configure("group", foreground=p["ink"], font="PL.group")
        self.nav.tag_configure("sub", foreground=p["muted"], font="PL.italic")
        self.nav.tag_configure("item", foreground=p["ink"])
        self.nav.tag_configure("inferred", foreground=p["kind.cross"])
        self.nav.tag_configure("empty", foreground=p["faint"])   # a view with no rows
        for k in KIND:
            self.links.tag_configure(k, foreground=p["kind." + k])
        self.detail.tag_configure("k", font="PL.bold", foreground=p["muted"])

    def _layout(self):
        role = self.themer.register       # colours by role (pltheme): the theme repaints them all
        bar = role(tk.Frame(self, height=52), zone="panel")
        bar.pack(fill="x")
        # the controls are packed first so they always get their space; the title is what yields on a narrow window
        self.theme_combo = ttk.Combobox(bar, state="readonly", width=21, values=list(THEME_LABELS.values()))
        self.theme_combo.bind("<<ComboboxSelected>>", lambda e: self._theme_picked())
        self.theme_combo.pack(side="right", padx=(4, 10))
        self.export_btn = role(tk.Button(bar, text="Export view (CSV)", command=self.export, relief="flat", padx=10),
                               background="accent", foreground="on_accent", activebackground="accent",
                               activeforeground="on_accent")
        self.export_btn.pack(side="right", padx=8)
        self.open_btn = tk.Button(bar, text="Open folder...", command=self.open_folder, relief="flat", padx=10)
        self.open_btn.pack(side="right", padx=4)
        self.cross_chk = tk.Checkbutton(bar, text="Show inferred matches", variable=self.show_cross,
                                        command=self.on_cross_toggle)
        self.cross_chk.pack(side="right", padx=12)
        # packed from the right: each control goes in BEFORE its label, so the label ends up on its left
        self.nearby_spin = tk.Spinbox(bar, from_=NEARBY_MIN, to=NEARBY_MAX, width=5, textvariable=self.window_min,
                                      command=self.rebuild)
        self.nearby_spin.bind("<Return>", lambda e: self.rebuild())
        self.nearby_spin.pack(side="right")
        self.nearby_lbl = tk.Label(bar, text="Nearby (min):")
        self.nearby_lbl.pack(side="right", padx=(12, 2))
        self.dev_combo = ttk.Combobox(bar, state="readonly", width=38,
                                      values=[f"{lbl}" for lbl, _ in self.devices])
        self.dev_combo.bind("<<ComboboxSelected>>", lambda e: self.load_device(self.dev_combo.current()))
        self.dev_lbl = tk.Label(bar, text="Device:")
        if len(self.devices) > 1:
            self.dev_combo.pack(side="right")
            self.dev_lbl.pack(side="right", padx=(12, 2))
        # the title takes what the controls leave (width=1: it never pushes them) and is shortened with "…" to fit
        self.app_title = role(tk.Label(bar, text=APP_TITLE, font="PL.title", anchor="w", width=1),
                              foreground="panel_title")
        self.app_title.pack(side="left", fill="x", expand=True, padx=14, pady=10)
        self.app_title.bind("<Configure>", lambda e: self.app_title.config(text=T.fit_text(
            APP_TITLE, tkfont.nametofont("PL.title", root=self).measure, max(1, e.width - 4))))
        # the source line has a row of its own, so a long path can never push a control off the toolbar
        srcrow = role(tk.Frame(self), zone="panel")
        srcrow.pack(fill="x")
        self.src_lbl = tk.Label(srcrow, text="", font="PL.body", anchor="w", width=1)
        self.src_lbl.pack(fill="x", padx=14, pady=(0, 6))
        self.src_lbl.bind("<Configure>", lambda e: self._fit_src())

        body = tk.PanedWindow(self, orient="horizontal", sashwidth=4)
        body.pack(fill="both", expand=True)
        left = role(tk.Frame(body), zone="field")
        body.add(left, minsize=self.px(260), width=self.px(430))
        role(tk.Label(left, text="CONTENTS", font="PL.smallbold"), foreground="muted").pack(anchor="w", padx=12,
                                                                                          pady=(10, 2))
        navbox = tk.Frame(left)
        navbox.pack(fill="both", expand=True, padx=6, pady=6)
        self.nav = ttk.Treeview(navbox, columns=("rows",), show="tree", style="Nav.Treeview", selectmode="browse")
        self.nav.column("#0", width=self.px(300), stretch=True)
        self.nav.column("rows", width=self.px(64), anchor="e", stretch=False)   # row count, right-aligned
        nav_scroll = ttk.Scrollbar(navbox, orient="vertical", command=self.nav.yview)
        self.nav.configure(yscrollcommand=nav_scroll.set)
        nav_scroll.pack(side="right", fill="y")
        self.nav.pack(side="left", fill="both", expand=True)
        self.nav.bind("<<TreeviewSelect>>", self.on_nav)
        for seq in ("<Configure>", "<Map>"):                 # names shortened to the width of the list ...
            self.nav.bind(seq, lambda e: self._fit_nav_soon(), add="+")
        self.nav_tip = TreeTip(self.nav, self.nav_tip_text)                     # ... and shown whole on hover

        right = tk.PanedWindow(body, orient="vertical", sashwidth=4)
        body.add(right)
        top = tk.Frame(right)
        right.add(top, minsize=self.px(300))
        head = tk.Frame(top)
        head.pack(fill="x", padx=12, pady=(10, 0))
        self.title_lbl = tk.Label(head, text="", font="PL.viewtitle", anchor="w")
        self.title_lbl.pack(side="left")
        self.kind_lbl = role(tk.Label(head, text="", font="PL.smallbold", padx=8),
                             background="kind.table", foreground="on_kind")
        self.kind_lbl.pack(side="left", padx=10)
        role(tk.Label(head, text="Filter:"), foreground="muted").pack(side="left", padx=(20, 4))
        self.filter_var = tk.StringVar(self)
        self._quiet = False      # True while show() clears the filter itself, so the table is filled once
        self.filter_var.trace_add("write", lambda *a: None if self._quiet else self._on_filter_text())
        self.filter_entry = tk.Entry(head, textvariable=self.filter_var, width=30)
        self.filter_entry.pack(side="left")
        self.count_lbl = role(tk.Label(head, text=""), foreground="muted")
        self.count_lbl.pack(side="left", padx=10)
        self.basis_lbl = tk.Label(top, text="", anchor="w", justify="left", wraplength=self.basis_wrap,
                                  font="PL.italic")
        self.basis_lbl.pack(fill="x", padx=12, pady=(4, 6))
        wrap = tk.Frame(top)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.table = ttk.Treeview(wrap, show="headings", selectmode="browse")
        ys = ttk.Scrollbar(wrap, orient="vertical", command=self.table.yview)
        xs = ttk.Scrollbar(wrap, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.table.bind("<<TreeviewSelect>>", self.on_row)

        bottom = tk.PanedWindow(right, orient="horizontal", sashwidth=4)
        right.add(bottom, minsize=self.px(180))
        dframe = tk.Frame(bottom)
        bottom.add(dframe, minsize=self.px(300))
        role(tk.Label(dframe, text="SELECTED ROW", font="PL.smallbold"), foreground="muted").pack(anchor="w", padx=12)
        self.detail = tk.Text(dframe, height=9, wrap="word", relief="flat", font="PL.body", padx=10, pady=6)
        self.detail.pack(fill="both", expand=True, padx=12, pady=(2, 8))
        self.detail.config(state="disabled")      # read-only, but the text can still be selected and copied
        self.detail.bind("<Button-1>", lambda e: self.detail.focus_set())   # a disabled Text does not take focus itself
        self.detail.bind("<Control-a>", lambda e: (self.detail.tag_add("sel", "1.0", "end"), "break")[1])
        lframe = tk.Frame(bottom)
        bottom.add(lframe, minsize=self.px(420))
        role(tk.Label(lframe, text="LINKED ROWS  (double-click or Enter to jump)", font="PL.smallbold"),
             foreground="muted").pack(anchor="w", padx=6)
        self.links = ttk.Treeview(lframe, columns=("how", "target", "where", "scope"), show="headings", height=8)
        for c, t, _ in LINK_COLS:
            self.links.heading(c, text=t)
            self.links.column(c, width=self.px(120), minwidth=self.px(60), anchor="w", stretch=c == "target")
        self.links["displaycolumns"] = tuple(c for c, _, _ in LINK_COLS)   # what it is and where, first
        self.links.bind("<Configure>", lambda e: self._fit_link_cols(e.width), add="+")
        links_x = ttk.Scrollbar(lframe, orient="horizontal", command=self.links.xview)
        self.links.configure(xscrollcommand=links_x.set)
        links_x.pack(side="bottom", fill="x", padx=6)
        self.links.pack(fill="both", expand=True, padx=6, pady=(2, 4))
        self.links.bind("<Double-1>", self.on_jump)
        self.links.bind("<Return>", self.on_jump)
        self.links.bind("<KP_Enter>", self.on_jump)
        self.link_targets = {}

        self.status = role(tk.Label(self, text="", anchor="w", font="PL.small", padx=12), zone="panel",
                           foreground="status")
        self.status.pack(fill="x", side="bottom")
        self._tag_colours()

    # ------------------------------------------------------------ data
    def nearby(self):
        """The Nearby box as a whole number of minutes, 1-1440. Empty or non-numeric text keeps the last valid value."""
        try:
            n = int(str(self.window_min.get()).strip())
        except (ValueError, tk.TclError):
            return self._last_min
        self._last_min = min(NEARBY_MAX, max(NEARBY_MIN, n))
        return self._last_min

    def _load(self, path, minutes, localcache, connections):
        """Copy, verify and relate one device. Returns (evidence, graph, linked-phones copy); nothing is kept on failure."""
        ev = R.Evidence(path)
        try:
            g = R.build(ev, minutes)
            pc = self._add_linked_phones(g, localcache, connections)
        except BaseException:
            self._remember(ev.close())
            raise
        return ev, g, pc

    def sources_for(self, path):
        """(LocalCache, live connections source, note) that belong to a newly opened input `path`. Nothing is
        committed here: load_device keeps them only if the input loads."""
        localcache = D.find_localcache(path)
        if self.this_pc and self.connections is not None:      # --this-pc: the examiner asked for this PC's views
            return localcache, self.connections, ""
        if is_this_pc_package(path):
            if self.connections is not None:
                return localcache, self.connections, ""
            src = self.live_source() if self.live_source else None
            return localcache, (C.CachedSource(src) if src is not None else None), ""
        note = ""
        if self.connections is not None or self.conn_note:
            note = ("USB/Bluetooth views hidden: the opened folder is not this PC's live Phone Link package; "
                    "use --this-pc to show this PC's connections anyway")
        return localcache, None, note

    def load_device(self, i, initial=None, devices=None, sources=None):
        """Load device i of self.devices (or of `devices`, which then replaces it). `sources` is the
        (LocalCache, connections, note) of a newly opened input; None keeps the current ones. A failure changes
        nothing: the previous device, its linked-phone and connection views stay, the picker goes back to it, and
        its copies are not touched."""
        pool = self.devices if devices is None else devices
        label, path = pool[i]
        localcache, connections, conn_note = sources or (self.localcache, self.connections, self.conn_note)
        self.config(cursor="watch")
        self.update_idletasks()
        minutes = self.nearby()
        try:
            ev, g, pc = self._load(path, minutes, localcache, connections)
        except Exception as e:  # report and keep the window usable
            self.config(cursor="")
            if self.g is not None and self.dev_idx is not None:
                self.dev_combo.current(self.dev_idx)
            kept = "\n\nThe previous device is still shown." if self.g is not None else ""
            messagebox.showerror("Could not load", self._scrub(f"{self._shown(path)}\n\n{self._why(e)}") + kept)
            return False
        self.config(cursor="")
        old = (self.ev, self.pc)      # the previous device copy AND the previous case's package copy
        self.ev, self.g, self.pc, self.built_min = ev, g, pc, minutes
        self.localcache, self.connections, self.conn_note = localcache, connections, conn_note
        for o in old:
            if o:
                self._remember(o.close())
        if devices is not None:
            self.devices = devices
            self.dev_combo["values"] = [lbl for lbl, _ in devices]
            self.cur_vid = None
        self.dev_idx = i
        self.close_panel()
        self.vstate, self.range_cols, self._types = {}, {}, {}   # filters belong to the data they were set on
        self.dev_combo.current(i)
        self._set_source(f"{label}  -  {self._shown(path)}")
        ok = sum(v["copy_matches"] == "yes" for v in self.ev.verify)
        na = sum(v["copy_matches"].startswith("n/a") for v in self.ev.verify)
        checked = f"{ok}/{len(self.ev.verify) - na} copies match their source SHA-256"
        if na:
            checked += f" ({na} not checkable: source changed during copy)"
        text = (f"Read-only COPIES in {self._shown(self.ev.work)}  |  {checked}  |  the source folder is never opened by SQLite  |  "
                f"{len(self.g.relations)} relations, "
                f"{sum(len(x) for x in self.g.edges.values()) // 2:,} links")
        stale = [] if self.redact else stale_copies(exclude=[self.ev.work] + ([self.pc.work] if self.pc else []))
        if stale:
            text += (f"  |  {len(stale)} leftover copy folder(s) from earlier runs in "
                     f"{self._shown(os.path.dirname(stale[0]))} - remove when done")
        if self.conn_note:
            text += f"  |  {self.conn_note}"
        self._status(text)
        self.fill_nav()
        self.show(initial or self.cur_vid or "N_notifications")
        return True

    def rebuild(self):
        if not self.ev or self.g is None:
            return
        minutes = self.nearby()
        self.window_min.set(str(minutes))      # show the value actually used
        if minutes == self.built_min:
            return
        g = R.build(self.ev, minutes)
        if self.pc is not None:      # keep the session's package copy: only the relations are rebuilt
            self._add_linked_phones(g, reuse=self.pc)
        else:
            self.pc = self._add_linked_phones(g)
        self.g, self.built_min = g, minutes
        self.close_panel()
        self.fill_nav()
        self.show(self.cur_vid)

    _CURRENT = object()

    def _add_linked_phones(self, g, localcache=_CURRENT, connections=_CURRENT, reuse=None):
        """Package-level views: every phone ever linked to this PC (copied and verified separately), from
        `localcache`, and the live USB/Bluetooth views from `connections` (both default to the current input's).
        Adds them to graph g and returns the copy (or None). `reuse`: an existing copy to build the views from
        again (a Nearby change) - nothing is copied or hashed again, and it is never closed here."""
        if localcache is self._CURRENT:
            localcache = self.localcache
        if connections is self._CURRENT:
            connections = self.connections
        pc = None
        try:
            if reuse is not None:
                pc = reuse
                D._build(reuse, g, R.View, R.ts_seconds, R.iso)
            else:
                _, pc = D.build(localcache, g, R.View, R.ts_seconds, R.iso)
        except (OSError, ValueError) as e:
            g.add_view(R.View("PC_devices", "Phone pairings (device folders)", "pc", f"could not read: {e}",
                              ["Error"], [{"Error": str(e)}]))
        if connections is not None:
            try:
                C.build(connections, g, R.View)
            except OSError as e:
                g.add_view(R.View("C_phones", "Phone connections", "conn", f"could not read: {e}", ["Error"],
                                  [{"Error": str(e)}]))
            except BaseException:
                if pc and pc is not reuse:
                    self._remember(pc.close())      # the package copy made above would otherwise be leaked
                raise
        return pc

    def _shown(self, path):
        return DEMO_PATH if self.redact else path

    def _why(self, e):
        """A short reason for an error, without the file name some exceptions carry."""
        return f"{type(e).__name__}: {e.strerror if isinstance(e, OSError) and e.strerror else e}"

    def _scrub(self, text):
        """Demo mode: replace this computer's folders (the copies, the temp folder, the sources) in text."""
        if not self.redact:
            return text
        known = [getattr(o, a, None) for o in (self.ev, self.pc) if o for a in ("src", "work")]
        known += [self.localcache, tempfile.gettempdir()] + [d for _, d in self.devices]
        paths = set()
        for k in known:
            if k:
                for f in (os.path.abspath, os.path.realpath):
                    paths.add(f(k))
        for k in sorted(paths, key=len, reverse=True):
            for form in (k, k.replace("\\", "/")):
                text = re.sub(re.escape(form), lambda m: DEMO_PATH, text, flags=re.IGNORECASE)
        return text

    def _status(self, text, warn=False):
        """The status bar. A snapshot warning from the evidence copy is always kept on it."""
        note = getattr(self.ev, "snapshot_note", "") if self.ev else ""
        if note:
            text += f"  |  WARNING: {note}"
        self.status.config(text=self._scrub(text))
        self.themer.set(self.status, foreground="warn" if warn or note else "status")

    def _set_source(self, text):
        self.src_full = self._scrub(text)
        self._fit_src()

    def _fit_src(self):
        """Show the source line shortened in the middle so it fits the window width."""
        w = self.src_lbl.winfo_width()
        font = tkfont.Font(font=self.src_lbl.cget("font"))
        chars = max(20, w // max(1, font.measure("0")) - 2) if w > 1 else 120
        text = elide_middle(self.src_full, chars)
        while w > 1 and chars > 20 and font.measure(text) > w:     # the "0" width is only an estimate: trim until it fits
            chars -= 1
            text = elide_middle(self.src_full, chars)
        self.src_lbl.config(text=text)

    def _remember(self, leftover):
        if leftover:
            self.leftovers.append(leftover)

    def fill_nav(self):
        """The view list: topic groups in plain English, row counts on the right, sub-headings folded away."""
        if self.g is None:
            return
        opened = {i for i in self._all_nav_ids() if self.nav.item(i, "open")} if self.nav.get_children() else None
        self.nav.delete(*self.nav.get_children())
        self.nav_full = {}
        shown = [v for v in self.g.views.values() if v.kind != "cross" or self.show_cross.get()]
        places = {v.vid: nav_place(v, self.built_min) for v in shown}
        rank = {vid: i for i, vid in enumerate(NAV)}
        built = {vid: i for i, vid in enumerate(self.g.views)}
        for gid, heading, open_default in GROUPS:
            views = [v for v in shown if places[v.vid][0] == gid]
            if not views:
                continue
            views.sort(key=lambda v: (rank.get(v.vid, len(rank)), built[v.vid]))   # NAV order, then build order
            node = f"grp:{gid}"
            self.nav.insert("", "end", iid=node, text=heading, values=("",), tags=("group",),
                            open=open_default if opened is None else node in opened)
            self.nav_full[node] = heading
            data, subs = [], {}
            for v in views:
                sub = places[v.vid][1]
                (subs.setdefault(sub, []) if sub else data).append(v)
            for v in data:     # the data first, then the folded sub-headings
                self._nav_item(node, v, places)
            for sub, members in sorted(subs.items(), key=lambda kv: list(SUBGROUPS).index(kv[0])
                                       if kv[0] in SUBGROUPS else len(SUBGROUPS)):
                sid = f"sub:{gid}:{sub}"
                self.nav.insert(node, "end", iid=sid, text=SUBGROUPS.get(sub, sub), values=("",), tags=("sub",),
                                open=False if opened is None else sid in opened)
                self.nav_full[sid] = SUBGROUPS.get(sub, sub)
                for v in members:
                    self._nav_item(sid, v, places)
        self._fit_count_col()
        self.fit_nav_labels()
        if self.cur_vid in self.g.views and self.nav.exists(self.cur_vid):
            self.nav.see(self.cur_vid)
            self.nav.selection_set(self.cur_vid)

    def _nav_item(self, parent, v, places):
        tags = ("empty",) if not v.rows else ("inferred",) if v.kind == "cross" else ("item",)
        self.nav.insert(parent, "end", iid=v.vid, text=places[v.vid][2], values=(self._nav_count(v),), tags=tags)
        self.nav_full[v.vid] = places[v.vid][2]

    # ---- Contents names that do not fit: shortened with an ellipsis, whole in a hover tip and above the table
    def nav_indent(self):
        """Pixels each Contents level is indented by (Tk 8.6: 20 px; Tk 9: 15 points)."""
        st = ttk.Style(self)
        v = st.lookup("Nav.Treeview", "indent") or st.lookup("Treeview", "indent")
        try:
            return int(round(self.winfo_fpixels(v))) if v else 20
        except tk.TclError:
            return 20

    def _fit_nav_soon(self):
        if self._nav_fit_job is None:
            self._nav_fit_job = self.after_idle(self.fit_nav_labels)

    def fit_nav_labels(self):
        """Shorten every Contents name to the width of its column, measured in its own font."""
        self._cancel_job("_nav_fit_job")
        # a hidden list has no real width yet: names stay whole until it is shown (<Map> / <Configure> refit them)
        width = int(self.nav.column("#0", "width")) if self.nav.winfo_ismapped() else 1 << 30
        indent = self.nav_indent()
        fonts = {"group": tkfont.nametofont("PL.group", root=self), "sub": tkfont.nametofont("PL.italic", root=self)}
        body = tkfont.nametofont("PL.body", root=self)
        for iid, full in list(self.nav_full.items()):
            if not self.nav.exists(iid):
                del self.nav_full[iid]
                continue
            depth, p = 0, self.nav.parent(iid)
            while p:
                depth, p = depth + 1, self.nav.parent(p)
            tags = self.nav.item(iid, "tags")
            font = fonts.get(tags[0], body) if tags else body
            # the text starts after the indent of its level and the open/close arrow (about one indent wide)
            text = T.fit_text(full, font.measure, width - (depth + 1) * indent - self.px(6))
            if self.nav.item(iid, "text") != text:
                self.nav.item(iid, text=text)

    def _fit_count_col(self):
        """The count column as wide as its widest count, so '2 issues / 28' is never cut."""
        font = tkfont.nametofont("PL.body", root=self)
        texts = [str(self.nav.set(i, "rows")) for i in self.nav_full if self.nav.exists(i)]
        width = max([self.px(64)] + [font.measure(t) + self.px(14) for t in texts])
        if int(self.nav.column("rows", "width")) != width:
            self.nav.column("rows", width=width)
            self._fit_nav_soon()

    def nav_tip_text(self, iid):
        """The full name of a Contents entry when the list shows it shortened, else None."""
        full = self.nav_full.get(iid)
        if full and self.nav.exists(iid) and self.nav.item(iid, "text") != full:
            return full
        return None

    def _all_nav_ids(self, parent=""):
        out = []
        for i in self.nav.get_children(parent):
            out += [i] + self._all_nav_ids(i)
        return out

    def friendly(self, vid):
        """'Group > name' for a view, as the view list shows it."""
        gid, _, name = nav_place(self.g.views[vid], self.built_min)
        return f"{GROUP_SHORT.get(gid) or dict((g, h) for g, h, _ in GROUPS).get(gid, gid)} \u203a {name}"

    # ------------------------------------------------------------ views
    def on_nav(self, _=None):
        sel = self.nav.selection()
        if self.g is not None and sel and not sel[0].startswith(("grp:", "sub:")) and sel[0] != self.cur_vid:
            self.show(sel[0], from_nav=True)

    def on_cross_toggle(self):
        """The 'Show inferred matches' box changed: refresh the view list and the linked rows, and leave an inferred
        view that is no longer allowed."""
        if self.g is None:
            return
        self.fill_nav()
        cur = self.g.views.get(self.cur_vid)
        if cur is not None and cur.kind == "cross" and not self.show_cross.get():
            self.show("N_notifications")        # falls back to the first view when there are no notifications
        elif self.table.selection():
            self.on_row()

    def show(self, vid, from_nav=False, select_index=None):
        if self.g is None:
            return
        if vid not in self.g.views:
            vid = next(iter(self.g.views))
        v = self.g.views[vid]
        self.cur_vid = vid
        if not from_nav and self.nav.exists(vid):
            self.nav.selection_set(vid)
            self.nav.see(vid)
        name, _, meaning = KIND[v.kind]
        kind_role = "kind." + v.kind if "kind." + v.kind in self.pal else "kind.table"
        self.title_lbl.config(text=nav_place(v, self.built_min)[2])     # the full name, however the list shows it
        self.kind_lbl.config(text=BADGE.get(v.kind, v.kind.upper()))
        self.themer.set(self.kind_lbl, background=kind_role, foreground="on_kind")
        where = f"{v.db}  -  " if v.db else ""
        self.basis_lbl.config(text=self._scrub(f"{where}{meaning}.   Source: {v.basis}.   Technical name: {v.vid} "
                                               f"\u2013 {v.title}"))
        self.themer.set(self.basis_lbl, foreground=kind_role)
        self.table["columns"] = v.cols
        for c in v.cols:
            self.table.heading(c, text=c, command=lambda c=c: self.open_filter(c))
            self.table.column(c, width=self.px(260 if c in WIDE else max(80, min(220, 9 * len(c) + 40))), anchor="w",
                              stretch=False)
        if select_index is not None and self.filter_var.get():
            self._quiet = True
            try:
                self.filter_var.set("")
            finally:
                self._quiet = False
        self.close_panel()
        self.filter_note = ""
        if select_index is not None:
            self._reveal(vid, select_index)
        self._update_range_picker()
        self.populate(select_index)

    def populate(self, select_index=None):
        if not self.g or self.cur_vid not in self.g.views:
            return
        self._cancel_filter_job()
        v = self.g.views[self.cur_vid]
        self.table.delete(*self.table.get_children())
        # column filters, the case-wide dates and the text box, then the sort: cur_rows is the display order
        self.cur_rows = self.view_indices(self.cur_vid, self.filter_var.get())
        vis, text = F.visible, F.cell_text
        for i in self.cur_rows:
            r = v.rows[i]
            self.table.insert("", "end", iid=str(i), values=[vis(text(r.get(c))) for c in v.cols])
        self.count_lbl.config(text=f"{len(self.cur_rows):,} of {len(v.rows):,} rows")
        self._headings()
        self._filter_status()
        self._detail_set()
        self.links.delete(*self.links.get_children())
        if select_index is not None and str(select_index) in self.table.get_children():
            self.table.selection_set(str(select_index))
            self.table.see(str(select_index))
            self.table.focus(str(select_index))

    def sort_by(self, col):
        # sort the row list and refill: moving 30,000 Tk items one by one took about 6 s, and keeping
        # cur_rows in display order makes Export write what the screen shows
        # the sort is kept per view and applied after the filters on every refill, so filtering never loses it
        if self.g is None or self.cur_vid not in self.g.views:
            return
        current = self.view_state(self.cur_vid)["sort"]
        self.set_sort(col, current == (col, False))      # first click ascending, the next one descending

    # ------------------------------------------------------------ sorting and filtering (Excel-style AutoFilter)
    def _layout_filters(self):
        """The case-wide date range (a second toolbar row), the filter line above the table, the keyboard
        shortcuts and the hover hints. Built after _layout, from new widgets only."""
        role = self.themer.register
        row = role(tk.Frame(self), zone="panel")
        row.pack(fill="x", after=self.export_btn.master)
        self.rangebar = row
        lab = dict(font="PL.body")
        pad = dict(pady=(0, 6))
        self.clear_btn = tk.Button(row, text="Clear all filters", command=self.clear_all_filters, relief="flat",
                                   padx=10)
        self.clear_btn.pack(side="right", padx=8, **pad)
        role(tk.Label(row, text="Dates in every view", font="PL.bold"),
             foreground="panel_title").pack(side="left", padx=(14, 4), **pad)
        self.range_from, self.range_to = tk.StringVar(self), tk.StringVar(self)
        self.range_keep = tk.BooleanVar(self, value=True)
        # packed from the left: each label goes in before its control, so it sits on the control's left
        self.from_lbl = tk.Label(row, text="From:", **lab)
        self.from_lbl.pack(side="left", padx=(12, 2), **pad)
        self.from_entry = tk.Entry(row, textvariable=self.range_from, width=19)
        self.from_entry.pack(side="left", **pad)
        self.to_lbl = tk.Label(row, text="To:", **lab)
        self.to_lbl.pack(side="left", padx=(12, 2), **pad)
        self.to_entry = tk.Entry(row, textvariable=self.range_to, width=19)
        self.to_entry.pack(side="left", **pad)
        for e in (self.from_entry, self.to_entry):
            e.bind("<Return>", lambda ev: self.apply_range())
            e.bind("<KP_Enter>", lambda ev: self.apply_range())
        self.range_apply_btn = role(tk.Button(row, text="Apply", command=self.apply_range, relief="flat", padx=8),
                                    background="accent", foreground="on_accent", activebackground="accent",
                                    activeforeground="on_accent")
        self.range_apply_btn.pack(side="left", padx=(8, 2), **pad)
        self.preset_combo = ttk.Combobox(row, state="readonly", width=20, values=list(F.PRESETS))
        self.preset_combo.set("Quick range…")
        self.preset_combo.bind("<<ComboboxSelected>>", lambda e: self.range_preset(self.preset_combo.get()))
        self.preset_combo.pack(side="left", padx=(6, 2), **pad)
        self.range_clear_btn = tk.Button(row, text="Clear dates", command=self.clear_range, relief="flat", padx=8)
        self.range_clear_btn.pack(side="left", padx=(6, 2), **pad)
        self.keep_chk = tk.Checkbutton(row, text="Keep undated rows", variable=self.range_keep,
                                       command=self._keep_changed)
        self.keep_chk.pack(side="left", padx=(8, 2), **pad)
        self.on_lbl = tk.Label(row, text="Filter on:", **lab)
        self.on_lbl.pack(side="left", padx=(12, 2), **pad)
        self.range_col_combo = ttk.Combobox(row, state="disabled", width=22)
        self.range_col_combo.bind("<<ComboboxSelected>>", lambda e: self.on_range_col())
        self.range_col_combo.pack(side="left", **pad)
        self.range_err = role(tk.Label(row, text="", font="PL.body", anchor="w"), foreground="warn")
        self.range_err.pack(side="left", padx=(10, 2), **pad)
        for w in (self.from_lbl, self.from_entry, self.to_lbl, self.to_entry, self.preset_combo, self.keep_chk,
                  self.on_lbl, self.range_col_combo):
            self._hint(w, HINTS["range"])
        self._hint(self.nearby_spin, HINTS["nearby"])
        self._hint(self.nearby_lbl, HINTS["nearby"])
        self._hint(self.cross_chk, HINTS["cross"])
        self._hint(self.theme_combo, HINTS["theme"])
        self._hint(self.clear_btn, "Clear every column filter in every view, the case-wide dates and the text "
                                   "filter (Ctrl+Shift+F). Sorting stays.")
        # the filter line: what is filtered and sorted in this view, and the counts
        self.filter_lbl = tk.Label(self.table.master.master, text="", anchor="w", justify="left",
                                   wraplength=self.basis_wrap, font="PL.body")
        self.filter_lbl.pack(fill="x", padx=12, pady=(0, 4), before=self.table.master)
        self.nav.column("#0", width=self.px(230))      # the names stretch into what is left after the counts
        for seq in ("<Control-L>", "<Control-Shift-L>"):   # light -> dark -> follow Windows -> light
            self.bind(seq, lambda e: self.cycle_theme())
        # keyboard
        self.filter_entry.bind("<Key>", self._key_typed)
        self.bind("<Control-f>", lambda e: self._focus_filter())
        self.bind("<Control-e>", lambda e: (self.export(), "break")[1])
        for seq in ("<Control-F>", "<Control-Shift-F>"):
            self.bind(seq, lambda e: (self.clear_all_filters(), "break")[1])
        self.bind("<F6>", lambda e: self._cycle_focus(1))
        self.bind("<Shift-F6>", lambda e: self._cycle_focus(-1))
        for seq in ("<Alt-Down>", "<Shift-F10>", "<App>", "<Menu>"):
            try:
                self.table.bind(seq, lambda e: self._open_focused())
            except tk.TclError:      # a key this platform does not have
                pass
        self.table.bind("<Button-1>", self._note_column, add="+")
        self.table.bind("<Button-3>", self._heading_click)
        self.bind("<Button-1>", self._click_outside, add="+")
        self.detail.configure(takefocus=1)       # the read-only box is reachable with Tab and F6
        self.detail.bind("<Tab>", lambda e: (self.detail.tk_focusNext().focus_set(), "break")[1])
        for seq in ("<Shift-Tab>", "<ISO_Left_Tab>"):
            try:
                self.detail.bind(seq, lambda e: (self.detail.tk_focusPrev().focus_set(), "break")[1])
            except tk.TclError:
                pass

    def _hint(self, widget, text):
        self.hints[widget] = text
        Hint(widget, text)

    def view_state(self, vid):
        return self.vstate.setdefault(vid, {"sort": None, "filters": {}})

    def col_type(self, col, vid=None):
        """TEXT, NUMBER or DATE for a column of a view, detected once per view build."""
        vid = vid or self.cur_vid
        v = self.g.views[vid]
        hit = self._types.get((vid, col))
        if hit is not None and hit[0] is v:
            return hit[1]
        t = F.detect_type([r.get(col) for r in v.rows], col)
        self._types[(vid, col)] = (v, t)
        return t

    def date_cols(self, vid):
        if self.g is None or vid not in self.g.views:
            return []
        return [c for c in self.g.views[vid].cols if self.col_type(c, vid) == F.DATE]

    def main_time_col(self, vid):
        """The column the case-wide dates filter on: the one picked under 'Filter on', else the first time column
        that holds any value, else the first time column; None when the view has no time column."""
        dates = self.date_cols(vid)
        pick = self.range_cols.get(vid)
        if pick in dates:
            return pick
        rows = self.g.views[vid].rows if dates else []
        for c in dates:
            if any(not F.is_blank(r.get(c)) for r in rows):
                return c
        return dates[0] if dates else None

    def _range_checks(self, vid):
        col = self.main_time_col(vid) if self.range_filter is not None else None
        return [(col, self.range_filter)] if col else []

    def view_indices(self, vid, text="", sort=True):
        """The rows view vid shows (indices, in display order): case-wide dates, column filters, text, sort."""
        v = self.g.views[vid]
        st = self.view_state(vid)
        srt = st["sort"] if sort and st["sort"] and st["sort"][0] in v.cols else None
        types = {srt[0]: self.col_type(srt[0], vid)} if srt else {}
        return F.shown_indices(v.rows, v.cols, types, st["filters"], srt, text, self._range_checks(vid))

    def _filtered(self, vid):
        return bool(self._range_checks(vid)) or any(f.active for f in self.view_state(vid)["filters"].values())

    def _nav_count(self, v):
        shown = len(self.view_indices(v.vid, "", sort=False)) if self._filtered(v.vid) else len(v.rows)
        return nav_count_text(v, shown)

    def _refresh_counts(self):
        if self.g is None:
            return
        for vid, v in self.g.views.items():
            if self.nav.exists(vid):
                self.nav.set(vid, "rows", self._nav_count(v))
        self._fit_count_col()

    def heading_text(self, col, vid=None):
        """'Name ▾', 'StartUTC ▼ ▾ ●' (sorted newest first, filtered)."""
        vid = vid or self.cur_vid
        st = self.view_state(vid)
        text = col
        if st["sort"] and st["sort"][0] == col:
            text += " " + (SORT_DOWN if st["sort"][1] else SORT_UP)
        text += " " + DROP_MARK
        f = st["filters"].get(col)
        if (f is not None and f.active) or any(c == col for c, _ in self._range_checks(vid)):
            text += FILTER_MARK
        return text

    def _headings(self):
        for c in self.g.views[self.cur_vid].cols:
            self.table.heading(c, text=self.heading_text(c))

    def _filter_status(self):
        """The filter line: every filter in force on this view, the counts, the sort."""
        v = self.g.views[self.cur_vid]
        st = self.view_state(self.cur_vid)
        checks = [(f"All views: {c}", f) for c, f in self._range_checks(self.cur_vid)] + list(st["filters"].items())
        text = F.describe_all(checks, self.filter_var.get(), len(self.cur_rows), len(v.rows))
        if st["sort"] and st["sort"][0] in v.cols:
            col, desc = st["sort"]
            text += f"   |   sorted by {col}, {sort_words(self.col_type(col), desc)} (blanks last)"
        if self.filter_note:
            text += f"   |   {self.filter_note}"
        self.filter_lbl.config(text=text)

    def set_sort(self, col, descending):
        if self.g is None or self.cur_vid not in self.g.views:
            return
        self.view_state(self.cur_vid)["sort"] = (col, bool(descending))
        sel = self.table.selection()
        self.populate(int(sel[0]) if sel else None)

    def apply_column_filter(self, vid, col, cf):
        """Set (or, with None or an inactive filter, remove) the filter on one column of a view, and refill."""
        filters = self.view_state(vid)["filters"]
        if cf is None or not cf.active:
            filters.pop(col, None)
        else:
            filters[col] = cf
        self.filter_note = ""
        self._filters_changed()

    def _filters_changed(self):
        if self.g is None:
            return
        sel = self.table.selection()
        self.populate(int(sel[0]) if sel else None)
        self._refresh_counts()

    def clear_all_filters(self):
        """Every column filter in every view, the case-wide dates and the text filter. The sort stays."""
        self.close_panel()
        for st in self.vstate.values():
            st["filters"] = {}
        self.range_filter = None
        self.range_from.set("")
        self.range_to.set("")
        self.range_err.config(text="")
        self._quiet = True
        try:
            self.filter_var.set("")
        finally:
            self._quiet = False
        self.filter_note = ""
        self._filters_changed()

    def _reveal(self, vid, idx):
        """A jump to a row the filters hide: clear that view's column filters so the row can be shown."""
        if idx in set(self.view_indices(vid, "", sort=False)):
            return
        st = self.view_state(vid)
        if any(f.active for f in st["filters"].values()):
            st["filters"] = {}
            self.filter_note = "the filters on this view were cleared to show the linked row"
            self._refresh_counts()
        if idx not in set(self.view_indices(vid, "", sort=False)):
            self.filter_note = "the linked row is outside the case-wide dates - use Clear dates to see it"

    # ---- the heading panel
    def open_filter(self, col):
        """Open the AutoFilter panel of a column of the current view (the heading click)."""
        if self.g is None or self.cur_vid not in self.g.views or col not in self.g.views[self.cur_vid].cols:
            return None
        self.close_panel()
        self.focus_col[self.cur_vid] = col
        self.panel = FilterPanel(self, self.cur_vid, col)
        return self.panel

    def close_panel(self):
        p, self.panel = self.panel, None
        if p is not None:
            try:
                p.destroy()
                self.table.focus_set()
            except tk.TclError:
                pass

    def _column_at(self, x):
        if self.g is None or self.cur_vid not in self.g.views:
            return None
        n = self.table.identify_column(x)            # '#3'
        cols = self.g.views[self.cur_vid].cols
        try:
            i = int(n.lstrip("#")) - 1
        except ValueError:
            return None
        return cols[i] if 0 <= i < len(cols) else None

    def _note_column(self, e):
        col = self._column_at(e.x)
        if col:
            self.focus_col[self.cur_vid] = col

    def _heading_click(self, e):
        if self.table.identify_region(e.x, e.y) == "heading":
            col = self._column_at(e.x)
            if col:
                self.open_filter(col)
                return "break"

    def _open_focused(self):
        if self.g is None or self.cur_vid not in self.g.views:
            return "break"
        cols = self.g.views[self.cur_vid].cols
        col = self.focus_col.get(self.cur_vid)
        if cols:
            self.open_filter(col if col in cols else cols[0])
        return "break"

    def _click_outside(self, e):
        """A click anywhere outside the open panel cancels it, as in Excel."""
        if self.panel is not None and not str(e.widget).startswith(str(self.panel)):
            self.close_panel()

    # ---- the text box
    def _key_typed(self, _=None):
        self._typing = True               # the keystroke's change to the box is debounced ...
        self.after_idle(lambda: setattr(self, "_typing", False))

    def _on_filter_text(self):
        if self._typing:                  # ... a change made in code (or a paste from the menu) refills at once
            self._typing = False
            self._cancel_filter_job()
            self._filter_job = self.after(FILTER_DELAY_MS, self._filter_due)
        else:
            self.populate()

    def _filter_due(self):
        self._filter_job = None
        self.populate()

    def _cancel_filter_job(self):
        if self._filter_job is not None:
            try:
                self.after_cancel(self._filter_job)
            except tk.TclError:
                pass
            self._filter_job = None

    def _focus_filter(self):
        self.filter_entry.focus_set()
        self.filter_entry.select_range(0, "end")
        return "break"

    def _cycle_focus(self, step):
        """F6: Contents -> table -> linked rows -> selected-row box -> Contents."""
        order = [self.nav, self.table, self.links, self.detail]
        cur = self.focus_get()
        i = next((n for n, w in enumerate(order) if w is cur), -1 if step > 0 else 0)
        nxt = order[(i + step) % len(order)]
        nxt.focus_set()
        if isinstance(nxt, ttk.Treeview) and not nxt.focus() and nxt.get_children():
            nxt.focus(nxt.get_children()[0])
        return "break"

    # ---- the case-wide date range
    def _update_range_picker(self):
        dates = self.date_cols(self.cur_vid)
        main = self.main_time_col(self.cur_vid) if dates else None
        self.range_col_combo.configure(values=dates, state="readonly" if len(dates) > 1 else "disabled")
        self.range_col_combo.set(main or "(no time column)")

    def on_range_col(self):
        col = self.range_col_combo.get()
        if self.g is None or col not in self.date_cols(self.cur_vid):
            return
        self.range_cols[self.cur_vid] = col
        self._filters_changed()

    def apply_range(self):
        """Apply From / To to every view's main time column. Bad input is shown and changes nothing."""
        frm, to = self.range_from.get().strip(), self.range_to.get().strip()
        if not frm and not to:
            self.range_filter = None
        else:
            try:
                cond = F.make_condition(F.DATE, "between", frm, to)
            except F.FilterError as e:
                self.range_err.config(text=f"Dates not applied: {e}")
                return False
            self.range_filter = F.ColumnFilter(F.DATE, cond=cond, blanks=bool(self.range_keep.get()))
        self.range_err.config(text="")
        self.filter_note = ""
        self._filters_changed()
        return True

    def _keep_changed(self):
        if self.range_filter is not None:
            self.apply_range()

    def clear_range(self):
        self.range_from.set("")
        self.range_to.set("")
        self.range_filter = None
        self.range_err.config(text="")
        self._filters_changed()

    def range_preset(self, name):
        """A quick range ending at the NEWEST time in any view's main time column (never at now)."""
        self.preset_combo.set("Quick range…")
        if self.g is None:
            return False
        values = (r.get(c) for vid, v in self.g.views.items() for c in [self.main_time_col(vid)] if c
                  for r in v.rows)
        rng = F.preset_range(values, name)
        if rng is None:
            self.range_err.config(text="No times in the case to count back from")
            return False
        self.range_from.set(rng[0])
        self.range_to.set(rng[1])
        return self.apply_range()

    def export_record(self, path):
        """The text of the sidecar <file>.filter.txt: which view, sort, filters and counts the CSV holds."""
        from datetime import datetime, timezone
        v = self.g.views[self.cur_vid]
        st = self.view_state(self.cur_vid)
        sort = st["sort"] if st["sort"] and st["sort"][0] in v.cols else None
        rng = self._range_checks(self.cur_vid)
        text = self.filter_var.get().strip()
        lines = [
            "Phone Link Relations Viewer - what the CSV next to this file contains", "",
            f"CSV file: {os.path.basename(path)}",
            f"Written (UTC): {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}",
            f"Device: {self.devices[self.dev_idx][0] if self.dev_idx is not None else ''}",
            f"View: {v.vid} - {nav_place(v, self.built_min)[2]} ({v.title})",
            f"Rows exported: {len(self.cur_rows):,} of {len(v.rows):,}, in the order shown in the viewer",
            "Sort: " + (f"{sort[0]}, {sort_words(self.col_type(sort[0]), sort[1])} (blanks last)" if sort
                        else "none - the order the rows were read"),
            "Case-wide dates: " + (rng[0][1].describe(rng[0][0]) if rng else
                                   "none" if self.range_filter is None else "none apply - this view has no time column"),
            "Column filters:"]
        lines += [f"  {f.describe(c)}" for c, f in st["filters"].items() if f.active] or ["  none"]
        lines += [f"Text search (all columns, case-insensitive): " + (f"'{F.visible(text)}'" if text else "none"), "",
                  "All filters combine with AND. Blank cells (and, for a date condition, cells that are not times) "
                  "are kept unless (Blanks) was unticked: see 'blanks kept' / 'blanks removed' above.",
                  "Cells are written as stored: invisible characters are not marked in the CSV. Excel-safe: a cell "
                  "that starts with = + - @ (or a tab or line break) gets a leading ' unless it is a plain number; "
                  "bytes are written as <N bytes>."]
        return self._scrub("\n".join(lines) + "\n")

    # ------------------------------------------------------------ links
    def row_entities(self, row):
        ents = []
        if row.get("_e"):
            ents.append(row["_e"])
        here = self.g.views[self.cur_vid].db
        for col, kind in list(COL_ENTITY.items()) + list(VIEW_COL_ENTITY.get(self.cur_vid, {}).items()):
            if row.get(col) not in (None, ""):
                e = (kind, row[col])
                if e in self.g.home and e not in ents:
                    same_db = self.g.views[self.g.home[e][0]].db == here
                    if same_db or self.show_cross.get():
                        ents.append(e)
        return ents

    def _detail_set(self, parts=()):
        """Replace the selected-row text. The box is read-only for the user, so it is unlocked only while we write."""
        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")
        for text, tag in parts:
            self.detail.insert("end", text, tag)
        self.detail.config(state="disabled")

    def on_row(self, _=None):
        sel = self.table.selection()
        if self.g is None or not sel:
            return
        v = self.g.views[self.cur_vid]
        row = v.rows[int(sel[0])]
        parts = []
        for c in v.cols:
            val = row.get(c)
            if val in (None, ""):
                continue
            parts += [(f"{c}: ", "k"), (f"{F.visible(F.cell_text(val), keep_breaks=True)}\n", "")]
        if row.get("_json"):
            import json
            parts += [("\nRaw JSON (as stored):\n", "k"),
                      (F.visible(json.dumps(row["_json"], indent=2, ensure_ascii=False), keep_breaks=True), "")]
        self._detail_set(parts)
        self.links.delete(*self.links.get_children())
        self.link_targets = {}
        n = 0
        own = row.get("_e")
        mine = self.row_entities(row)
        for ent in mine:
            for rid, target, note in sorted(self.g.links(ent), key=lambda x: (x[0], str(x[1]))):
                rv = self.g.views.get(rid)
                scope = rv.kind if rv else "in-table"
                if scope == "cross" and not self.show_cross.get():
                    continue
                if target == own or target in mine:   # the row's own entities are not "linked rows"
                    continue
                home = self.g.home.get(target)
                where = ""
                if home:
                    hv = self.g.views[home[0]]
                    where = self.friendly(home[0])
                iid = f"l{n}"
                self.links.insert("", "end", iid=iid, tags=(scope,),
                                  values=(note or (nav_place(rv, self.built_min)[2] if rv else rid), self.g.label.get(target, str(target)),
                                          where, SCOPE_WORDS.get(scope, scope)))
                self.link_targets[iid] = target
                n += 1
        if n == 0:      # in "target", the first column shown: the pane never looks empty
            self.links.insert("", "end", values=("", "No other row is linked to this one" +
                                                 ("" if self.show_cross.get() else " inside its database"), "", ""))

    def _fit_link_cols(self, width=None):
        """The linked-rows columns sized to the pane: each at least as wide as its heading, the rest shared out
        (most to the linked row and how it is linked). Narrower than the headings: those widths, and it scrolls."""
        width = int(width or self.links.winfo_width())
        if width <= 1:
            return
        head = tkfont.nametofont("PL.bold", root=self)
        mins = {c: head.measure(t) + self.px(18) for c, t, _ in LINK_COLS}
        spare = width - self.px(4) - sum(mins.values())       # the Treeview's own border
        weights = sum(w for _, _, w in LINK_COLS)
        widths = {c: mins[c] + (max(0, spare) * w // weights) for c, _, w in LINK_COLS}
        if spare > 0:      # rounding: the stretching column takes what is left
            widths["target"] += width - self.px(4) - sum(widths.values())
        for c, w in widths.items():
            if int(self.links.column(c, "width")) != w:
                self.links.column(c, width=w)

    def on_jump(self, _=None):
        sel = self.links.selection()
        if self.g is None or not sel or sel[0] not in self.link_targets:
            return
        target = self.link_targets[sel[0]]
        if target not in self.g.home:
            messagebox.showinfo("No table row", f"{target} has no row of its own to open.")
            return
        vid, idx = self.g.home[target]
        self.show(vid, select_index=idx)
        self.on_row()

    # ------------------------------------------------------------ actions
    def open_folder(self):
        d = filedialog.askdirectory(title="Phone Link device Database folder, or the package folder")
        if not d:
            return
        devs = P.find_device_dirs(d)
        if not devs:
            messagebox.showerror("No Phone Link databases",
                                 self._scrub(f"No Phone Link databases found under\n{self._shown(d)}"))
            return
        # the linked-phone and live this-PC views belong to the folder opened, not to the first one; like the old
        # list, view and device, they stay as they are if this folder cannot be loaded
        self.load_device(0, devices=devs, sources=self.sources_for(d))

    def export(self):
        if self.g is None or not self.cur_vid:
            return
        v = self.g.views[self.cur_vid]
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"{self.cur_vid}.csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        name = os.path.basename(path) if self.redact else path
        try:
            write_csv_atomic(path, v.cols, ([v.rows[i].get(c) for c in v.cols] for i in self.cur_rows))
        except Exception as e:   # nothing was written to `path`; say so, and do not leave an old success message
            self._status(f"Export FAILED - nothing was written to {name}: {self._why(e)}", warn=True)
            messagebox.showerror("Export failed", self._scrub(f"Could not write\n{name}\n\n{self._why(e)}"))
            return
        side = path + ".filter.txt"     # what the CSV holds: view, sort, every filter and the counts
        try:
            write_text_atomic(side, self.export_record(path))
            side_note = f"; the view, sort and filters are recorded in {os.path.basename(side)}"
            warn = False
        except Exception as e:
            side_note, warn = f"; WARNING: the filter record {os.path.basename(side)} could not be written: " \
                                    f"{self._why(e)}", True
        self._status(f"Exported {len(self.cur_rows):,} rows to {name} - Excel-safe: a cell that starts with = + - @ "
                     f"(or a tab or line break) gets a leading ' so Excel shows it as text, unless the whole cell is a "
                     f"plain number such as -5, 3.14 or +15550100; bytes are written as <N bytes> "
                     f"(contains case data - keep local){side_note}", warn=warn)

    def report_callback_exception(self, exc, val, tb):
        """An error inside a button, key or timer callback: print the traceback and tell the user, briefly."""
        traceback.print_exception(exc, val, tb)
        try:
            messagebox.showerror("Something went wrong", self._scrub(
                f"{exc.__name__}: {val}\n\nThe window is still open. The full details are in the console."))
        except tk.TclError:
            pass

    def on_close(self, warn=True):
        self._cancel_filter_job()
        self.theme_mode = "light"          # stops Follow Windows polling
        self._schedule_theme_poll()
        for attr in ("_title_job", "_nav_fit_job"):
            self._cancel_job(attr)
        if self.ev:
            self._remember(self.ev.close())
        if self.pc:
            self._remember(self.pc.close())
        if self.leftovers and warn:
            messagebox.showwarning("Copies not removed", "These temporary copies of the evidence could not be "
                                   "removed (a file is still in use). Remove them yourself:\n\n" +
                                   "\n".join(self._shown(p) for p in self.leftovers))
        self.destroy()

    def screenshot(self, path):
        """Save the window to a PNG (needs Pillow). Captures the window itself, not the screen behind it."""
        self.deiconify()
        self.update()
        self.after(500)
        self.update()
        img = None
        if os.name == "nt":
            try:
                img = self._print_window()
            except (OSError, ValueError, AttributeError):
                img = None
        if img is None:
            from PIL import ImageGrab
            self.lift()
            self.update()
            x, y = self.winfo_rootx(), self.winfo_rooty()
            img = ImageGrab.grab(bbox=(x, y, x + self.winfo_width(), y + self.winfo_height()))
        img.save(path)

    def _print_window(self):
        """Win32 PrintWindow(PW_RENDERFULLCONTENT): renders the window even when it is covered or off-screen."""
        import ctypes
        from ctypes import wintypes
        from PIL import Image
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindowDC.restype = wintypes.HDC
        user32.GetWindowDC.argtypes = [wintypes.HWND]
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                    ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT]
        hwnd = user32.GetAncestor(self.winfo_id(), 2)  # GA_ROOT
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        hdc = user32.GetWindowDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        try:
            if not user32.PrintWindow(hwnd, mdc, 2):
                return None
            bmi = BITMAPINFOHEADER(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            if not gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0):
                return None
            return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
            user32.ReleaseDC(hwnd, hdc)


GLYPH = {"on": CHECKED, "off": UNCHECKED, "partial": PARTIAL}


class FilterPanel(tk.Frame):
    """The Excel-style AutoFilter drop-down of one column, placed under its heading: sort (worded for the column's
    type), clear, a typed condition, a search box, the value list with row counts - (Select All) first and (Blanks)
    last; a date column is grouped Year > Month > Day with tri-state ticks - and OK / Cancel. Esc cancels, Enter
    applies. The logic is in plfilter; this only draws it."""

    def __init__(self, app, vid, col):
        super().__init__(app, highlightthickness=1, bd=0)
        role = app.themer.register        # colours by role: the panel follows the theme, also when it changes
        role(self, zone="page", highlightbackground="outline", highlightcolor="outline")
        self.app, self.vid, self.col = app, vid, col
        v = app.g.views[vid]
        self.ctype = ctype = app.col_type(col, vid)
        st = app.view_state(vid)
        cur = st["filters"].get(col)
        # the value list shows what the OTHER filters leave, like Excel; presets count back from the whole column
        others = [(c, f) for c, f in st["filters"].items() if c != col] + app._range_checks(vid)
        values = [v.rows[i].get(col) for i in F.filter_indices(v.rows, others)]
        self.all_values = [r.get(col) for r in v.rows]
        items, blanks = F.distinct(values, ctype)
        self.undated = sum(1 for x in values if not F.is_blank(x) and F.parse_time(x) is None) \
            if ctype == F.DATE else 0
        self.checklist = F.Checklist(items, blanks, ticked=cur.values if cur else None,
                                     keep_blanks=cur.blanks if cur else True)
        self._groups = {}
        font, small = "PL.body", "PL.small"
        flat = dict(relief="flat", anchor="w", font=font, padx=6)
        flat_roles = dict(background="bg", foreground="ink", activebackground="hover", activeforeground="ink")
        muted = dict(foreground="muted")

        tk.Label(self, text=col, font="PL.group", anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        role(tk.Label(self, text=f"{ctype} column - {len(v.rows):,} rows", font=small, anchor="w"),
             **muted).pack(fill="x", padx=10)
        self.sort_btns = []
        for desc, label in enumerate(F.sort_labels(ctype)):
            b = role(tk.Button(self, text=label, command=lambda d=bool(desc): self.sort(d), **flat), **flat_roles)
            b.pack(fill="x", padx=4)
            self.sort_btns.append(b)
        self.clear_btn = role(tk.Button(self, text=f'Clear filter from "{col}"', command=self.clear,
                                        state="normal" if cur is not None and cur.active else "disabled", **flat),
                              **flat_roles)
        self.clear_btn.pack(fill="x", padx=4)
        ttk.Separator(self).pack(fill="x", padx=6, pady=4)

        box = tk.Frame(self)
        box.pack(fill="x", padx=10)
        role(tk.Label(box, text="Condition:", font=font), **muted).grid(row=0, column=0, sticky="w")
        self.op_var = tk.StringVar(self, value=cur.cond.op if cur is not None and cur.cond else NONE_OP)
        self.op_combo = ttk.Combobox(box, textvariable=self.op_var, state="readonly", width=18,
                                     values=[NONE_OP] + F.OPS[ctype])
        self.op_combo.grid(row=0, column=1, columnspan=3, sticky="w", padx=(4, 0))
        self.a_var = tk.StringVar(self, value=cur.cond.a if cur is not None and cur.cond else "")
        self.b_var = tk.StringVar(self, value=cur.cond.b if cur is not None and cur.cond else "")
        self.a_entry = tk.Entry(box, textvariable=self.a_var, width=19)
        self.a_entry.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        role(tk.Label(box, text="to" if ctype == F.DATE else "and", font=font), **muted).grid(row=1, column=2,
                                                                                            padx=4, pady=(4, 0))
        self.b_entry = tk.Entry(box, textvariable=self.b_var, width=19)
        self.b_entry.grid(row=1, column=3, sticky="w", pady=(4, 0))
        self.preset_btns = []
        if ctype == F.DATE:
            role(tk.Label(box, text=f"Dates: {F.DATE_HELP}; To is inclusive", font=small),
                 **muted).grid(row=2, column=0, columnspan=4, sticky="w")
            quick = tk.Frame(box)
            quick.grid(row=3, column=0, columnspan=4, sticky="w", pady=(2, 0))
            role(tk.Label(quick, text="Newest in column, back:", font=small), **muted).pack(side="left")
            for name in F.PRESETS:
                b = role(tk.Button(quick, text=name.replace("Last ", "").replace(" of data", ""), relief="flat",
                                   font=small, padx=4, command=lambda n=name: self.preset(n)),
                         background="bg", foreground="kind.in-table", activebackground="hover",
                         activeforeground="kind.in-table")
                b.pack(side="left")
                self.preset_btns.append(b)
        wrap = app.px(330)
        self.error_lbl = role(tk.Label(self, text="", font=font, anchor="w", justify="left", wraplength=wrap),
                              foreground="kind.check")
        self.error_lbl.pack(fill="x", padx=10)
        self.blanks_var = tk.BooleanVar(self, value=self.checklist.blanks)
        self.blanks_chk = tk.Checkbutton(self, variable=self.blanks_var, anchor="w", font=font)
        self.blanks_chk.pack(fill="x", padx=8)
        ttk.Separator(self).pack(fill="x", padx=6, pady=4)

        role(tk.Label(self, text="Search values", font=small, anchor="w"), **muted).pack(fill="x", padx=10)
        self.search_var = tk.StringVar(self)
        self.search_entry = tk.Entry(self, textvariable=self.search_var)
        self.search_entry.pack(fill="x", padx=10, pady=(0, 4))
        lst = tk.Frame(self)
        lst.pack(fill="both", expand=True, padx=10)
        self.list = ttk.Treeview(lst, show="tree", selectmode="browse", height=11)
        self.list.column("#0", width=app.px(320), stretch=True)
        ys = ttk.Scrollbar(lst, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.list.pack(side="left", fill="both", expand=True)
        self.cap_lbl = role(tk.Label(self, text="", font=small, anchor="w", justify="left", wraplength=wrap),
                            **muted)
        self.cap_lbl.pack(fill="x", padx=10)
        btns = tk.Frame(self)
        btns.pack(fill="x", padx=10, pady=8)
        self.cancel_btn = tk.Button(btns, text="Cancel", width=9, command=self.cancel, relief="flat")
        self.cancel_btn.pack(side="right", padx=(6, 0))
        self.ok_btn = role(tk.Button(btns, text="OK", width=9, command=self.ok, relief="flat"),
                           background="accent", foreground="on_accent", activebackground="accent",
                           activeforeground="on_accent")
        self.ok_btn.pack(side="right")
        app.themer.paint(self)

        self.op_var.trace_add("write", lambda *a: self._op_changed())
        self.blanks_var.trace_add("write", lambda *a: self._blanks_changed())
        self.search_var.trace_add("write", lambda *a: self._search())
        self.list.bind("<Button-1>", self._click)
        self.list.bind("<space>", self._space)
        self._op_changed(clear=False)
        self.fill_list()
        self.refresh_note()
        self.bind_class("PLFilterPanel", "<Escape>", lambda e: self._key(e, "cancel"))
        self.bind_class("PLFilterPanel", "<Return>", lambda e: self._key(e, "ok"))
        self.bind_class("PLFilterPanel", "<KP_Enter>", lambda e: self._key(e, "ok"))
        self._keys(self)
        self._place()
        self.search_entry.focus_set()

    # ---- drawing
    def _keys(self, w):
        """Esc and Enter on every widget of the panel (before the widget's own class bindings)."""
        tags = w.bindtags()
        w.bindtags((tags[0], "PLFilterPanel") + tags[1:])
        for c in w.winfo_children():
            self._keys(c)

    def _key(self, e, what):
        panel = getattr(e.widget.winfo_toplevel(), "panel", None) if hasattr(e.widget, "winfo_toplevel") else None
        if panel is not None:
            panel.cancel() if what == "cancel" else panel.ok()
        return "break"

    def _place(self):
        """Under the column's heading, kept inside the window."""
        app, t = self.app, self.app.table
        app.update_idletasks()
        shown = t["displaycolumns"]
        cols = list(t["columns"]) if not shown or shown[0] == "#all" else list(shown)
        widths = [int(t.column(c, "width")) for c in cols]
        x = sum(widths[:cols.index(self.col)]) if self.col in cols else 0
        x -= t.xview()[0] * sum(widths)
        x += t.winfo_rootx() - app.winfo_rootx()
        y = t.winfo_rooty() - app.winfo_rooty() + app.px(24)
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = max(4, min(int(x), app.winfo_width() - w - 4))
        y = max(4, min(int(y), app.winfo_height() - h - 4))
        self.place(in_=app, x=x, y=y)
        self.lift()

    def fill_list(self):
        cl, lst = self.checklist, self.list
        lst.delete(*lst.get_children())
        keys, capped, total = cl.shown()
        self._groups = {}
        lst.insert("", "end", iid="all", text=self._text("all"))
        if self.ctype == F.DATE:
            tree = F.group_days(keys)
            for y, months in tree["years"]:
                yid = f"y:{y}"
                self._groups[yid] = [k for _, days in months for k in days]
                lst.insert("", "end", iid=yid, text=self._text(yid), open=True)
                for ym, days in months:
                    mid = f"m:{ym}"
                    self._groups[mid] = days
                    lst.insert(yid, "end", iid=mid, text=self._text(mid),
                               open=bool(cl.query) or (len(tree["years"]) == 1 and len(months) == 1))
                    for d in days:
                        lst.insert(mid, "end", iid="v:" + d, text=self._text("v:" + d))
            flat = tree["other"]          # cells that are not times: listed as they are, after the dates
        else:
            flat = keys
        for k in flat:
            lst.insert("", "end", iid="v:" + k, text=self._text("v:" + k))
        if cl.blank_count:
            lst.insert("", "end", iid="blanks", text=self._text("blanks"))
        if capped:
            self.cap_lbl.config(text=f"Showing the first {F.SHOWN_CAP:,} of {total:,} values (like Excel) - type in "
                                     f"Search to find the others; Select All and OK still cover every value")
        elif cl.query and not total:
            self.cap_lbl.config(text="No value matches the search")
        else:
            self.cap_lbl.config(text="")

    def _text(self, iid):
        cl = self.checklist
        if iid == "all":
            return f"{GLYPH[cl.all_state()]} {'(Select All Search Results)' if cl.query else '(Select All)'}"
        if iid == "blanks":
            return f"{GLYPH['on' if cl.blanks else 'off']} (Blanks)   ({cl.blank_count:,})"
        if iid in self._groups:
            keys = self._groups[iid]
            label = iid[2:] if iid.startswith("y:") else F.month_label(iid[2:])
            return f"{GLYPH[cl.group_state(keys)]} {label}   ({sum(cl.counts[k] for k in keys):,})"
        key = iid[2:]
        if self.ctype == F.DATE and len(key) == 10 and F.parse_when(key) is not None:
            label = key[8:10]
        else:
            label = elide_middle(F.visible(key), 90)
        return f"{GLYPH['on' if key in cl.ticked else 'off']} {label}   ({cl.counts.get(key, 0):,})"

    def _retext(self, iids=None):
        lst = self.list
        if iids is None:
            iids, stack = [], list(lst.get_children())
            while stack:
                i = stack.pop()
                iids.append(i)
                stack.extend(lst.get_children(i))
        for i in iids:
            if lst.exists(i):
                lst.item(i, text=self._text(i))

    def refresh_note(self):
        """'(Blanks: N rows kept)' - the explicit choice about blank (and, with a date condition, undated) rows."""
        extra = self.undated if self.ctype == F.DATE and self.op_var.get() != NONE_OP else 0
        n = self.checklist.blank_count + extra
        what = "Blanks and undated" if extra else "Blanks"
        self.blanks_chk.config(text=f"({what}: {n:,} row{'' if n == 1 else 's'} "
                                    f"{'kept' if self.blanks_var.get() else 'removed'})")

    # ---- events
    def _op_changed(self, clear=True):
        op = self.op_var.get()
        if op == NONE_OP and clear:
            self.a_var.set("")
            self.b_var.set("")
        self.a_entry.config(state="disabled" if op == NONE_OP else "normal")
        self.b_entry.config(state="normal" if op == "between" else "disabled")
        self.error_lbl.config(text="")
        self.refresh_note()

    def _blanks_changed(self):
        self.checklist.blanks = bool(self.blanks_var.get())
        self._retext(["blanks", "all"])
        self.refresh_note()

    def _search(self):
        self.checklist.search(self.search_var.get())
        self.fill_list()

    def _click(self, e):
        iid = self.list.identify_row(e.y)
        if not iid or "indicator" in str(self.list.identify_element(e.x, e.y)):
            return None             # the open/close arrow of a year or month works as usual
        self.list.focus(iid)
        self.list.selection_set(iid)
        self.toggle_item(iid)
        return "break"

    def _space(self, _=None):
        iid = self.list.focus()
        if iid:
            self.toggle_item(iid)
        return "break"

    def toggle_item(self, iid):
        """Tick or untick a list entry, as a click on it does. A partial or unticked group ticks all its values."""
        cl, lst = self.checklist, self.list
        if iid == "all":
            cl.set_all(cl.all_state() != "on")
            if not cl.query:
                self.blanks_var.set(cl.blanks)
            self._retext()
            return
        if iid == "blanks":
            self.blanks_var.set(not self.blanks_var.get())
            return
        if iid in self._groups:
            keys = self._groups[iid]
            on = cl.group_state(keys) != "on"
            for k in keys:
                cl.set(k, on)
            changed = [iid] + list(lst.get_children(iid))
            changed += [c for m in lst.get_children(iid) for c in lst.get_children(m)]
        else:
            cl.toggle(iid[2:])
            changed = [iid]
        p = lst.parent(iid)
        while p:
            changed.append(p)
            p = lst.parent(p)
        self._retext(changed + ["all"])

    # ---- actions
    def ok(self):
        """Apply: the condition (if any) AND the ticked values AND the blanks choice. Bad input stays shown."""
        op = self.op_var.get()
        cond = None
        if op != NONE_OP:
            try:
                cond = F.make_condition(self.ctype, op, self.a_var.get(), self.b_var.get())
            except F.FilterError as e:
                self.error_lbl.config(text=f"Not applied: {e}")
                return False
        values, blanks = self.checklist.result()
        self.app.apply_column_filter(self.vid, self.col, F.ColumnFilter(self.ctype, values, blanks, cond))
        self.app.close_panel()
        return True

    def cancel(self):
        self.app.close_panel()

    def clear(self):
        self.app.apply_column_filter(self.vid, self.col, None)
        self.app.close_panel()

    def sort(self, descending):
        self.app.close_panel()
        self.app.set_sort(self.col, descending)

    def preset(self, name):
        """A quick date range ending at the NEWEST time in this column (evidence is often old - never now)."""
        rng = F.preset_range(self.all_values, name)
        if rng is None:
            self.error_lbl.config(text="No dates in this column to count back from")
            return
        self.op_var.set("between")
        self.a_var.set(rng[0])
        self.b_var.set(rng[1])


class Hint:
    """A hover hint for a widget (shown after half a second, gone when the pointer leaves)."""

    def __init__(self, widget, text):
        self.widget, self.text, self.tip, self.job = widget, text, None, None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _=None):
        self._leave()
        self.job = self.widget.after(500, self._show)

    def _leave(self, _=None):
        if self.job is not None:
            try:
                self.widget.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None

    def _show(self):
        self.job = None
        try:
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.geometry(f"+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}")
            tip_label(tip, self.text)
            self.tip = tip
        except tk.TclError:
            pass


def tip_label(tip, text):
    """The text of a hover tip, in the tip colours of the window's theme."""
    lbl = tk.Label(tip, text=text, font="PL.body", justify="left", wraplength=T.px(tip, 420), padx=8, pady=6)
    lbl.pack()
    themer = getattr(tip.master.winfo_toplevel(), "themer", None)      # the viewer the tip belongs to
    if themer is not None:
        themer.set(tip, zone="tip")
    else:
        light = T.PALETTES["light"]
        tip.configure(background=light["tip_bg"])
        lbl.configure(background=light["tip_bg"], foreground=light["tip_fg"])
    return lbl


class TreeTip:
    """A hover tip for the rows of a Treeview whose text is shown shortened: text_for(iid) gives the full text,
    or None when the row shows it whole."""

    def __init__(self, tree, text_for):
        self.tree, self.text_for = tree, text_for
        self.iid = self.tip = self.job = None
        tree.bind("<Motion>", self._motion, add="+")
        tree.bind("<Leave>", lambda e: self.hide(), add="+")
        tree.bind("<ButtonPress>", lambda e: self.hide(), add="+")

    def _motion(self, e):
        iid = self.tree.identify_row(e.y)
        if iid == self.iid:
            return
        self.hide()
        self.iid = iid
        if iid and self.text_for(iid):
            self.job = self.tree.after(500, lambda: self.show(iid))

    def show(self, iid):
        """Show the full text of row iid under it; returns the tip window (or None)."""
        self.job = None
        text = self.text_for(iid)
        if not text:
            return None
        self.hide_tip()
        try:
            tip = tk.Toplevel(self.tree)
            tip.wm_overrideredirect(True)
            box = self.tree.bbox(iid) if self.tree.exists(iid) else ""
            x = self.tree.winfo_rootx() + (box[0] if box else 0)
            y = self.tree.winfo_rooty() + (box[1] + box[3] if box else 0) + 2
            tip.geometry(f"+{x}+{y}")
            tip_label(tip, text)
            self.tip = tip
        except tk.TclError:
            return None
        return self.tip

    def hide_tip(self):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None

    def hide(self):
        if self.job is not None:
            try:
                self.tree.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        self.iid = None
        self.hide_tip()

def stale_copies(exclude=()):
    """Copy folders left in the temp folder by earlier runs that still hold databases."""
    tmp = tempfile.gettempdir()
    out = []
    for name in os.listdir(tmp):
        p = os.path.join(tmp, name)
        if name.startswith(("plrelate-", "pldevices-")) and os.path.isdir(p) and p not in exclude:
            for _, _, files in os.walk(p):
                if any(f.endswith(".db") for f in files):
                    out.append(p)
                    break
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="device Database folder, Indexed folder or Phone Link package folder")
    src.add_argument("--demo", action="store_true", help="synthetic data only")
    ap.add_argument("--window", type=int, default=R.DEFAULT_WINDOW_MIN, help="nearby-events window in minutes")
    ap.add_argument("--view", help="view to open first, for example R13 or E_calls")
    ap.add_argument("--select", type=int, help="row index to select in that view")
    ap.add_argument("--screenshot", help="save a PNG of the window and exit (needs Pillow)")
    ap.add_argument("--open-filter", metavar="COLUMN",
                    help="open the filter panel of this column of the first view (for screenshots)")
    ap.add_argument("--this-pc", action="store_true",
                    help="also show phones connected to THIS computer by USB cable or Bluetooth (live registry, logs "
                         "and event logs, read-only). On by default when --input is this PC's own Phone Link folder")
    ap.add_argument("--theme", choices=T.MODES,
                    help="light, dark, or system (follow the Windows app colour). Default: the theme last picked in "
                         "the window, else system. --demo and --screenshot runs never read or save that choice")
    a = ap.parse_args()
    theme, settings = startup_theme(a.theme, a.demo, a.screenshot)
    if a.demo:
        import pldemo
        root = pldemo.build_package(tempfile.mkdtemp(prefix="pldemo-"))
        devices = [(f"demo (synthetic) {lbl}", d) for lbl, d in P.find_device_dirs(root)]
        devices.sort(key=lambda d: -len([f for f in os.listdir(d[1]) if f.endswith(".db")]))  # fullest first
        localcache = D.find_localcache(root)
        connections = C.demo_source()
    else:
        devices = P.find_device_dirs(os.path.expandvars(a.input))
        if not devices:
            sys.exit(f"No Phone Link databases found under {a.input}")
        devices.sort(key=lambda d: -len([f for f in os.listdir(d[1]) if f.endswith(".db")]))  # fullest first
        localcache = D.find_localcache(a.input)
        connections = C.LiveSource() if (a.this_pc or is_this_pc_package(a.input)) and os.name == "nt" else None
    app = Viewer(devices, a.window, a.view, localcache, connections, redact=a.demo, this_pc=a.this_pc, theme=theme,
                 settings_path=settings)
    if a.select is not None:
        app.show(app.cur_vid, select_index=a.select)
        app.on_row()
    if a.open_filter:
        app.after(300, lambda: app.open_filter(a.open_filter))
    if a.screenshot:
        app.after(1500, lambda: (app.screenshot(a.screenshot), app.on_close(warn=False)))
    app.mainloop()


if __name__ == "__main__":
    main()
