"""
pltheme.py - light and dark colours, named fonts, DPI and the title bar for the Phone Link viewer.

Standard library only. Nothing here touches evidence: the only things it reads on this computer are the Windows
app-colour setting (for "Follow Windows") and the viewer's own settings file.

API
---
PALETTES            {"light": {role: "#rrggbb"}, "dark": {...}}; both have the same roles:
                      bg (page), field (tables, text boxes, the Contents list), panel (toolbars, status bar),
                      panel_fg / panel_title (text on panel), ink (text), muted (secondary text), faint (empty
                      views, disabled text), accent / on_accent (main buttons), button / on_button (other buttons),
                      select_bg / select_fg (selected rows and text), heading_bg (column headings), hover, border,
                      outline (the frame of the filter panel),
                      trough / thumb / thumb_active (scrollbars), edge (the border of text boxes and drop-downs),
                      warn and status (status-bar text), tip_bg / tip_fg (hover hints),
                      on_kind (badge text), deleted (reserved: deleted rows), and kind.<k> for every kind of view
                      (kind.check is red, kind.cross orange, kind.in-table green ...). Meaning never rests on colour
                      alone: every badge, scope and group also says it in words.
TEXT_PAIRS, AA      every (text role, background role) the viewer draws; failing_pairs(name) lists the pairs of a
                    palette under AA (4.5:1, WCAG 2 AA for normal text) as (fg, bg, ratio).
NON_TEXT_PAIRS, UI_AA
                    controls against their neighbours (scrollbar thumb on trough, text-box borders on their fill
                    and surfaces); failing_ui_pairs(name) lists those under 3:1 (WCAG 2.1 1.4.11, non-text).
contrast(a, b)      WCAG 2 contrast ratio of two '#rrggbb' colours; hue(c) the hue in degrees.
MODES, next_mode(m) "light", "dark", "system" (follow Windows), in the order Ctrl+Shift+L cycles through them.
read_apps_use_light_theme()
                    the real reader: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize
                    AppsUseLightTheme as an int, or None when it is missing or this is not Windows.
windows_theme(reader=None), resolve(mode, reader=None)
                    the palette name: "dark" only when reader() returns 0; missing or unreadable is "light".
                    The reader is injectable so tests never touch the registry.
settings_path(), load_settings(path), save_settings(data, path), saved_mode(path), save_mode(mode, path)
                    %APPDATA%\\phone-link-forensics\\viewer.json, written atomically. A missing or corrupt file
                    reads as {} / None and a failed write returns False: never an exception.
FONTS, make_fonts(root), refit_fonts(root)
                    named fonts (PL.body, PL.bold, PL.italic, PL.small, PL.smallbold, PL.group, PL.title,
                    PL.viewtitle) in points, so they follow the display scaling; keep the returned dict alive.
px(root, n)         n pixels at 96 DPI, in this display's pixels. row_height(root, *fonts): a Treeview rowheight
                    that fits the tallest of the named fonts at the current scaling.
fit_text(text, measure, width)
                    text cut at the end with an ellipsis so that measure(result) <= width.
dpi_aware(api=None, os_name=None)
                    system DPI awareness (returns 1; not per-monitor: Tk does not handle a DPI change), else
                    None. Off Windows: None.
set_title_bar(window, dark, api=None, os_name=None)
                    a dark or light Windows title bar (DWMWA_USE_IMMERSIVE_DARK_MODE 20, then 19 for older
                    Windows 10), then a frame refresh. Failures are ignored (False); nothing happens off Windows.
style_ttk(root, pal)
                    the ttk "clam" styles (".", Treeview, Treeview.Heading, TScrollbar, TCombobox, TSeparator) and
                    the Combobox drop-down list colours (option database).
Themer(pal)         colours classic Tk widgets (which ignore ttk styles) by role:
                      register(widget, zone=None, **{option: role}) - a zone ("page", "panel", "field", "tip")
                      sets the surface for the widget and everything inside it; options name roles;
                      paint(widget) - recolour the widget and everything inside it with the current palette,
                        walking Tk's own widget tree (so a Combobox drop-down, made by Tk, is included);
                      set(widget, **roles) - register and paint at once (runtime colours: badge, status ...).
"""
import colorsys
import json
import os
import tempfile

