#!/usr/bin/env python3
"""
plparse.py - Microsoft Phone Link (Your Phone) correlating parser.

Reads the nine per-device SQLite databases plus the FullTrust app-streaming
database, correlates them (the databases hold NO foreign keys between files),
and writes CSVs, a unified timeline and a single self-contained HTML report.

Usage:
    python plparse.py --input "<path>" --out "<dir>" [--case "name"] [--digits 10]

--input may be any of:
    ...\\Packages\\Microsoft.YourPhone_8wekyb3d8bbwe          (package root)
    ...\\LocalCache\\Indexed                                  (all devices)
    ...\\LocalCache\\Indexed\\{GUID}                          (one device)
    a directory holding the .db files themselves               (a KAPE/triage dump)

Design notes that matter forensically:
  * Source files are never opened in place. Every .db is copied WITH its -wal
    and -shm into a working directory, then hashed. If the WAL is left behind
    the data is lost silently, so the copy is explicit and reported.
  * Timestamps: most tables use Windows FILETIME; phoneapps.phone_apps uses
    Unix milliseconds. The converter detects by magnitude and RECORDS which
    rule it applied, per value, in the *_epoch column.
  * Correlation is done here, not by the data. Every joined name is an
    inference by this tool and is labelled as such in the output.
  * FullTrust\\phone_apps.db is PACKAGE-WIDE: one file shared by every device
    folder. It is staged, hashed and carved once. A row of it (live or carved) is
    credited to a device ONLY when its hosted_app_ids.device_id equals that
    device's folder GUID (case and braces ignored). Rows whose device_id is blank,
    unknown or another phone's are never written into a device's outputs or
    counts; they go to <out>\\_package\\ (carved_records.csv with scope=package,
    fulltrust_hosted_app_ids.csv with the folder each live row was credited to).
    Its vacant-rowid counter (sqlite_sequence) cannot be split by device, so it is
    reported only in _package\\deleted_evidence.csv. A device whose label is not a
    GUID (a plain folder of .db files) receives no FullTrust row at all, and its
    warnings say so. Per-device carved rows carry scope=device.
  * DeviceMetadataStorage.json is also package-wide. Each device's report and
    acquisition.json get only the entry/entries whose Metadata.Id equals the
    folder GUID (verified on real data: Metadata.Id is the pairing folder's
    GUID, and Phone Link removes a phone's entry when it is unlinked), or the
    note "no DeviceMetadataStorage.json entry has this folder's GUID as its Id";
    the other entries are listed separately under "Other entries in
    DeviceMetadataStorage.json (their Id is not this folder's GUID)" - nothing
    more is claimed about them. (pldemo's synthetic package is being corrected
    separately to use the folder GUID as Id.) Both
    package-wide inputs are hashed and recorded under "package_files" in every
    acquisition.json and in _package\\acquisition.json.
  * Recoverability is judged from the file: freelist_count and whether the free
    pages still hold non-zero bytes. PRAGMA secure_delete is never reported - it
    is the analysing connection's setting, not a property of the evidence file.

Stdlib only. Read-only with respect to the evidence.
"""

import argparse, csv, hashlib, html, json, os, pathlib, re, shutil, sqlite3, struct, sys, tempfile
try:
    import plcarve
except ImportError:
    plcarve = None
from datetime import datetime, timedelta, timezone

TOOL = "plparse"
VERSION = "1.0"

DB_NAMES = ["phone.db", "calling.db", "contacts.db", "photos.db", "phoneapps.db",
            "notifications.db", "continuity.db", "deviceData.db", "settings.db"]
FULLTRUST_DB = "phone_apps.db"

CALL_TYPE = {1: "Incoming", 2: "Outgoing", 3: "Missed", 4: "Voicemail",
             5: "Rejected", 6: "Blocked", 7: "AnsweredElsewhere"}
MSG_TYPE = {1: "Inbox", 2: "Sent", 3: "Draft", 4: "Outbox", 5: "Failed", 6: "Queued"}

FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------- timestamps
def conv_time(v):
    """Return (iso_utc, rule). Magnitude decides; the rule is recorded, never assumed."""
    if v is None:
        return ("", "null")
    try:
        v = int(v)
    except (TypeError, ValueError):
        return ("", "unparsed")
    if v <= 0:
        return ("", "zero")
    try:
        if v > 10 ** 16:                       # ~1.3e17 on a current host
            return ((FILETIME_EPOCH + timedelta(microseconds=v // 10)).isoformat(), "FILETIME")
        if v > 10 ** 11:                       # ~1.8e12
            return ((UNIX_EPOCH + timedelta(milliseconds=v)).isoformat(), "UnixMillis")
        if v > 10 ** 8:                        # ~1.8e9
            return ((UNIX_EPOCH + timedelta(seconds=v)).isoformat(), "UnixSeconds")
    except (OverflowError, OSError, ValueError):
        return ("", "out-of-range")
    return ("", "unrecognised-magnitude")


# ---------------------------------------------------------------- addresses
SIP_RE = re.compile(r"^sip:([^@]+)@", re.I)


def norm_addr(a, digits=10):
    """
    Return (key, kind, display).

    kind: phone | short | alpha | rcs-agent | empty
    Matching is on the LAST n digits because the same number appears as
    +15550100123, 15550100123 and 5550100123 across these databases.
    """
    if a is None:
        return ("", "empty", "")
    a = str(a).strip()
    if not a:
        return ("", "empty", "")
    m = SIP_RE.match(a)
    if m:
        return (a.lower(), "rcs-agent", m.group(1).replace("_", " ").strip())
    d = re.sub(r"\D", "", a)
    if not d:
        return (a.upper(), "alpha", a)          # e.g. AB-EXAMPL-S (bank sender ID)
    if len(d) >= digits:
        return (d[-digits:], "phone", a)
    return (d, "short", a)                      # 5-6 digit shortcode


# ---------------------------------------------------------------- discovery
def find_device_dirs(root):
    """Return [(label, dir_with_dbs)]. Handles all four accepted --input shapes."""
    root = os.path.abspath(root)
    out = []

    def has_dbs(d):
        try:
            return any(f in os.listdir(d) for f in DB_NAMES)
        except OSError:
            return False

    if has_dbs(root):
        out.append((os.path.basename(root.rstrip("\\/")) or "root", root))
        return out
    own = os.path.join(root, "System", "Database")       # a device folder: Indexed\{GUID}
    if has_dbs(own):
        out.append((os.path.basename(root.rstrip("\\/")) or "root", own))
        return out

    cands = [root,
             os.path.join(root, "LocalCache", "Indexed"),
             os.path.join(root, "Indexed")]
    for base in cands:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            sub = os.path.join(base, name)
            if not os.path.isdir(sub):
                continue
            for probe in (os.path.join(sub, "System", "Database"), sub):
                if has_dbs(probe):
                    out.append((name, probe))
                    break
        if out:
            return out
    return out


def find_fulltrust(root):
    root = os.path.abspath(root)
    for base in (root,
                 os.path.join(root, "LocalCache", "Indexed", "FullTrust"),
                 os.path.join(root, "Indexed", "FullTrust"),
                 os.path.join(os.path.dirname(root), "FullTrust")):
        p = os.path.join(base, FULLTRUST_DB)
        if os.path.isfile(p):
            return p
    return None


_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
GUID_RE = re.compile(r"^(?:\{%s\}|%s)$" % (_GUID, _GUID))      # bare, or in a pair of braces


def is_guid(v):
    return bool(GUID_RE.match(str(v or "").strip()))


def norm_guid(v):
    """THE normaliser for device ids / GUIDs in this engine: surrounding space and one pair of braces dropped,
    lower case. Every comparison of a device id or GUID goes through same_guid (or this)."""
    t = str(v or "").strip()
    if len(t) >= 2 and t[0] == "{" and t[-1] == "}":
        t = t[1:-1].strip()
    return t.lower()


def same_guid(a, b):
    """True when a and b are both GUIDs (bare or braced) and the same one, case ignored."""
    return is_guid(a) and is_guid(b) and norm_guid(a) == norm_guid(b)


def owns_ft_row(device_id, label):
    """
    True only when a package-wide FullTrust row names this pairing folder: the folder label
    is a GUID and the row's device_id is the same GUID (case and braces ignored). A blank,
    unknown or other device_id, or a label that is not a GUID, never matches.
    """
    return same_guid(device_id, label)


def _inside(path, folder):
    """Is path a folder strictly beneath folder? (case-insensitive, as on Windows)"""
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    try:
        return a != b and os.path.commonpath([a, b]) == b
    except ValueError:                       # different drives
        return False


def locate_metadata(root, device_dirs=None):
    """
    (path, note) of the package's DeviceMetadataStorage.json. It is accepted ONLY at the LocalCache level of a
    recognised Phone Link layout: ...\\LocalCache\\DeviceMetadataStorage.json with an Indexed folder beside it that
    contains every device folder being parsed. Looked for at --input, --input\\LocalCache and the folders above
    --input. A JSON found elsewhere (e.g. above a plain folder of .db files) is not used, and the note says where it
    was; path is then None.
    """
    root = os.path.abspath(root)
    if device_dirs is None:
        device_dirs = [d for _, d in find_device_dirs(root)]
    cands, p = [root, os.path.join(root, "LocalCache")], root
    for _ in range(6):
        p = os.path.dirname(p)
        cands.append(p)
    rejected = []
    for lc in dict.fromkeys(cands):
        j = os.path.join(lc, META_FILE)
        if not os.path.isfile(j):
            continue
        idx = os.path.join(lc, "Indexed")
        if (os.path.basename(lc).lower() == "localcache" and os.path.isdir(idx) and device_dirs
                and all(_inside(d, idx) for d in device_dirs)):
            return j, ""
        if j not in rejected:
            rejected.append(j)
    if not rejected:
        return None, ""
    return None, ("%s found at %s but not used: it is not at the LocalCache level of a Phone Link layout "
                  "(...\\LocalCache\\%s beside an Indexed folder that holds the device folder), so it cannot be "
                  "tied to this input." % (META_FILE, "; ".join(rejected), META_FILE))


def find_metadata(root, device_dirs=None):
    """Path of the package's DeviceMetadataStorage.json, or None (see locate_metadata)."""
    return locate_metadata(root, device_dirs)[0]


META_FILE = "DeviceMetadataStorage.json"
NO_META_MATCH = ("no DeviceMetadataStorage.json entry has this folder's GUID as its Id (Phone Link removes a phone's "
                 "entry when it is unlinked). The file's other entries are listed separately.")
OTHER_META_HEADING = "Other entries in DeviceMetadataStorage.json (their Id is not this folder's GUID)"


def read_metadata(meta_src, pkg_acq):
    """
    Read DeviceMetadataStorage.json (package-wide: one entry list per hardware ID, for every
    phone ever paired). The bytes are read once, hashed, and those same bytes parsed, so the
    recorded hash is of exactly what was read. Returns (entries, error).
    """
    if not meta_src:
        return [], ""
    try:
        with open(meta_src, "rb") as fh:
            blob = fh.read()
    except OSError as e:
        pkg_acq.append({"file": META_FILE, "status": "unreadable", "source": meta_src, "error": str(e)})
        return [], "could not read %s: %s" % (META_FILE, e)
    pkg_acq.append({"file": META_FILE, "status": "read (not copied - parsed from these bytes)",
                    "source": meta_src, "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})
    entries = []
    try:
        raw = json.loads(blob.decode("utf-8-sig"))
        for hw, lst in (raw.get("DeviceMetadatas") or {}).items():
            for e in lst or []:
                m = e.get("Metadata", {}) or {}
                inner = m.get("Metadata", {}) or {}
                entries.append({
                    "id": m.get("Id", ""), "hardware_id": hw, "linked": e.get("IsLinked"),
                    # Microsoft ships this key misspelled; read both.
                    "registered": m.get("RegitrationTime") or m.get("RegistrationTime"),
                    "last_seen": m.get("LastSeenTime"),
                    "os": "%s %s" % (inner.get("OsName", ""), inner.get("OsVersion", "")),
                    "make": inner.get("Manufacture"), "model": inner.get("ModelName"),
                    "client": inner.get("ClientType"), "client_ver": inner.get("ClientVersion"),
                    "capabilities": inner.get("Capabilities"),
                })
    except (ValueError, TypeError, AttributeError) as e:
        return [], "could not parse %s: %s: %s" % (META_FILE, type(e).__name__, e)
    return entries, ""


def device_metadata(label, entries, error, meta_src, locate_note=""):
    """
    Split the package-wide metadata into this device's entry/entries (Metadata.Id equal to
    the folder GUID) and everyone else's. Returns (this_device, package_other_entries).
    """
    mine = [e for e in entries if same_guid(e.get("id"), label)]
    others = [e for e in entries if e not in mine]
    if not meta_src:
        note = "%s was not found; no device metadata available." % META_FILE
        if locate_note:
            note += " " + locate_note
    elif error:
        note = error
    elif mine:
        note = "%d entry(ies) whose Metadata.Id equals this device folder's GUID." % len(mine)
    else:
        note = NO_META_MATCH
        if not is_guid(label):
            note += " (Device label %r is not a pairing-folder GUID, so no entry can be matched.)" % label
    return ({"source": META_FILE, "match_rule": "Metadata.Id == device folder GUID",
             "entries": mine, "note": note}, others)


# ---------------------------------------------------------------- acquisition
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ro_uri(path):
    """SQLite read-only URI for a path. Escapes '#', '%', '?' and spaces so any folder name opens correctly."""
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"


SNAPSHOT_TRIES = 3
UNSTABLE = "NO - source changed during copy"


def _source_state(src):
    """Hashes of the source .db and -wal. The -shm is a shared-memory index and is left out."""
    w = src + "-wal"
    return (sha256(src), sha256(w) if os.path.isfile(w) else "")


def stage(src_dir, work, acq, names=DB_NAMES):
    """
    Copy every .db WITH its -wal/-shm. Report what was and was not found.

    A live database can change while its three files are copied one after another, which
    would give a .db and a -wal from different moments. The .db and -wal are hashed before
    and after each set; if either changed the whole set is copied again (3 tries). A set
    that never settles is kept, and its row says so: source_stable = UNSTABLE. Such a row
    holds no source hash (sha256 is ""); the hashes of the copy actually taken are
    copy_sha256 / copy_wal_sha256 / copy_shm_sha256.

    The -shm never gets a source hash, stable or not: it is a shared-memory index that a running Phone Link
    rewrites constantly, so the source can differ from the copy a moment later. Its row carries copy_shm_sha256 only.

    Every copy is re-hashed after it is taken: copy_sha256 (and copy_wal_sha256 for a stable set) are the hashes
    of the copy, and copy_matches says "yes" when they equal the source hashes, "NO - ..." when they do not (see
    copy_mismatch_warnings), or "n/a - source changed during copy" for an unstable set.
    """
    os.makedirs(work, exist_ok=True)
    staged = {}
    for name in names:
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            acq.append({"file": name, "status": "absent"})
            continue
        dst = os.path.join(work, name)
        stable = False
        for _ in range(SNAPSHOT_TRIES):
            before = _source_state(src)
            shutil.copy2(src, dst)
            for suf in ("-wal", "-shm"):
                if os.path.isfile(src + suf):
                    shutil.copy2(src + suf, dst + suf)
                elif os.path.isfile(dst + suf):
                    os.remove(dst + suf)         # left over from an earlier try
            if _source_state(src) == before:
                stable = True
                break
        row = {"file": name, "status": "copied", "bytes": os.path.getsize(dst),
               "sha256": before[0] if stable else "",
               "wal": "", "shm": "", "wal_sha256": "", "shm_sha256": "",
               "source_stable": "yes" if stable else UNSTABLE}
        # The copy is hashed in its own right. Unstable: no source hash settled, so none is claimed and the copy's
        # hash is kept under its own name. Stable: the copy must equal the source hash taken around the copy.
        row["copy_sha256"] = sha256(dst)
        bad = []
        if stable and row["copy_sha256"] != before[0]:
            bad.append(name)
        for suf in ("-wal", "-shm"):
            if os.path.isfile(dst + suf):
                k = suf[1:]
                row[k] = os.path.getsize(dst + suf)
                # The WAL can hold most of the evidence, so it needs a hash too.
                if not stable:
                    row["copy_" + k + "_sha256"] = sha256(dst + suf)
                elif suf == "-wal":
                    row[k + "_sha256"] = before[1]
                    row["copy_wal_sha256"] = sha256(dst + suf)
                    if row["copy_wal_sha256"] != before[1]:
                        bad.append(name + suf)
                else:
                    # The -shm is a shared-memory index the running app rewrites constantly, so a hash of the source
                    # taken now need not be the bytes that were copied. Only the copy's own hash is recorded, and
                    # the -shm is not verified against the source (the .db and -wal are).
                    row["copy_" + k + "_sha256"] = sha256(dst + suf)
        if not stable:
            row["copy_matches"] = "n/a - source changed during copy"
        elif bad:
            row["copy_matches"] = "NO - " + ", ".join(bad) + " copy does not match the source hash"
        else:
            row["copy_matches"] = "yes"
        acq.append(row)
        staged[name] = dst
    return staged


def copy_mismatch_warnings(acq):
    """One warning per staged file whose copy does not match the source hash taken around the copy."""
    return ["Staged copy of %s does not match its source hash (source %s, copy %s): the copy is not a faithful "
            "image of the evidence - re-acquire before relying on its results."
            % (r.get("file"), (r.get("sha256") or "")[:16], (r.get("copy_sha256") or "")[:16])
            for r in acq if str(r.get("copy_matches", "")).startswith("NO")]


def deleted_evidence(path, dbname):
    """
    AUTOINCREMENT tables keep a high-water mark in sqlite_sequence. A mark above the
    live row count means rowids were issued that are no longer occupied.

    READ THIS BEFORE QUOTING IT. It is NOT a count of user deletions:
      * Phone Link re-syncs wholesale - it deletes and reinserts rows routinely, so
        churn is the ordinary explanation, not a person deleting something.
      * Some tables carry app-assigned rowids (huge, non-sequential values). The
        subtraction is meaningless there and is reported as such, not as a number.
      * content_sequence and _change_notifications are plumbing, rewritten constantly.
        They are excluded entirely.
    What it IS good for: telling you a table once held more rows than it does now,
    so its free pages are worth carving.
    """
    SKIP = {"content_sequence", "_change_notifications"}
    APP_ASSIGNED = 10 ** 12          # beyond any plausible insert counter here
    out = []
    try:
        c = sqlite3.connect(ro_uri(path), uri=True)
        try:
            seqs = c.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
        except sqlite3.Error:
            c.close()
            return out
        for name, seq in seqs:
            if name in SKIP or name.startswith("_"):
                continue
            try:
                live = c.execute('SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
            except sqlite3.Error:
                continue
            seq = seq or 0
            if seq >= APP_ASSIGNED:
                out.append({"db": dbname, "table": name, "live_rows": live,
                            "highwater_seq": seq, "rowids_unused": "",
                            "classification": "app-assigned-rowids",
                            "note": "Counter is not a sequential insert count - no deletion "
                                    "inference can be drawn from it."})
                continue
            gap = seq - live
            if gap > 0:
                out.append({"db": dbname, "table": name, "live_rows": live,
                            "highwater_seq": seq, "rowids_unused": gap,
                            "classification": "churn-or-deletion",
                            "note": "%s rowid(s) issued and now vacant. Sync churn is the ordinary "
                                    "cause; user deletion is only one possibility. Treat as a reason "
                                    "to carve free pages, not as proof of deletion." % gap})
        c.close()
    except sqlite3.Error:
        pass
    return out


def free_page_residue(path):
    """
    Free pages in the main file that still hold non-zero bytes (for a freelist trunk page,
    bytes after its pointer array). Returns None when the file cannot be read as SQLite.

    This, not PRAGMA secure_delete, is what says whether free pages are worth carving.
    secure_delete is a setting of the CONNECTION that runs the PRAGMA - the analysis
    host's SQLite build default - and is not stored in the file: a database written with
    secure_delete ON (free pages zero-filled) reads back 0 on a build whose default is off.
    Cheap check: main file only (not the -wal), and freeblocks inside live pages are not
    examined - zero here does not mean nothing can be carved from freeblocks or the WAL.
    """
    try:
        with open(path, "rb") as fh:
            hdr = fh.read(100)
            if len(hdr) < 100 or hdr[:16] != b"SQLite format 3\x00":
                return None
            ps = struct.unpack(">H", hdr[16:18])[0]
            ps = 65536 if ps == 1 else ps
            size = os.fstat(fh.fileno()).st_size

            def page(n):
                fh.seek((n - 1) * ps)
                return fh.read(ps)

            hits, seen = 0, set()
            trunk = struct.unpack(">I", hdr[32:36])[0]
            while trunk and trunk not in seen and trunk * ps <= size:
                seen.add(trunk)
                data = page(trunk)
                nxt, cnt = struct.unpack(">II", data[:8])
                cnt = min(cnt, (ps - 8) // 4)
                if data[8 + 4 * cnt:].strip(b"\x00"):
                    hits += 1
                for i in range(cnt):
                    leaf = struct.unpack(">I", data[8 + 4 * i:12 + 4 * i])[0]
                    if leaf and leaf not in seen and leaf * ps <= size:
                        seen.add(leaf)
                        if page(leaf).strip(b"\x00"):
                            hits += 1
                trunk = nxt
            return hits
    except (OSError, struct.error):
        return None


def db_health(path):
    """
    PRAGMAs that decide whether the evidence is complete and carvable, plus what the FILE
    shows about its free pages (free_pages_with_residue). PRAGMA secure_delete is
    deliberately not reported: it describes the analysis host's connection, not the file.
    """
    out = {}
    try:
        c = sqlite3.connect(ro_uri(path), uri=True)
        for p in ("journal_mode", "page_count", "freelist_count", "page_size"):
            try:
                out[p] = c.execute("PRAGMA " + p).fetchone()[0]
            except sqlite3.Error:
                out[p] = "?"
        c.close()
    except sqlite3.Error as e:
        out["error"] = str(e)
    res = free_page_residue(path)
    out["free_pages_with_residue"] = "?" if res is None else res
    return out


# Every query failure is recorded here. An empty result and a failed query look
# identical to the caller, so a forensic tool must not let them look identical in
# the report: "0 messages" must never silently mean "the query blew up".
QUERY_ERRORS = []
# Columns a query's rows were asked for but this copy's table does not have ("file: table.column"). A Phone Link
# version can lack a column; its field is then blank and the run says so once, instead of ending on a KeyError.
MISSING_COLUMNS = []
_FROM_RE = re.compile(r"\bFROM\s+\"?(\w+)", re.I)
_NO_COL_RE = re.compile(r"no such column: (?:\w+\.)?(\w+)")


class _Row(dict):
    """A query row whose missing column reads as None (and is recorded once in MISSING_COLUMNS)."""
    __slots__ = ("_where",)

    def __missing__(self, col):
        miss = "%s: %s.%s" % (self._where[0], self._where[1], col)
        if miss not in MISSING_COLUMNS:
            MISSING_COLUMNS.append(miss)
        return None


def missing_columns_warning():
    """One warning naming every column that was missing, '' when none was."""
    if not MISSING_COLUMNS:
        return ""
    return ("%d column(s) the parser reads are not in this copy of the database (another Phone Link version?), "
            "so those fields are blank: %s" % (len(MISSING_COLUMNS), "; ".join(MISSING_COLUMNS)))


def q(path, sql, params=()):
    """Query helper: missing table or file yields [] AND records why. Rows are dicts in which a column this copy
    lacks reads as None and is recorded in MISSING_COLUMNS; an ORDER BY on such a column is dropped, not fatal."""
    if not path:
        return []          # that database is not in this folder: stated once per device, not a failed read
    if not os.path.isfile(path):
        QUERY_ERRORS.append({"file": os.path.basename(path), "sql": sql.strip()[:120],
                             "error": "file not present"})
        return []
    m = _FROM_RE.search(sql)
    where = (os.path.basename(path), m.group(1) if m else "?")

    def wrap(r):
        row = _Row(r)
        row._where = where
        return row
    try:
        c = sqlite3.connect(ro_uri(path), uri=True)
        c.row_factory = sqlite3.Row
        try:
            try:
                cur = c.execute(sql, params)
            except sqlite3.OperationalError as e:
                # The ORDER BY column is not in this version of the table: read the rows unordered rather than none.
                gone = _NO_COL_RE.search(str(e))
                head, sep, order = sql.rpartition(" ORDER BY ")
                if not (gone and sep and gone.group(1) in order):
                    raise
                miss = "%s: %s.%s" % (where[0], where[1], gone.group(1))
                if miss not in MISSING_COLUMNS:
                    MISSING_COLUMNS.append(miss)
                cur = c.execute(head, params)
            rows = [wrap(r) for r in cur]
        finally:
            c.close()
        return rows
    except sqlite3.Error as e:
        QUERY_ERRORS.append({"file": os.path.basename(path), "sql": sql.strip()[:120],
                             "error": "%s: %s" % (type(e).__name__, e)})
        return []


# ---------------------------------------------------------------- parsing
def build_contact_index(db, digits):
    """key -> {name, contact_id, label}. The basis of every name in the output."""
    idx = {}
    for r in q(db, """SELECT p.phone_number, p.display_phone_number, p.label,
                             c.contact_id, c.display_name, c.company
                      FROM phonenumber p LEFT JOIN contact c ON c.contact_id = p.contact_id"""):
        k, kind, _ = norm_addr(r["phone_number"], digits)
        if not k:
            continue
        idx.setdefault(k, {"name": r["display_name"] or "",
                           "contact_id": r["contact_id"],
                           "company": r["company"] or "",
                           "label": r["label"] or ""})
    return idx


GROUP_SEP = re.compile(r"[,;]|\s{2,}")


def resolve(addr, idx, digits):
    """Return (display, name, kind, matched). 'matched' is what makes it an inference."""
    # A multi-recipient thread must NOT be collapsed to one contact: norm_addr would
    # strip separators and key on a splice of the last recipient's digits, which can
    # match a real person and put their name on someone else's group thread.
    if addr and GROUP_SEP.search(str(addr)):
        parts = [x for x in GROUP_SEP.split(str(addr)) if x.strip()]
        names = []
        for prt in parts:
            pk, pkind, pdisp = norm_addr(prt, digits)
            hit = idx.get(pk)
            names.append(hit["name"] if hit and hit["name"] else pdisp)
        return (" | ".join(p.strip() for p in parts), " | ".join(names), "group",
                "contacts.db (per-member)")
    k, kind, disp = norm_addr(addr, digits)
    if kind == "rcs-agent":
        return (disp, "", kind, "rcs-business-agent")
    hit = idx.get(k)
    if hit and hit["name"]:
        return (disp, hit["name"], kind, "contacts.db")
    return (disp, "", kind, "")


def parse_all(dbs, ft_db, idx, digits, device_label=None):
    d = {}

    # --- calls -------------------------------------------------------------
    calls = []
    for r in q(dbs.get("calling.db"), "SELECT * FROM call_history ORDER BY start_time DESC"):
        iso, rule = conv_time(r["start_time"])
        sync, _ = conv_time(r.get("last_updated_time"))
        disp, name, kind, src = resolve(r["phone_number"], idx, digits)
        calls.append({"start_utc": iso, "start_epoch": rule,
                      "direction": CALL_TYPE.get(r["call_type"], "type=%s" % r["call_type"]),
                      "number": disp, "addr_kind": kind,
                      "contact_name": name, "name_source": src,
                      "duration_sec": r["duration"],
                      "is_read": r["is_read"], "sim_account": r.get("phone_account_id") or "",
                      "synced_utc": sync, "call_id": r["call_id"]})
    d["calls"] = calls

    # --- SIM ---------------------------------------------------------------
    sims = {}
    for r in q(dbs.get("phone.db"), "SELECT * FROM subscription"):
        sims[r["subscription_id"]] = r
    d["sims"] = [{"subscription_id": k, "own_number": v.get("number") or "",
                  "carrier": v.get("name") or "", "country": v.get("country_iso") or "",
                  "sim_slot": v.get("sim_slot_index"), "roaming": v.get("is_roaming"),
                  "default_sms": v.get("is_default_sms_subscription"),
                  "default_voice": v.get("is_default_voice_subscription"),
                  "rcs_supported": v.get("is_rcs_supported")} for k, v in sims.items()]

    # --- threads -----------------------------------------------------------
    parts = {}
    for r in q(dbs.get("phone.db"), "SELECT thread_id, normalized_address, send_address FROM participant"):
        parts.setdefault(r["thread_id"], []).append(r["normalized_address"] or r["send_address"])

    bodies = {}
    errs_before = len(QUERY_ERRORS)
    for r in q(dbs.get("phone.db"), "SELECT thread_id, COUNT(*) n FROM message GROUP BY thread_id"):
        bodies[r["thread_id"]] = r["n"]
    # A failed read of the messages is not "no messages": then nothing can be said about aging out.
    bodies_read = len(QUERY_ERRORS) == errs_before

    threads = []
    for r in q(dbs.get("phone.db"), "SELECT * FROM conversation ORDER BY timestamp DESC"):
        iso, rule = conv_time(r["timestamp"])
        disp, name, kind, src = resolve(r["recipient_list"], idx, digits)
        threads.append({"thread_id": r["thread_id"], "counterparty": disp,
                        "addr_kind": kind, "contact_name": name, "name_source": src,
                        "last_activity_utc": iso, "epoch": rule,
                        "msg_count_reported": r["msg_count"],
                        "bodies_retained": bodies.get(r["thread_id"], 0) if bodies_read else "",
                        "content_aged_out": "YES" if bodies_read and bodies.get(r["thread_id"], 0) == 0 else "",
                        "unread_pc": r["unread_count"], "unread_phone": r.get("phone_unread_count"),
                        "has_rcs": r["has_rcs"],
                        "participants": " | ".join(parts.get(r["thread_id"], []))})
    d["threads"] = threads
    tmap = {t["thread_id"]: t for t in threads}

    # --- messages ----------------------------------------------------------
    msgs = []
    for r in q(dbs.get("phone.db"), "SELECT * FROM message ORDER BY timestamp DESC"):
        iso, rule = conv_time(r["timestamp"])
        t = tmap.get(r["thread_id"], {})
        # to_address is empty on every observed row: direction comes from type.
        direction = MSG_TYPE.get(r["type"], "type=%s" % r["type"])
        counter = r["from_address"] if direction == "Inbox" and r["from_address"] else t.get("counterparty", "")
        disp, name, kind, src = resolve(counter, idx, digits)
        sim = sims.get(r["subscription_id"], {})
        msgs.append({"timestamp_utc": iso, "epoch": rule, "direction": direction,
                     "counterparty": disp, "addr_kind": kind,
                     "contact_name": name or t.get("contact_name", ""),
                     "name_source": src or t.get("name_source", ""),
                     "thread_id": r["thread_id"], "subject": r.get("subject") or "",
                     "body": (r["body"] or "").replace("\r", " ").replace("\n", " "),
                     "body_len": len(r["body"] or ""),
                     "own_number": sim.get("number", ""), "carrier": sim.get("name", ""),
                     "pc_status": r.get("pc_status"), "message_id": r["message_id"]})
    d["messages"] = msgs

    # --- RCS ---------------------------------------------------------------
    rcs = []
    for r in q(dbs.get("phone.db"), "SELECT * FROM rcs_chat ORDER BY timestamp DESC"):
        iso, rule = conv_time(r["timestamp"])
        disp, name, kind, src = resolve(r["from_address"], idx, digits)
        rcs.append({"timestamp_utc": iso, "epoch": rule, "thread_id": r["thread_id"],
                    "from": disp, "contact_name": name, "addr_kind": kind,
                    "content_type": r["content_type"], "body": (r["body"] or "").replace("\n", " "),
                    "session_id": r["session_id"], "message_id": r["message_id"]})
    d["rcs"] = rcs

    # --- MMS parts (attachments) -------------------------------------------
    att = []
    for r in q(dbs.get("phone.db"), "SELECT * FROM mms_part"):
        att.append({"message_id": r["message_id"], "name": r.get("name") or "",
                    "content_type": r.get("content_type") or "", "size": r.get("size"),
                    "blob_bytes": len(r["blob"]) if r.get("blob") else 0,
                    "text": (r.get("text") or "")[:200]})
    for r in q(dbs.get("phone.db"), "SELECT * FROM rcs_filetransfer"):
        iso, _ = conv_time(r["timestamp"])
        att.append({"message_id": r["message_id"], "name": r.get("name") or "",
                    "content_type": r.get("content_type") or "", "size": r.get("size"),
                    "blob_bytes": len(r["blob"]) if r.get("blob") else 0, "text": "RCS " + iso})
    d["attachments"] = att

    # --- outbound composed ON THE PC ---------------------------------------
    d["sent_from_pc"] = q(dbs.get("phone.db"), "SELECT * FROM sending_message")

    # --- contacts ----------------------------------------------------------
    emails, addrs = {}, {}
    for r in q(dbs.get("contacts.db"), "SELECT contact_id, address FROM emailaddress"):
        emails.setdefault(r["contact_id"], []).append(r["address"])
    for r in q(dbs.get("contacts.db"), "SELECT * FROM postaladdress"):
        addrs.setdefault(r["contact_id"], []).append(r.get("display_address") or
                                                     " ".join(filter(None, [r.get("street"), r.get("city"),
                                                                            r.get("region"), r.get("postal_code"),
                                                                            r.get("country_code")])))
    nums = {}
    for r in q(dbs.get("contacts.db"), "SELECT contact_id, phone_number, label FROM phonenumber"):
        nums.setdefault(r["contact_id"], []).append(r["phone_number"])
    cons = []
    for r in q(dbs.get("contacts.db"), "SELECT * FROM contact ORDER BY display_name"):
        iso, rule = conv_time(r.get("last_updated_time"))
        cons.append({"contact_id": r["contact_id"], "display_name": r.get("display_name") or "",
                     "given": r.get("given_name") or "", "family": r.get("family_name") or "",
                     "company": r.get("company") or "", "job_title": r.get("job_title") or "",
                     "notes": (r.get("notes") or "").replace("\n", " "),
                     "numbers": " | ".join(nums.get(r["contact_id"], [])),
                     "emails": " | ".join(emails.get(r["contact_id"], [])),
                     "addresses": " | ".join(addrs.get(r["contact_id"], [])),
                     "has_photo": "yes" if r.get("thumbnail") else "",
                     "synced_utc": iso, "epoch": rule})
    d["contacts"] = cons
    d["deleted_contact_refs"] = q(dbs.get("contacts.db"), "SELECT * FROM contact_index")

    # --- photos ------------------------------------------------------------
    pics = []
    for r in q(dbs.get("photos.db"), "SELECT * FROM media ORDER BY taken_time DESC"):
        iso, rule = conv_time(r["taken_time"])
        pics.append({"taken_utc": iso, "epoch": rule, "name": r["name"],
                     "mime": r["mime_type"], "width": r["width"], "height": r["height"],
                     "size_bytes": r["size"], "android_uri": r["uri"],
                     "thumbnail_cached": "yes" if r.get("thumbnail") else "",
                     "fullres_cached": "yes" if r.get("media") else "", "id": r["id"]})
    d["photos"] = pics

    # --- apps: installed x streamed ----------------------------------------
    # hosted_app_ids is SHARED across devices and carries device_id. Scope it, or
    # a device's streamed-app list is silently credited to every other device.
    # A row is this device's only when its device_id equals the folder GUID: blank or
    # other ids, and labels that are not GUIDs, get nothing (see owns_ft_row).
    hosted = {}
    ft_rows = q(ft_db, "SELECT * FROM hosted_app_ids")
    ft_devices = sorted({(r.get("device_id") or "") for r in ft_rows})
    for r in ft_rows:
        if device_label is not None and not owns_ft_row(r.get("device_id"), device_label):
            continue                      # another device's, or not attributable to any device
        hosted[r["package_id"]] = r
    apps = []
    seen = set()
    for r in q(dbs.get("phoneapps.db"), "SELECT * FROM phone_apps ORDER BY app_name"):
        pkg = r["package_name"]
        seen.add(pkg)
        h = hosted.get(pkg)
        iso, rule = conv_time(r.get("last_updated_time"))
        apps.append({"app_name": r["app_name"], "package": pkg, "version": r["version"],
                     "installed_on_phone": "yes",
                     "streamed_to_pc": "yes" if h else "",
                     "last_window_position": (h or {}).get("last_window_position", ""),
                     "last_volume": (h or {}).get("last_volume_level", ""),
                     "last_muted": (h or {}).get("last_is_muted_state", ""),
                     "synced_utc": iso, "epoch": rule})
    for pkg, h in sorted(hosted.items()):
        if pkg in seen:
            continue
        apps.append({"app_name": "", "package": pkg, "version": "",
                     "installed_on_phone": "NO - streamed but not in current inventory",
                     "streamed_to_pc": "yes",
                     "last_window_position": h.get("last_window_position", ""),
                     "last_volume": h.get("last_volume_level", ""),
                     "last_muted": h.get("last_is_muted_state", ""),
                     "synced_utc": "", "epoch": ""})
    d["apps"] = apps
    d["_ft_devices"] = ft_devices
    # If scoping removed every row, say so - otherwise it reads as "no apps streamed"
    if device_label and ft_rows and not hosted:
        d["_ft_scope_warning"] = ("hosted_app_ids holds %d row(s) for device_id(s) %s but none "
                                  "for this device (%s). Streamed-app correlation is empty - "
                                  "check the folder name is the device_id (case and braces ignored)."
                                  % (len(ft_rows), ", ".join(x or "(blank)" for x in ft_devices),
                                     device_label))
    appname = {a["package"]: a["app_name"] for a in apps if a["app_name"]}

    # --- notifications ------------------------------------------------------
    nots = []
    for r in q(dbs.get("notifications.db"), "SELECT * FROM notifications ORDER BY post_time DESC"):
        iso, rule = conv_time(r["post_time"])
        title = body = ""
        try:
            j = json.loads(r["json"])
            if isinstance(j, dict):
                for k in ("title", "Title", "contentTitle"):
                    if j.get(k):
                        title = str(j[k]); break
                for k in ("text", "Text", "body", "contentText", "message"):
                    if j.get(k):
                        body = str(j[k]); break
        except (ValueError, TypeError):
            pass
        nots.append({"post_utc": iso, "epoch": rule, "package": r["package_name"],
                     "app_name": appname.get(r["package_name"], ""),
                     "title": title, "text": body, "state": r["state"],
                     "json_len": len(r["json"] or ""), "notification_id": r["notification_id"]})
    d["notifications"] = nots

    # --- browser handoff ----------------------------------------------------
    hist = []
    for r in q(dbs.get("continuity.db"), "SELECT * FROM browser_history"):
        hist.append({"title": r.get("browser_history_title") or "", "uri": r.get("web_uri") or "",
                     "package": r.get("package_name") or "", "source": r.get("source") or ""})
    d["browser"] = hist

    return d


# ---------------------------------------------------------------- timeline
def build_timeline(d):
    tl = []

    def add(ts, epoch, kind, who, detail, src):
        if ts:
            tl.append({"timestamp_utc": ts, "epoch_rule": epoch, "event": kind,
                       "party": who, "detail": detail, "source": src})

    for r in d["calls"]:
        add(r["start_utc"], r["start_epoch"], "Call " + r["direction"],
            r["contact_name"] or r["number"], "%ss" % r["duration_sec"], "calling.db/call_history")
    for r in d["messages"]:
        add(r["timestamp_utc"], r["epoch"], "SMS " + r["direction"],
            r["contact_name"] or r["counterparty"],
            (r["body"][:80] + ("..." if len(r["body"]) > 80 else "")), "phone.db/message")
    for r in d["rcs"]:
        add(r["timestamp_utc"], r["epoch"], "RCS", r["contact_name"] or r["from"],
            r["body"][:80], "phone.db/rcs_chat")
    for r in d["photos"]:
        add(r["taken_utc"], r["epoch"], "Photo taken", "",
            "%s  %sx%s  %s bytes" % (r["name"], r["width"], r["height"], r["size_bytes"]),
            "photos.db/media")
    for r in d["notifications"]:
        add(r["post_utc"], r["epoch"], "Notification",
            r["app_name"] or r["package"], (r["title"] + " " + r["text"])[:80],
            "notifications.db/notifications")
    tl.sort(key=lambda x: x["timestamp_utc"], reverse=True)
    return tl


# ---------------------------------------------------------------- output
_CTRL = re.compile("[" + "".join(chr(c) for c in list(range(0, 9)) + [11, 12] + list(range(14, 32))) + "]")


def clean(v):
    """
    Carved data is partially overwritten by definition, so a recovered TEXT field
    can contain NUL and other control bytes that the csv writer rejects. Replace
    them visibly rather than dropping the row - the marker is itself a signal that
    the value is damaged.
    """
    if isinstance(v, bytes):
        return "<blob %d bytes>" % len(v)
    if isinstance(v, str) and _CTRL.search(v):
        return _CTRL.sub(chr(0xFFFD), v)
    return v


_PLAIN_NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?|\+[0-9]+")


def csv_safe(v):
    """A CSV cell a spreadsheet will show as text, never run as a formula. Names, numbers and message text come
    from the phone, so a cell starting with = + - @ (or a tab / carriage return) gets a leading apostrophe - unless
    it is exactly a number such as -5, 3.14 or +15550100. The same rule as the viewer's export."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") and not _PLAIN_NUMBER.fullmatch(v):
        return "'" + v
    return v


def write_csv(path, rows, cols=None):
    if not rows:
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerow(cols or ["(no rows)"])
        return 0
    if not cols:
        # Union of every row's keys, in first-seen order. Taking rows[0] alone
        # silently drops every column of every other shape in a mixed list -
        # which is exactly what carved_records.csv is.
        cols, seen_k = [], set()
        for r in rows:
            for k in r:
                if k not in seen_k:
                    seen_k.add(k)
                    cols.append(k)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: csv_safe(clean(v)) for k, v in r.items()})
    return len(rows)


def esc(x):
    return html.escape("" if x is None else str(x))


def html_report(out, case, dev, meta, acq, health, d, tl, counts, digits, warnings):
    """meta is (this_device_metadata, package_other_entries) from device_metadata()."""
    dev_meta, other_meta = meta

    def table(rows, cols, limit=40):
        if not rows:
            return "<p class=muted>No rows.</p>"
        h = "<table><tr>" + "".join("<th>%s</th>" % esc(c) for c in cols) + "</tr>"
        for r in rows[:limit]:
            h += "<tr>" + "".join("<td>%s</td>" % esc(r.get(c, "")) for c in cols) + "</tr>"
        h += "</table>"
        if len(rows) > limit:
            h += "<p class=muted>Showing %d of %d — full set in the CSV.</p>" % (limit, len(rows))
        return h

    def wal_cell(a):
        # Built as markup on purpose: the value is escaped, the warning span is not.
        # Escaping the whole thing printed the literal tag and killed the red cue.
        if a.get("status") != "copied":
            return ""
        return esc(a["wal"]) if a.get("wal") else "<span class=warn>none</span>"

    def hash_cell(a):
        # An unstable source has no source hash; show the copy's hash and say whose it is.
        if a.get("sha256"):
            return a["sha256"][:32]
        return ("copy only: " + a["copy_sha256"][:20]) if a.get("copy_sha256") else ""

    acq_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class=mono>%s</td></tr>" % (
            esc(a.get("file")), esc(a.get("status")), esc(a.get("bytes", "")),
            wal_cell(a), esc(a.get("shm", "")), esc(hash_cell(a)))
        for a in acq)

    hl = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        esc(k), esc(v.get("journal_mode")), esc(v.get("page_count")),
        esc(v.get("freelist_count")), esc(v.get("free_pages_with_residue"))) for k, v in health.items())

    warn_html = "".join("<li>%s</li>" % esc(w) for w in warnings) or "<li>None raised.</li>"

    cnt = "".join("<div class=stat><b>%s</b><span>%s</span></div>" % (esc(v), esc(k))
                  for k, v in counts.items())

    doc = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(case)s — Phone Link report</title><style>
:root{--bg:#0e1a2b;--pan:#16293f;--ink:#13233a;--pap:#fff;--teal:#2ec4b6;--amb:#f4a215;--red:#d9534f;--mut:#667c93}
*{box-sizing:border-box}body{margin:0;background:#f4f7fa;color:var(--ink);font:15px/1.55 Calibri,Segoe UI,sans-serif}
header{background:var(--bg);color:#fff;padding:28px 32px}
header h1{margin:0 0 6px;font:600 26px Cambria,Georgia,serif}
header p{margin:0;color:#9fb3c8;font-size:13px}
main{padding:24px 32px;max-width:1400px}
h2{font:600 19px Cambria,Georgia,serif;margin:30px 0 10px;color:var(--bg)}
table{border-collapse:collapse;width:100%%;font-size:12.5px;background:#fff;margin:8px 0 4px}
th{background:var(--pan);color:#fff;text-align:left;padding:7px 9px;font-weight:600}
td{border-bottom:1px solid #e3e9ef;padding:6px 9px;vertical-align:top}
tr:nth-child(even) td{background:#f7fafc}
.mono{font-family:Consolas,monospace;font-size:11.5px}
.muted{color:var(--mut);font-size:12.5px}
.warn{color:var(--red);font-weight:700}
.stats{display:flex;flex-wrap:wrap;gap:12px;margin:14px 0}
.stat{background:#fff;border-radius:8px;padding:12px 18px;min-width:120px;box-shadow:0 1px 3px #0002}
.stat b{display:block;font:700 24px Cambria,serif;color:var(--bg)}
.stat span{font-size:11.5px;color:var(--mut)}
.note{background:#fff6e4;border-radius:8px;padding:14px 18px;margin:14px 0;font-size:13px}
.note b{color:#8a5a00}
footer{padding:18px 32px;color:var(--mut);font-size:12px}
@media(max-width:700px){main,header,footer{padding:16px}table{font-size:11px}}
</style></head><body>
<header><h1>%(case)s</h1>
<p>Phone Link correlating parse &middot; device <span class=mono>%(dev)s</span> &middot; generated %(now)s UTC by %(tool)s %(ver)s</p></header><main>

<div class=stats>%(cnt)s</div>

<div class=note><b>Every name in this report is an inference by this tool.</b>
These databases contain no foreign keys between files. Numbers were matched to contacts on the
last %(digits)d digits, because the same number appears as +91…, 91… and bare in different tables.
Each correlated row carries a <span class=mono>name_source</span> column recording where the name came from.
Rows with an empty <span class=mono>name_source</span> were not matched to any contact.</div>

<h2>Acquisition integrity</h2>
<table><tr><th>File</th><th>Status</th><th>Bytes</th><th>WAL bytes</th><th>SHM bytes</th><th>SHA-256 (first 32)</th></tr>%(acq)s</table>
<p class=muted>A database copied without its WAL can open cleanly and be empty. "none" above means no WAL existed
at copy time — expected only if the app had exited cleanly.</p>

<h2>Database health</h2>
<table><tr><th>File</th><th>journal_mode</th><th>pages</th><th>freelist</th><th>free pages with non-zero bytes</th></tr>%(hl)s</table>
<p class=muted>The last column is read from the file itself: free pages that still hold non-zero bytes are worth
carving (residue is not proof of a recoverable record). Zero-filled free pages hold nothing to carve; freeblocks
inside live pages and the WAL are not covered by this check. PRAGMA secure_delete is not shown: it is a setting of
the analysing SQLite connection, not a property stored in the evidence file.</p>

<h2>Warnings</h2><ul>%(warn)s</ul>

<h2>Device</h2><p>%(meta_note)s</p><pre class=mono>%(meta)s</pre>

<h2>%(meta_other_heading)s</h2>
<p class=muted>The file is package-wide. The Metadata.Id of each entry below is not this device folder's GUID.</p>
<pre class=mono>%(meta_other)s</pre>

<h2>SIM / subscriber</h2>%(sims)s
<h2>Timeline (most recent)</h2>%(tl)s
<h2>Calls</h2>%(calls)s
<h2>Threads whose content has aged out</h2>%(aged)s
<h2>Apps streamed to this PC</h2>%(apps)s
<h2>Photo index</h2>%(pics)s
</main>
<footer>%(tool)s %(ver)s — correlation is performed by this tool, not by the data.
Validate every join against the databases before relying on it in a report.</footer></body></html>""" % {
        "case": esc(case), "dev": esc(dev), "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "tool": TOOL, "ver": VERSION, "cnt": cnt, "acq": acq_rows, "hl": hl, "warn": warn_html,
        "digits": digits, "meta_note": esc(dev_meta.get("note", "")),
        "meta": esc(json.dumps(dev_meta.get("entries", []), indent=1, default=str)[:2500]),
        "meta_other": esc(json.dumps(other_meta, indent=1, default=str)[:2500]),
        "meta_other_heading": OTHER_META_HEADING,         # a fixed string of this tool, not data
        "sims": table(d["sims"], ["subscription_id", "own_number", "carrier", "country", "sim_slot",
                                  "roaming", "default_sms", "default_voice", "rcs_supported"]),
        "tl": table(tl, ["timestamp_utc", "event", "party", "detail", "source"], 60),
        "calls": table(d["calls"], ["start_utc", "direction", "number", "contact_name",
                                    "name_source", "duration_sec", "sim_account"], 40),
        "aged": table([t for t in d["threads"] if t["content_aged_out"]],
                      ["counterparty", "contact_name", "name_source", "last_activity_utc",
                       "msg_count_reported", "participants"], 40),
        "apps": table([a for a in d["apps"] if a["streamed_to_pc"]],
                      ["app_name", "package", "installed_on_phone", "last_window_position"], 40),
        "pics": table(d["photos"], ["taken_utc", "name", "width", "height", "size_bytes",
                                    "thumbnail_cached", "android_uri"], 30),
    }
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(doc)


# ---------------------------------------------------------------- package-wide output
PACKAGE_DIR = "_package"


def write_package(a, pkg_acq, pkg_warnings, devs, ft_work, ft_carved, ft_attributed,
                  ft_carve_stats, ft_gone, meta_entries):
    """
    Write <out>\\_package: what belongs to the Phone Link package as a whole and must not be
    credited to any one device folder - carved FullTrust rows whose device_id is blank, unknown
    or another phone's, the FullTrust carve statistics and vacant-rowid counter, every live
    hosted_app_ids row with the device folder (if any) it was attributed to, every
    DeviceMetadataStorage.json entry, and the hashes of both package-wide input files.
    """
    pdir = os.path.join(a.out, PACKAGE_DIR)
    os.makedirs(pdir, exist_ok=True)
    labels = [lb for lb, _ in devs]
    unattributed = [dict(r, scope="package") for r in ft_carved if id(r) not in ft_attributed]
    live = []
    if ft_work:
        for r in q(ft_work, "SELECT * FROM hosted_app_ids"):
            owner = [lb for lb in labels if owns_ft_row(r.get("device_id"), lb)]
            live.append(dict(r, attributed_to_device_folder=owner[0] if owner else ""))
    write_csv(os.path.join(pdir, "carved_records.csv"), unattributed)
    write_csv(os.path.join(pdir, "carve_stats.csv"), ft_carve_stats)
    write_csv(os.path.join(pdir, "deleted_evidence.csv"), ft_gone)
    write_csv(os.path.join(pdir, "fulltrust_hosted_app_ids.csv"), live)
    counts = {"carved FullTrust rows (all)": len(ft_carved),
              "carved FullTrust rows credited to a device": len(ft_carved) - len(unattributed),
              "carved FullTrust rows not attributable": len(unattributed),
              "live hosted_app_ids rows": len(live),
              "live hosted_app_ids rows not attributable": sum(1 for r in live
                                                               if not r["attributed_to_device_folder"])}
    with open(os.path.join(pdir, "acquisition.json"), "w", encoding="utf-8") as fh:
        json.dump({"tool": TOOL, "version": VERSION, "scope": "package",
                   "generated_utc": datetime.now(timezone.utc).isoformat(),
                   "input": a.input, "device_folders": labels,
                   "package_files": pkg_acq, "warnings": pkg_warnings, "counts": counts,
                   "device_metadata_all_entries": meta_entries},
                  fh, indent=2, default=str)
    print("[%s]" % PACKAGE_DIR)
    for k, v in counts.items():
        print("    %-44s %s" % (k, v))
    for w in pkg_warnings:
        print("    ! %s" % w)
    print("    -> %s" % pdir)


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Phone Link correlating parser")
    ap.add_argument("--input", required=True, help="package root, Indexed dir, device dir, or a folder of .db files")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--case", default="Phone Link", help="case or exhibit label")
    ap.add_argument("--digits", type=int, default=10, help="trailing digits used to match numbers (default 10)")
    ap.add_argument("--keep-work", action="store_true", help="keep the staged copies")
    ap.add_argument("--carve", action="store_true",
                    help="also carve deleted records from free pages, freeblocks, page slack and WAL frames")
    a = ap.parse_args(argv)
    # Package-wide inputs (FullTrust phone_apps.db) are staged once, here, for every device.
    pkg_work = tempfile.mkdtemp(prefix="plparse_pkg_")
    try:
        return run(a, pkg_work)
    finally:
        if a.keep_work:
            print("package-wide staged copies kept at %s" % pkg_work)
        else:
            shutil.rmtree(pkg_work, ignore_errors=True)


def run(a, pkg_work):
    devs = find_device_dirs(a.input)
    if not devs:
        print("No device database folders found under: %s" % a.input, file=sys.stderr)
        print("Expected one of phone.db / calling.db / contacts.db somewhere beneath it.", file=sys.stderr)
        return 2

    ft_src = find_fulltrust(a.input)
    meta_src, meta_note = locate_metadata(a.input, [d for _, d in devs])
    pkg_acq, pkg_warnings = [], []
    if meta_note:
        pkg_warnings.append(meta_note)
    meta_entries, meta_error = read_metadata(meta_src, pkg_acq)
    if meta_error:
        pkg_warnings.append(meta_error)

    if os.path.exists(a.out) and not os.path.isdir(a.out):
        print("--out %s exists and is not a folder. Give a folder name (it is created if missing)." % a.out)
        return 2
    os.makedirs(a.out, exist_ok=True)
    print("%s %s" % (TOOL, VERSION))
    print("Found %d device folder(s). FullTrust db: %s. Metadata: %s\n"
          % (len(devs), "yes" if ft_src else "no", "yes" if meta_src else "no"))

    # ---- package-wide inputs: staged, carved and read ONCE, then split by device_id
    ft_work, ft_carved, ft_carve_stats, ft_carve_error, ft_gone = None, [], [], None, []
    if ft_src:
        ft_acq = []
        ft_work = stage(os.path.dirname(ft_src), pkg_work, ft_acq, names=[FULLTRUST_DB]).get(FULLTRUST_DB)
        for r in ft_acq:
            r.update({"file": "FullTrust/" + FULLTRUST_DB, "source": ft_src})
        pkg_acq.extend(ft_acq)
        pkg_warnings.extend(copy_mismatch_warnings(ft_acq))
    if ft_work:
        if a.carve and plcarve is not None:
            try:
                ft_carved, st = plcarve.carve_db(ft_work)
                ft_carve_stats = [st]
            except Exception as e:   # noqa: BLE001 - a damaged file must not end the run for every device
                ft_carve_error = ("carve failed on FullTrust/%s (package-wide): %s: %s"
                                  % (FULLTRUST_DB, type(e).__name__, e))
                pkg_warnings.append(ft_carve_error)
        ft_gone = deleted_evidence(ft_work, "FullTrust/" + FULLTRUST_DB)
        for g in ft_gone:
            if g["classification"] == "churn-or-deletion":
                pkg_warnings.append("%s.%s: %s live row(s), %s rowid(s) vacant across ALL devices - the "
                                    "counter is package-wide and cannot be split by device. Sync churn is "
                                    "the ordinary cause; NOT proof of user deletion."
                                    % (g["db"], g["table"], g["live_rows"], g["rowids_unused"]))
    ft_attributed = set()                    # id() of carved FullTrust rows credited to a device

    summary = []
    for label, src in devs:
        outdir = os.path.join(a.out, re.sub(r"[^A-Za-z0-9._-]", "_", label))
        os.makedirs(outdir, exist_ok=True)
        work = tempfile.mkdtemp(prefix="plparse_")
        acq, warnings = [], []
        try:
            dbs = stage(src, work, acq)
            if not dbs:
                print("[%s] no databases — skipped" % label)
                continue
            warnings.extend(copy_mismatch_warnings(acq))
            if ft_src and not is_guid(label):
                warnings.append("Device label %r is not a pairing-folder GUID, so no row of the package-wide "
                                "FullTrust %s (live or carved) is attributed to it. Rows that cannot be "
                                "attributed are written to the _package folder." % (label, FULLTRUST_DB))

            health = {n: db_health(p) for n, p in dbs.items()}
            for n, h in health.items():
                if h.get("journal_mode") == "wal" and not any(
                        x["file"] == n and x.get("wal") for x in acq):
                    warnings.append("%s is in WAL mode but no -wal file was present at copy time. "
                                    "If the source was live, data may be missing." % n)
                fl, res = h.get("freelist_count"), h.get("free_pages_with_residue")
                if isinstance(fl, int) and fl > 0 and isinstance(res, int):
                    if res > 0:
                        warnings.append("%s has %s free page(s); %s of them still hold non-zero bytes in the "
                                        "file - deleted content may be recoverable by carving (residue is "
                                        "not proof of a record)." % (n, fl, res))
                    else:
                        warnings.append("%s has %s free page(s), all zero-filled in the file - nothing to "
                                        "carve from free pages (freeblocks and the WAL are not covered by "
                                        "this check)." % (n, fl))

            del QUERY_ERRORS[:]                      # per-device
            del MISSING_COLUMNS[:]
            idx = build_contact_index(dbs.get("contacts.db"), a.digits)
            d = parse_all(dbs, ft_work, idx, a.digits, device_label=label)
            if MISSING_COLUMNS:
                warnings.append(missing_columns_warning())

            if d.get("_ft_scope_warning"):
                warnings.append(d["_ft_scope_warning"])
            not_here = [n for n in DB_NAMES if n not in dbs]
            if not_here:
                warnings.append("%d Phone Link database(s) are not in this folder, so their sections are empty: %s. "
                                "That is normal for a partial collection or a phone whose data Phone Link removed."
                                % (len(not_here), ", ".join(not_here)))
            if QUERY_ERRORS:
                warnings.append("%d query/table read(s) FAILED - an empty section below may mean "
                                "the read failed, not that the table was empty. See query_errors.csv."
                                % len(QUERY_ERRORS))
            d["query_errors"] = list(QUERY_ERRORS)

            carved, carve_stats = [], []
            if a.carve:
                if plcarve is None:
                    warnings.append("--carve requested but plcarve.py was not importable; skipped.")
                else:
                    for n, pth in dbs.items():
                        try:
                            rs, st = plcarve.carve_db(pth)
                        except Exception as e:   # noqa: BLE001 - one damaged file must not end a multi-device run
                            warnings.append("carve failed on %s: %s: %s - its deleted rows were not carved; the "
                                            "other files were" % (n, type(e).__name__, e))
                            continue
                        carve_stats.append(st)
                        carved.extend(rs)
                    for r in carved:
                        r["scope"] = "device"
                    # FullTrust is package-wide: only rows whose device_id is this folder's GUID.
                    for r in ft_carved:
                        if owns_ft_row(r.get("device_id"), label):
                            ft_attributed.add(id(r))
                            carved.append(dict(r, scope="device (FullTrust device_id = folder GUID)"))
                    if ft_carve_error:
                        warnings.append(ft_carve_error + " - this device's carved streamed-app rows "
                                        "may be missing.")
                    dl = sum(1 for r in carved if r.get("status") != "also_live")
                    if carved:
                        warnings.append("Carved %d candidate record(s) from dead space, %d not present "
                                        "in the live tables. Candidates are shape matches, not proof - "
                                        "check type_fit, region and offset before relying on any row."
                                        % (len(carved), dl))
            d["carved"] = carved
            d["carve_stats"] = carve_stats

            gone = []
            for n, pth in dbs.items():
                gone.extend(deleted_evidence(pth, n))
            # FullTrust's sqlite_sequence counter is shared by every device: it cannot be
            # split by device, so it is reported once, in _package, never per device.
            d["deleted_evidence"] = gone
            for g in gone:
                if g["classification"] == "churn-or-deletion":
                    warnings.append("%s.%s: %s live row(s), %s rowid(s) vacant - carve the free pages. "
                                    "Sync churn is the ordinary cause; NOT proof of user deletion."
                                    % (g["db"], g["table"], g["live_rows"], g["rowids_unused"]))
            tl = build_timeline(d)

            named = sum(1 for r in d["calls"] if r["contact_name"])
            counts = {
                "calls": len(d["calls"]), "calls named": named,
                "messages": len(d["messages"]), "threads": len(d["threads"]),
                "threads aged out": sum(1 for t in d["threads"] if t["content_aged_out"]),
                "contacts": len(d["contacts"]), "photos indexed": len(d["photos"]),
                "apps installed": sum(1 for x in d["apps"] if x["installed_on_phone"] == "yes"),
                "apps streamed": sum(1 for x in d["apps"] if x["streamed_to_pc"]),
                "notifications": len(d["notifications"]),
                "attachments": len(d["attachments"]),
                "timeline events": len(tl),
                "failed reads": len(d["query_errors"]),
                "carved candidates": len(d["carved"]),
                "carved not-live": sum(1 for r in d["carved"] if r.get("status") != "also_live"),
                "vacant-rowid signals": sum(1 for g in d["deleted_evidence"]
                                            if g["classification"] == "churn-or-deletion"),
            }

            files = {
                "calls.csv": d["calls"], "messages.csv": d["messages"], "threads.csv": d["threads"],
                "contacts.csv": d["contacts"], "photos.csv": d["photos"], "apps.csv": d["apps"],
                "notifications.csv": d["notifications"], "rcs_chat.csv": d["rcs"],
                "attachments.csv": d["attachments"], "sims.csv": d["sims"],
                "browser_handoff.csv": d["browser"], "timeline.csv": tl,
                "sent_from_pc.csv": d["sent_from_pc"], "deleted_contact_refs.csv": d["deleted_contact_refs"],
                "deleted_evidence.csv": d["deleted_evidence"],
                "carved_records.csv": d["carved"],
                "carve_stats.csv": d["carve_stats"],
                "query_errors.csv": d["query_errors"],
            }
            for fn, rows in files.items():
                write_csv(os.path.join(outdir, fn), rows)

            if d["carved"]:
                cdir = os.path.join(outdir, "carved")
                os.makedirs(cdir, exist_ok=True)
                by = {}
                for r in d["carved"]:
                    by.setdefault(r["table"], []).append(r)
                for t, rows in by.items():
                    cols, seen_c = [], set()
                    for r in rows:
                        for k in r:
                            if k not in seen_c:
                                seen_c.add(k); cols.append(k)
                    write_csv(os.path.join(cdir, "carved_%s.csv" % t), rows, cols)

            dev_meta, other_meta = device_metadata(label, meta_entries, meta_error, meta_src, meta_note)
            with open(os.path.join(outdir, "acquisition.json"), "w", encoding="utf-8") as fh:
                json.dump({"tool": TOOL, "version": VERSION,
                           "generated_utc": datetime.now(timezone.utc).isoformat(),
                           "source": src, "device_label": label,
                           "match_digits": a.digits, "files": acq,
                           # package-wide inputs this device's results also depend on
                           "package_files": pkg_acq, "health": health,
                           "warnings": warnings, "counts": counts,
                           "device_metadata": dev_meta,
                           "package_metadata_other_entries": other_meta}, fh, indent=2, default=str)

            html_report(os.path.join(outdir, "report.html"), a.case, label, (dev_meta, other_meta),
                        acq + pkg_acq, health, d, tl, counts, a.digits, warnings)

            print("[%s]" % label)
            for k, v in counts.items():
                print("    %-20s %s" % (k, v))
            for w in warnings:
                print("    ! %s" % w)
            print("    -> %s" % outdir)
            summary.append((label, counts))
        finally:
            if a.keep_work:
                print("    staged copies kept at %s" % work)
            else:
                shutil.rmtree(work, ignore_errors=True)

    if ft_src or meta_src:
        write_package(a, pkg_acq, pkg_warnings, devs, ft_work, ft_carved, ft_attributed,
                      ft_carve_stats, ft_gone, meta_entries)

    if not summary:
        print("\nNothing parsed.")
        return 1
    print("\nDone. Open report.html in each device folder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