AA = 4.5
MODES = ("light", "dark", "system")
KINDS = ("pc", "conn", "table", "in-table", "in-db", "check", "cross", "map", "provenance")

PALETTES = {
    "light": {
        "bg": "#f4f7fa", "field": "#ffffff", "panel": "#16293f", "panel_fg": "#9fb3c8", "panel_title": "#ffffff",
        "ink": "#13233a", "muted": "#55697e", "faint": "#66727f",
        "accent": "#00766c", "on_accent": "#ffffff", "button": "#1e3a56", "on_button": "#ffffff",
        "select_bg": "#4a6984", "select_fg": "#ffffff", "heading_bg": "#e3e9ef", "hover": "#d3dde7",
        "border": "#b8c4d0", "outline": "#16293f", "trough": "#e3e9ef", "thumb": "#717e8c",
        "thumb_active": "#55697e", "edge": "#717e8c",
        "warn": "#ffb74d", "status": "#9fb3c8", "tip_bg": "#13233a", "tip_fg": "#ffffff",
        "on_kind": "#ffffff", "deleted": "#ad1457",
        "kind.pc": "#5d4037", "kind.conn": "#37474f", "kind.table": "#1e3a56", "kind.in-table": "#2e7d32",
        "kind.in-db": "#00796b", "kind.check": "#c62828", "kind.cross": "#b45309", "kind.map": "#6a1b9a",
        "kind.provenance": "#546e7a",
    },
    "dark": {
        "bg": "#1b1f24", "field": "#22272e", "panel": "#0d1117", "panel_fg": "#9aa7b4", "panel_title": "#f0f6fc",
        "ink": "#e6edf3", "muted": "#9aa7b4", "faint": "#8b949e",
        "accent": "#2ec4b6", "on_accent": "#0d1117", "button": "#30363d", "on_button": "#e6edf3",
        "select_bg": "#1f4e79", "select_fg": "#ffffff", "heading_bg": "#2d333b", "hover": "#373e47",
        "border": "#444c56", "outline": "#768390", "trough": "#1b1f24", "thumb": "#6e7681",
        "thumb_active": "#8b949e", "edge": "#768390",
        "warn": "#ffb454", "status": "#9aa7b4", "tip_bg": "#30363d", "tip_fg": "#e6edf3",
        "on_kind": "#0d1117", "deleted": "#f778ba",
        "kind.pc": "#d7a98c", "kind.conn": "#a7b6c2", "kind.table": "#79b8ff", "kind.in-table": "#7ee787",
        "kind.in-db": "#4dd0c4", "kind.check": "#ff7b72", "kind.cross": "#ffb454", "kind.map": "#d2a8ff",
        "kind.provenance": "#a5b4c0",
    },
}

TEXT_PAIRS = [
    ("ink", "bg"), ("ink", "field"), ("muted", "bg"), ("muted", "field"), ("faint", "bg"), ("faint", "field"),
    ("panel_fg", "panel"), ("panel_title", "panel"), ("status", "panel"), ("warn", "panel"),
    ("on_accent", "accent"), ("on_button", "button"), ("select_fg", "select_bg"),
    ("ink", "heading_bg"), ("ink", "hover"), ("tip_fg", "tip_bg"), ("deleted", "field"), ("deleted", "bg"),
] + [p for k in KINDS for p in ((f"kind.{k}", "bg"), (f"kind.{k}", "field"), ("on_kind", f"kind.{k}"))]

# Controls, not text (WCAG 2.1, 1.4.11 non-text contrast, 3:1): a scrollbar thumb against its trough (also while
# pointed at), and the border of a text box or drop-down against its own fill and every surface it sits on.
UI_AA = 3.0
NON_TEXT_PAIRS = [("thumb", "trough"), ("thumb_active", "trough"), ("edge", "field"), ("edge", "bg"),
                  ("edge", "panel"), ("outline", "bg")]


# ---------------------------------------------------------------- colour arithmetic
def _rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _luminance(c):
    lin = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in _rgb(c)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    """WCAG 2 contrast ratio of two '#rrggbb' colours (1 to 21)."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def hue(c):
    """Hue of a '#rrggbb' colour in degrees (0 = red)."""
    return colorsys.rgb_to_hls(*_rgb(c))[0] * 360


def failing_pairs(name):
    """[(text role, background role, ratio)] of palette `name` under AA."""
    pal = PALETTES[name]
    return [(f, b, contrast(pal[f], pal[b])) for f, b in TEXT_PAIRS if contrast(pal[f], pal[b]) < AA]


def failing_ui_pairs(name):
    """[(control role, neighbour role, ratio)] of palette `name` under UI_AA (3:1, non-text contrast)."""
    pal = PALETTES[name]
    return [(f, b, contrast(pal[f], pal[b])) for f, b in NON_TEXT_PAIRS if contrast(pal[f], pal[b]) < UI_AA]


# ---------------------------------------------------------------- light, dark, follow Windows
def next_mode(mode):
    return MODES[(MODES.index(mode) + 1) % len(MODES)] if mode in MODES else MODES[0]


PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def read_apps_use_light_theme():
    """AppsUseLightTheme from the current user's registry (0 = dark apps), or None."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE) as k:
            return int(winreg.QueryValueEx(k, "AppsUseLightTheme")[0])
    except (OSError, ValueError, TypeError):
        return None


def windows_theme(reader=None):
    """'dark' when Windows apps are set to dark, else 'light' (missing or unreadable setting: light)."""
    try:
        value = (reader or read_apps_use_light_theme)()
    except (OSError, ValueError, TypeError):
        return "light"
    return "dark" if value == 0 else "light"


def resolve(mode, reader=None):
    """The palette name for a mode: light and dark as they are, system from Windows."""
    return mode if mode in ("light", "dark") else windows_theme(reader)


# ---------------------------------------------------------------- the settings file
APP_DIR, SETTINGS_FILE = "phone-link-forensics", "viewer.json"


def settings_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_DIR, SETTINGS_FILE)


def load_settings(path=None):
    """The settings as a dict; {} when the file is missing, unreadable or not a JSON object."""
    try:
        with open(path or settings_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data, path=None):
    """Write the settings next to their place and move them in, so a crash never leaves half a file. True if saved."""
    path = path or settings_path()
    tmp = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".viewer-", suffix=".tmp", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def saved_mode(path=None):
    mode = load_settings(path).get("theme")
    return mode if mode in MODES else None


def save_mode(mode, path=None):
    data = load_settings(path)
    data["theme"] = mode
    return save_settings(data, path)


# ---------------------------------------------------------------- fonts and sizes
FONTS = {  # name -> (family, points, weight, slant)
    "PL.body": ("Segoe UI", 9, "normal", "roman"),
    "PL.bold": ("Segoe UI", 9, "bold", "roman"),
    "PL.italic": ("Segoe UI", 9, "normal", "italic"),
    "PL.small": ("Segoe UI", 8, "normal", "roman"),
    "PL.smallbold": ("Segoe UI", 8, "bold", "roman"),
    "PL.group": ("Segoe UI", 10, "bold", "roman"),
    "PL.title": ("Cambria", 15, "bold", "roman"),
    "PL.viewtitle": ("Cambria", 14, "bold", "roman"),
}


def make_fonts(root):
    """Create (or reset) the named fonts on root's interpreter. Keep the returned dict: a Font made here is
    deleted when its Python object is."""
    from tkinter import TclError, font as tkfont
    out = {}
    for name, (family, size, weight, slant) in FONTS.items():
        try:
            out[name] = tkfont.Font(root=root, name=name, family=family, size=size, weight=weight, slant=slant)
        except TclError:      # it already exists in this interpreter
            out[name] = tkfont.nametofont(name, root=root)
            out[name].configure(family=family, size=size, weight=weight, slant=slant)
    return out


def refit_fonts(root):
    """Measure the named fonts again (after the display scaling changed)."""
    from tkinter import font as tkfont
    for name in FONTS:
        f = tkfont.nametofont(name, root=root)
        f.configure(size=f.cget("size"))


def scale(root):
    """The display scale: 1.0 at 96 DPI, 1.5 at 144 DPI ..."""
    return float(root.tk.call("tk", "scaling")) * 72 / 96


def px(root, n):
    return int(round(n * scale(root)))


def row_height(root, *fonts):
    """Room for the tallest named font plus padding: never less than the old 22 px, scaled."""
    from tkinter import font as tkfont
    line = max(tkfont.nametofont(f, root=root).metrics("linespace") for f in fonts)
    return max(line + px(root, 4), px(root, 22))


def fit_text(text, measure, width):
    """text, or its longest start that fits `width` pixels with an ellipsis after it."""
    if measure(text) <= width:
        return text
    lo, hi = 0, len(text)            # the longest n with text[:n] + "…" fitting
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid].rstrip() + "…") <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


# ---------------------------------------------------------------- Windows: DPI and the title bar
def dpi_aware(api=None, os_name=None):
    """Crisp text on scaled displays: SYSTEM DPI awareness (1), as IDLE does, falling back to the older call
    (also system aware). Not per-monitor (2): Tk does not handle WM_DPICHANGED, so on a second monitor with
    another scaling the fonts and row heights would keep the first monitor's size; system aware, Windows scales
    the whole window there instead (slightly soft, but every size stays right)."""
    if (os_name or os.name) != "nt":
        return None
    if api is None:
        try:
            import ctypes
            api = ctypes.windll
        except (ImportError, AttributeError):
            return None
    try:
        api.shcore.SetProcessDpiAwareness(1)      # PROCESS_SYSTEM_DPI_AWARE
        return 1
    except (AttributeError, OSError):
        pass
    try:
        api.user32.SetProcessDPIAware()           # Windows 7 and 8: system DPI aware
        return 1
    except (AttributeError, OSError):
        return None


DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 20, 19
SWP_FLAGS = 0x0020 | 0x0002 | 0x0001 | 0x0004     # FRAMECHANGED | NOMOVE | NOSIZE | NOZORDER


class _WinApi:
    """The three Win32 calls the title bar needs, with their argument types."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        user32, dwmapi = ctypes.WinDLL("user32"), ctypes.WinDLL("dwmapi")
        self.GetAncestor = user32.GetAncestor
        self.GetAncestor.restype = wintypes.HWND
        self.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.DwmSetWindowAttribute = dwmapi.DwmSetWindowAttribute
        self.DwmSetWindowAttribute.restype = ctypes.c_long        # an HRESULT, read as a number
        self.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        self.SetWindowPos = user32.SetWindowPos
        self.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wintypes.UINT]


def set_title_bar(window, dark, api=None, os_name=None):
    """Dark or light title bar for a mapped Tk window. True when Windows accepted it; never raises."""
    if (os_name or os.name) != "nt":
        return False
    try:
        import ctypes
        api = api or _WinApi()
        wid = window.winfo_id()
        hwnd = api.GetAncestor(wid, 2) or wid           # GA_ROOT: the frame window, not Tk's inner one
        value = ctypes.c_int(1 if dark else 0)
        ok = False
        for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
            if api.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                ok = True
                break
        api.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_FLAGS)   # repaint the frame now, not at the next resize
        return ok
    except Exception:
        return False


# ---------------------------------------------------------------- ttk styles
def style_ttk(root, pal):
    from tkinter import ttk
    st = ttk.Style(root)
    bg, field, ink, faint = pal["bg"], pal["field"], pal["ink"], pal["faint"]
    st.configure(".", background=bg, foreground=ink, fieldbackground=field, troughcolor=pal["trough"],
                 bordercolor=pal["border"], lightcolor=bg, darkcolor=bg, selectbackground=pal["select_bg"],
                 selectforeground=pal["select_fg"], insertcolor=ink, arrowcolor=ink, focuscolor=pal["accent"],
                 font="PL.body")
    st.map(".", background=[("disabled", bg)], foreground=[("disabled", faint)])
    st.configure("Treeview", background=field, fieldbackground=field, foreground=ink, bordercolor=pal["border"],
                 lightcolor=field, darkcolor=field)
    st.map("Treeview", background=[("disabled", bg), ("selected", pal["select_bg"])],
           foreground=[("disabled", faint), ("selected", pal["select_fg"])])
    st.configure("Treeview.Heading", background=pal["heading_bg"], foreground=ink, bordercolor=pal["border"],
                 lightcolor=pal["heading_bg"], darkcolor=pal["heading_bg"], font="PL.bold")
    st.map("Treeview.Heading", background=[("pressed", pal["hover"]), ("active", pal["hover"])],
           foreground=[("active", ink)])
    st.configure("TScrollbar", background=pal["thumb"], troughcolor=pal["trough"], bordercolor=pal["trough"],
                 lightcolor=pal["thumb"], darkcolor=pal["thumb"], arrowcolor=ink, gripcount=0)
    st.map("TScrollbar", background=[("disabled", pal["trough"]), ("pressed", pal["thumb_active"]),
                                     ("active", pal["thumb_active"])], arrowcolor=[("disabled", faint)])
    st.configure("TCombobox", fieldbackground=field, background=pal["heading_bg"], foreground=ink, arrowcolor=ink,
                 bordercolor=pal["edge"], lightcolor=field, darkcolor=field, insertcolor=ink,
                 selectbackground=pal["select_bg"], selectforeground=pal["select_fg"])
    st.map("TCombobox", fieldbackground=[("disabled", bg), ("readonly", field)],
           foreground=[("disabled", faint), ("readonly", ink)],
           background=[("disabled", bg), ("pressed", pal["hover"]), ("active", pal["hover"])],
           arrowcolor=[("disabled", faint)],
           selectbackground=[("!focus", field)], selectforeground=[("!focus", ink)])
    st.configure("TSeparator", background=pal["border"])
    # the drop-down list of a Combobox is a classic Listbox in a Toplevel, made when first opened: the option
    # database colours it then (Themer.paint recolours the ones already made)
    for opt, role in (("background", "field"), ("foreground", "ink"), ("selectBackground", "select_bg"),
                      ("selectForeground", "select_fg"), ("disabledForeground", "faint"),
                      ("highlightBackground", "border"), ("highlightColor", "accent")):
        root.option_add(f"*TCombobox*Listbox.{opt}", pal[role])
    for opt, role in (("background", "field"), ("highlightBackground", "field"), ("highlightColor", "accent")):
        root.option_add(f"*ComboboxPopdown.{opt}", pal[role])
    root.option_add("*TCombobox*Listbox.font", "PL.body")


# ---------------------------------------------------------------- classic Tk widgets
ZONES = {  # zone -> (surface, text, tick box)
    "page": ("bg", "ink", "field"), "panel": ("panel", "panel_fg", "panel"),
    "field": ("field", "ink", "field"), "tip": ("tip_bg", "tip_fg", "tip_bg"),
}
_COMMON = {"highlightbackground": "@surface", "highlightcolor": "accent"}
_LABEL = {"background": "@surface", "foreground": "@text", "activebackground": "@surface",
          "activeforeground": "@text", "disabledforeground": "faint"}
_TEXTBOX = {"background": "field", "foreground": "ink", "insertbackground": "ink", "selectbackground": "select_bg",
            "selectforeground": "select_fg", "inactiveselectbackground": "select_bg", "disabledbackground": "bg",
            "disabledforeground": "faint", "readonlybackground": "field", "buttonbackground": "heading_bg",
            "activebackground": "hover", "highlightbackground": "edge"}
CLASS_ROLES = {
    "Tk": {"background": "@surface"}, "Toplevel": {"background": "@surface"},
    "ComboboxPopdown": {"background": "field"},
    "Frame": {"background": "@surface"}, "Panedwindow": {"background": "@surface", "proxybackground": "border"},
    "Labelframe": {"background": "@surface", "foreground": "@text"},
    "Label": _LABEL, "Message": _LABEL,
    "Button": {"background": "button", "foreground": "on_button", "activebackground": "button",
               "activeforeground": "on_button", "disabledforeground": "faint"},
    "Checkbutton": dict(_LABEL, selectcolor="@box"), "Radiobutton": dict(_LABEL, selectcolor="@box"),
    "Entry": _TEXTBOX, "Spinbox": _TEXTBOX, "Text": _TEXTBOX, "Listbox": _TEXTBOX,
    "Canvas": {"background": "field", "selectbackground": "select_bg", "selectforeground": "select_fg",
               "insertbackground": "ink"},
    "Scrollbar": {"background": "thumb", "troughcolor": "trough", "activebackground": "thumb_active"},
    "Scale": {"background": "thumb", "troughcolor": "trough", "activebackground": "hover", "foreground": "@text"},
    "Menu": {"background": "field", "foreground": "ink", "activebackground": "select_bg",
             "activeforeground": "select_fg", "selectcolor": "ink", "disabledforeground": "faint"},
}


class Themer:
    """Colours classic Tk widgets by role. ttk widgets are left to style_ttk."""

    def __init__(self, pal):
        self.pal = pal
        self.roles = {}          # widget path -> {"@zone": zone, option: role}
        self._keys = {}          # widget class -> its option names

    def register(self, widget, zone=None, **roles):
        d = self.roles.setdefault(str(widget), {})
        if zone:
            d["@zone"] = zone
        d.update(roles)
        return widget

    def set(self, widget, zone=None, **roles):
        self.register(widget, zone, **roles)
        self.paint(widget)
        return widget

    def zone_of(self, widget):
        w = widget
        while w is not None:
            z = self.roles.get(str(w), {}).get("@zone")
            if z:
                return z
            w = getattr(w, "master", None)
        return "page"

    def paint(self, widget):
        """Recolour widget and everything inside it - also widgets Tk made itself (a Combobox drop-down list),
        which Python does not list as children."""
        tk_ = widget.tk
        self._paint(tk_, str(widget), self.zone_of(widget))
        if str(widget) == ".":           # the whole window: forget widgets that are gone
            self.roles = {p: r for p, r in self.roles.items() if int(tk_.call("winfo", "exists", p))}

    def _keys_of(self, tk_, path, cls):
        keys = self._keys.get(cls)
        if keys is None:
            keys = self._keys[cls] = {str(tk_.splitlist(o)[0])[1:] for o in tk_.splitlist(tk_.call(path, "configure"))
                                      if len(tk_.splitlist(o)) == 5}
        return keys

    def _paint(self, tk_, path, zone):
        own = self.roles.get(path, {})
        zone = own.get("@zone", zone)
        cls = str(tk_.call("winfo", "class", path))
        table = CLASS_ROLES.get(cls)
        if table is not None:
            keys = self._keys_of(tk_, path, cls)
            surface, text, box = ZONES.get(zone, ZONES["page"])
            special = {"@surface": surface, "@text": text, "@box": box}
            args = []
            for opt, role in {**_COMMON, **table, **{k: v for k, v in own.items() if k != "@zone"}}.items():
                if opt in keys:
                    args += ["-" + opt, self.pal[special.get(role, role)]]
            if args:
                tk_.call(path, "configure", *args)
        for child in tk_.splitlist(tk_.call("winfo", "children", path)):
            self._paint(tk_, str(child), zone)
