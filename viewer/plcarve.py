#!/usr/bin/env python3
"""
plcarve.py - schema-guided SQLite record carver for the Phone Link databases.

Recovers deleted rows from three kinds of dead space inside a .db file:

  1. FREE PAGES      - pages on the freelist. Whole pages, often still holding
                       an intact run of records.
  2. FREEBLOCKS      - gaps inside a live b-tree page, chained from the page
                       header. This is where a single deleted row usually lands.
  3. PAGE SLACK      - the unallocated gap between the cell-pointer array and
                       the start of cell content on a live page.

It also carves WAL frames, which hold page images the main file may no longer
have. In a frame that a later frame of the same page supersedes, the LIVE cells
are decoded too: a row deleted since then survives there whole, and so does
the earlier version of a row edited since (a changed message body or status).
The main file's own image of every page that the WAL holds a newer version of
is decoded the same way (region main-superseded-cell): a row deleted after the
last checkpoint, whose freed space in the WAL copy was reused, survives there.

STATUS of each record (column `status`; compared with the live table by rowid,
then by decoded values):
  also_live              dead space; the same values as the live row of that rowid
  earlier version of live row
                         dead space; the rowid is live but the values differ (an
                         edited row's old cell - or the rowid was reused)
  deleted_or_overwritten dead space; no live row has that rowid
  wal-superseded         a live cell of a superseded page version; its rowid is
                         no longer live (deleted since that version)
  wal-superseded (earlier version of live row)
                         a live cell of a superseded page version; the rowid is
                         live, the values differ
  wal-uncommitted        a live cell of a WAL frame after the log's last commit
                         frame: a transaction that was never committed
  wal-stale              a live cell of a WAL frame whose salts are not the WAL
                         header's: left over from an older generation of the log
  wal-invalid (checksum mismatch)
                         a live cell of a WAL frame with the header's salts whose
                         checksum chain fails (torn or damaged write)
  unknown - live rows unreadable
                         the live table could not be read, so liveness is unknown
For the last three, `rowid_live` says whether a live row has that rowid.

WAL FRAMES ARE VALIDATED as SQLite reads them (wal_read): the log is the run of
frames from the start whose salts equal the WAL header's and whose cumulative
checksums VERIFY (header and every frame checksum are checked); frames of that
run up to the last commit frame (non-zero database-size field) are committed.
The current version of a page is its last committed frame (else the main
file's image); only committed frames make another version superseded. Every
WAL-derived record carries wal_frame (index) and wal_frame_state (committed,
uncommitted, stale (older WAL generation), invalid (checksum mismatch)).
A live cell of a superseded version whose values equal the live row is NOT
reported: every live row has such copies. A table column whose name is also a
field of the carver (message.status, postaladdress.region) is written as
col_<name>, so the carver's own field is never overwritten.

WHY SCHEMA-GUIDED. A deleted cell is unlinked, so there is no pointer to it.
Carving therefore means recognising a record by its own shape. A SQLite record
is self-describing: a header of serial types followed by a body whose length is
implied by those types. A candidate is accepted only when

    payload_size == header_size + sum(field sizes implied by the serial types)

and the field count equals a known table's column count. That equation is a
strong filter - random bytes satisfy it very rarely - but it is not proof, so
every carved row is emitted with the evidence needed to judge it: which file,
which page, which region kind, and the byte offset.

READ THIS BEFORE USING THE OUTPUT
  * A carved row has NO rowid guarantee and NO timestamp guarantee. The values
    are whatever survived; adjacent bytes may have been overwritten.
  * A row matching table X's column count may actually be from table Y with the
    same shape. The `table` column is the tool's best fit, not a fact.
  * Carving finds deleted AND live records in the regions it scans. Rows are
    de-duplicated against the live table where a rowid match is possible, and
    flagged `also_live` when they are not clearly deleted.
  * Phone Link re-syncs wholesale, so a carved row is more often sync churn than
    a user deleting something. Say that in the report.

Stdlib only. Opens evidence read-only, byte-wise; never through SQLite.
"""

import json, os, re, struct, sqlite3

# ------------------------------------------------------------------ varint
def varint(buf, off):
    """SQLite big-endian base-128 varint. Returns (value, new_offset) or (None, off)."""
    n = 0
    for i in range(9):
        if off + i >= len(buf):
            return (None, off)
        b = buf[off + i]
        if i == 8:
            n = (n << 8) | b
            return (n, off + 9)
        n = (n << 7) | (b & 0x7F)
        if not (b & 0x80):
            return (n, off + i + 1)
    return (None, off)


# serial type -> byte length
_FIXED = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8, 7: 8, 8: 0, 9: 0}


def serial_len(t):
    if t in _FIXED:
        return _FIXED[t]
    if t in (10, 11):
        return None                 # reserved - never valid in a database file
    return (t - 12) // 2 if t % 2 == 0 else (t - 13) // 2


def decode_value(buf, off, t):
    ln = serial_len(t)
    if ln is None or off + ln > len(buf):
        return (None, off)
    if t == 0:
        return (None, off)
    if t == 8:
        return (0, off)
    if t == 9:
        return (1, off)
    if t in (1, 2, 3, 4, 5, 6):
        v = int.from_bytes(buf[off:off + ln], "big", signed=True)
        return (v, off + ln)
    if t == 7:
        return (struct.unpack(">d", buf[off:off + ln])[0], off + ln)
    raw = buf[off:off + ln]
    if t % 2 == 1:                  # TEXT
        try:
            return (raw.decode("utf-8"), off + ln)
        except UnicodeDecodeError:
            return (raw.decode("utf-8", "replace"), off + ln)
    return (raw, off + ln)          # BLOB


# ------------------------------------------------------------------ regions
def db_page_size(data):
    if len(data) < 100 or data[:16] != b"SQLite format 3\x00":
        return None
    ps = struct.unpack(">H", data[16:18])[0]
    return 65536 if ps == 1 else ps


def free_pages(data, ps):
    """Walk the freelist trunk chain from the file header."""
    out = set()
    if len(data) < 40:
        return out
    trunk = struct.unpack(">I", data[32:36])[0]
    seen = set()
    while trunk and trunk not in seen:
        seen.add(trunk)
        off = (trunk - 1) * ps
        if off + 8 > len(data):
            break
        out.add(trunk)
        nxt, cnt = struct.unpack(">II", data[off:off + 8])
        # a damaged count can claim more entries than a page holds, and a truncated file can end inside the page
        cnt = max(0, min(cnt, (ps - 8) // 4, (len(data) - off - 8) // 4))
        for i in range(cnt):
            p = struct.unpack(">I", data[off + 8 + i * 4: off + 12 + i * 4])[0]
            if p:
                out.add(p)
        trunk = nxt
    return out


def page_regions(data, ps, pno, freelist, hdr_offset=None):
    """
    Yield (kind, absolute_offset, length) of every dead-space region on a page.

    hdr_offset lets a caller override the 100-byte file-header skip. It matters for
    WAL frames: a frame is a standalone page image, so it sits at buffer offset 0
    regardless of which page number it holds. Passing pno=1 to get base=0 would
    otherwise also apply the file-header skip and misread the page header.
    """
    base = (pno - 1) * ps
    if base + ps > len(data):
        return
    if pno in freelist:
        yield ("free-page", base + 8, ps - 8)
        return
    hdr = base + ((100 if pno == 1 else 0) if hdr_offset is None else hdr_offset)
    if hdr + 8 > len(data):
        return
    ptype = data[hdr]
    if ptype not in (0x02, 0x05, 0x0A, 0x0D):
        return                                        # not a b-tree page
    ncells = struct.unpack(">H", data[hdr + 3:hdr + 5])[0]
    content = struct.unpack(">H", data[hdr + 5:hdr + 7])[0]
    if content == 0:
        content = 65536
    hlen = 12 if ptype in (0x02, 0x05) else 8
    ptr_end = hdr + hlen + ncells * 2
    cstart = base + content
    if cstart > ptr_end and cstart - ptr_end > 8:     # unallocated slack
        yield ("page-slack", ptr_end, cstart - ptr_end)
    fb = struct.unpack(">H", data[hdr + 1:hdr + 3])[0]  # freeblock chain
    guard = 0
    while fb and guard < 512:
        guard += 1
        o = base + fb
        if o + 4 > len(data):
            break
        nxt, sz = struct.unpack(">HH", data[o:o + 4])
        if sz >= 4:
            yield ("freeblock", o + 4, sz - 4)
        fb = nxt


# Statuses (see the module docstring)
ALSO_LIVE = "also_live"                       # dead space: the same values as the live row of that rowid
DELETED = "deleted_or_overwritten"            # dead space: no live row has that rowid
EARLIER_VERSION = "earlier version of live row"   # dead space: the rowid is live, its values differ
WAL_SUPERSEDED = "wal-superseded"             # a cell of a superseded page version; its rowid is no longer live
WAL_EARLIER_VERSION = "wal-superseded (earlier version of live row)"   # ... its rowid is live, its values differ
WAL_UNCOMMITTED = "wal-uncommitted"           # a live cell of a WAL frame after the log's last commit
WAL_STALE = "wal-stale"                       # a live cell of a WAL frame of an older log generation (other salts)
WAL_INVALID = "wal-invalid (checksum mismatch)"   # a live cell of a WAL frame whose checksum chain fails
# _version of a record decoded from a page version's live cells -> (status if rowid not live, if live but different)
VERSION_STATUS = {"superseded": (WAL_SUPERSEDED, WAL_EARLIER_VERSION),
                  "uncommitted": (WAL_UNCOMMITTED, WAL_UNCOMMITTED),
                  "stale": (WAL_STALE, WAL_STALE),
                  "invalid": (WAL_INVALID, WAL_INVALID)}
SUPERSEDED_REGION = "wal-superseded-cell"   # region: a live cell of a WAL frame that a later frame supersedes
MAIN_SUPERSEDED_REGION = "main-superseded-cell"   # region: a live cell of the main file's image of a page the WAL holds


def leaf_cells(page, hdr_off=0):
    """Start offsets of the cells of a table-leaf page image, from its cell-pointer array (empty for other pages)."""
    if hdr_off + 8 > len(page) or page[hdr_off] != 0x0D:
        return []
    n = struct.unpack(">H", page[hdr_off + 3:hdr_off + 5])[0]
    out = []
    for i in range(n):
        p = hdr_off + 8 + 2 * i
        if p + 2 > len(page):
            break
        off = struct.unpack(">H", page[p:p + 2])[0]
        if hdr_off + 8 + 2 * n <= off < len(page):
            out.append(off)
    return out


# State of a WAL frame (wal_read). Only "committed" frames are part of the database as SQLite reads it.
COMMITTED = "committed"          # in the valid log, at or before its last commit frame
UNCOMMITTED = "uncommitted"      # in the valid log (salts and checksums match) but after its last commit frame
STALE = "stale (older WAL generation)"     # its salts are not the WAL header's: left over from before a log restart
INVALID = "invalid (checksum mismatch)"    # the header's salts, but the checksum chain fails at or before it


FRAME_VERSION = {UNCOMMITTED: "uncommitted", STALE: "stale", INVALID: "invalid"}   # frame state -> record _version


def wal_checksum(buf, s0, s1, big):
    """SQLite's WAL checksum over buf (a multiple of 8 bytes), continued from (s0, s1)."""
    words = struct.unpack((">" if big else "<") + "%dI" % (len(buf) // 4), buf)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def wal_read(path, ps):
    """Read a -wal file. Returns a list of frames {index, pno, page, state, commit} (see the states above).

    As SQLite does: the log is the run of frames from the start whose salts equal the header's and whose cumulative
    checksums verify; it ends at the first frame that fails. Frames of that run up to the last commit frame (a
    non-zero database-size field) are committed, the rest of the run is uncommitted. A frame after the run whose
    salts differ belongs to an older generation of the log (stale); one with the header's salts is invalid. A WAL
    header whose own checksum fails makes every frame invalid."""
    if not os.path.isfile(path):
        return []
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 32:
        return []
    magic, ver, fps = struct.unpack(">III", data[0:12])
    if magic not in (0x377F0682, 0x377F0683):
        return []
    big = bool(magic & 1)                       # 0x377F0683: checksum words are big-endian
    if fps:
        ps = fps
    salts = data[16:24]
    s = struct.unpack(">II", data[24:32])
    in_log = wal_checksum(data[0:24], 0, 0, big) == s
    frames, last_commit = [], None
    off, i = 32, 0
    while off + 24 + ps <= len(data):
        pno, dbsize = struct.unpack(">II", data[off:off + 8])
        page = data[off + 24: off + 24 + ps]
        if data[off + 8:off + 16] != salts:
            state, in_log = STALE, False
        elif not in_log:
            state = INVALID
        else:
            c = wal_checksum(data[off:off + 8] + page, s[0], s[1], big)
            if c == struct.unpack(">II", data[off + 16:off + 24]):
                s, state = c, COMMITTED
                if dbsize:
                    last_commit = i
            else:
                state, in_log = INVALID, False
        frames.append({"index": i, "pno": pno, "page": page, "state": state, "commit": bool(dbsize)})
        off += 24 + ps
        i += 1
    for f in frames:
        if f["state"] == COMMITTED and (last_commit is None or f["index"] > last_commit):
            f["state"] = UNCOMMITTED
    return frames


def wal_frames(path, ps):
    """Yield (frame_index, page_number, page_bytes) of every frame of a -wal file, whatever its state."""
    for f in wal_read(path, ps):
        yield (f["index"], f["pno"], f["page"])


# ------------------------------------------------------------------ carving
def _ro(path):
    import plparse                  # imported late: plparse imports this module
    return plparse.ro_uri(path)


def local_payload(usable, p):
    """Bytes of a table-leaf cell's payload kept on the page itself (the rest is on overflow pages)."""
    x = usable - 35
    if p <= x:
        return p
    m = ((usable - 12) * 32 // 255) - 23
    k = m + ((p - m) % (usable - 4))
    return k if k <= x else m


PLUMBING = {"_change_notifications", "content_sequence"}
UNKNOWN_LIVE = "unknown - live rows unreadable"    # status when the table's live rowids could not be read


def affinity(decl):
    d = (decl or "").upper()
    if "INT" in d:
        return "int"
    if "BLOB" in d:
        return "blob"
    if any(k in d for k in ("CHAR", "CLOB", "TEXT")):
        return "text"
    return "any"


def type_fits(serial, aff):
    """Does a serial type look like it belongs to a column of this affinity?"""
    if serial == 0:
        return True                       # NULL fits anywhere
    if aff == "int":
        return serial in (1, 2, 3, 4, 5, 6, 8, 9)
    if aff == "text":
        return serial >= 13 and serial % 2 == 1
    if aff == "blob":
        return serial >= 12 and serial % 2 == 0
    return True


def table_shapes(db_path, include_plumbing=False, errors=None):
    """Column names AND affinities per table, straight from the live schema."""
    shapes = {}
    c = None
    try:
        c = sqlite3.connect(_ro(db_path), uri=True)
        c.text_factory = bytes             # a corrupt name must not abort the whole listing
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    except Exception as e:
        if errors is not None:
            errors.append("schema: %s: %s" % (type(e).__name__, e))
        names = []
    try:
        for raw in names:
            try:
                name = raw.decode("utf-8")
                if name.startswith("sqlite_") or name.startswith("fts_"):
                    continue
                info = list(c.execute('PRAGMA table_info("%s")' % name.replace('"', '""')))
                if info:
                    dec = lambda b: b.decode("utf-8", "replace") if b else ""
                    shapes.setdefault(len(info), []).append(
                        (name, [dec(r[1]) for r in info], [affinity(dec(r[2])) for r in info]))
            except Exception as e:
                if errors is not None:
                    errors.append("table %r: %s: %s" % (raw.decode("utf-8", "replace"), type(e).__name__, e))
    finally:
        if c is not None:
            c.close()
    return shapes


# Fields the carver itself writes into a record. A table column with one of these names is written as col_<name>.
CARVER_FIELDS = {"table", "type_fit", "candidates", "source_file", "page", "region", "offset", "rowid", "cols",
                 "overflow_lost", "status", "wal_frame", "wal_frame_state", "rowid_live", "json_package", "json_key",
                 "json_post_time_utc", "time_basis", "confidence", "live_check", "scope"}


def cut_at(types, q, lim):
    """Index of the first value that does not fit whole in the local payload (it is cut off or missing)."""
    for n, t in enumerate(types):
        q += serial_len(t)
        if q > lim:
            return n
    return None


def carve_region(buf, start, length, shapes, ps, src, pno, kind, min_text=1, usable=None):
    """Scan one dead-space region for records whose self-description is consistent."""
    out = []
    usable = usable or ps
    end = min(start + length, len(buf))
    page_end = (start // ps + 1) * ps       # a cell never crosses its own page
    i = start
    while i < end:
        psz, o1 = varint(buf, i)
        if psz is None or psz < 4 or psz > ps * 8:
            i += 1
            continue
        rowid, o2 = varint(buf, o1)
        if rowid is None or rowid < 0:
            i += 1
            continue
        hsz, o3 = varint(buf, o2)
        # Only the local part of the payload is on this page; the rest of an overflowing value is elsewhere.
        local = local_payload(usable, psz)
        if hsz is None or hsz < 2 or hsz > psz or hsz > local or o2 + local > min(len(buf), page_end):
            i += 1
            continue
        # decode the serial-type array
        types, p = [], o3
        bad = False
        while p < o2 + hsz:
            t, p = varint(buf, p)
            if t is None or t in (10, 11):
                bad = True
                break
            types.append(t)
        if bad or p != o2 + hsz or not types:
            i += 1
            continue
        body = sum((serial_len(t) or 0) for t in types)
        # THE FILTER: the record must account for exactly its own payload length
        if hsz + body != psz:
            i += 1
            continue
        cand = shapes.get(len(types))
        if not cand:
            i += 1
            continue
        vals, q, lost = [], o2 + hsz, False
        lim = o2 + local
        for t in types:
            ln = serial_len(t)
            if not lost and q + ln <= lim:
                v, q = decode_value(buf, q, t)
                vals.append(v)
                continue
            # past the local payload: keep a cut-off text/blob, never read another page's bytes
            raw = buf[q:lim] if not lost and t >= 12 else b""
            vals.append(None if not raw else (raw.decode("utf-8", "replace") if t % 2 else bytes(raw)))
            lost, q = True, lim
        texts = [v for v in vals if isinstance(v, str) and v.strip()]
        if len(texts) < min_text:
            i += 1
            continue
        # Column count alone is ambiguous. Score each candidate table by how well
        # the observed serial types match its declared column affinities.
        scored = []
        for nm, cols_, affs in cand:
            hits = sum(1 for t, af in zip(types, affs) if type_fits(t, af))
            scored.append((hits / float(len(types)), nm, cols_))
        scored.sort(key=lambda x: -x[0])
        fit, tname, cols = scored[0]
        if fit < 0.6:                     # shape does not really look like any table
            i += 1
            continue
        rec = {"table": tname, "type_fit": round(fit, 2),
               "candidates": "|".join("%s:%.2f" % (n, f) for f, n, _ in scored[:3]),
               "source_file": src, "page": pno, "region": kind,
               "offset": i, "rowid": rowid, "cols": len(types),
               "_vals": vals, "_cut": cut_at(types, o2 + hsz, lim) if lost else None}
        if lost:
            rec["overflow_lost"] = "yes"      # the value continues on overflow pages that are not read here
        for cn, v in zip(cols, vals):
            if cn in CARVER_FIELDS:
                cn = "col_" + cn              # a column named like a carver field (message.status) keeps its value
            if isinstance(v, bytes):
                rec[cn] = "<blob %d bytes>" % len(v)
            elif isinstance(v, str):
                rec[cn] = v.replace("\r", " ").replace("\n", " ")
            else:
                rec[cn] = v
        out.append(rec)
        i = o2 + local + (4 if local < psz else 0)    # skip the cell, incl. its overflow pointer
    return out


def live_rowids(db_path, table, errors=None):
    """Rowids currently in the table, or None when they could not be read (then liveness is unknown)."""
    c = None
    try:
        c = sqlite3.connect(_ro(db_path), uri=True)
        return {r[0] for r in c.execute('SELECT rowid FROM "%s"' % table.replace('"', '""'))}
    except Exception as e:
        if errors is not None:
            errors.append("live rows of %r: %s: %s" % (table, type(e).__name__, e))
        return None
    finally:
        if c is not None:
            c.close()


def live_values(db_path, table, rowids, errors=None):
    """{rowid: tuple of column values} of those live rows, or None when they could not be read."""
    c = None
    out = {}
    try:
        c = sqlite3.connect(_ro(db_path), uri=True)
        want = sorted(r for r in rowids if isinstance(r, int))
        for i in range(0, len(want), 500):
            part = want[i:i + 500]
            sql = 'SELECT rowid, * FROM "%s" WHERE rowid IN (%s)' % (table.replace('"', '""'), ",".join("?" * len(part)))
            for row in c.execute(sql, part):
                out[row[0]] = tuple(row[1:])
        return out
    except Exception as e:
        if errors is not None:
            errors.append("live values of %r: %s: %s" % (table, type(e).__name__, e))
        return None
    finally:
        if c is not None:
            c.close()


def same_as_live(rec, live):
    """Do a carved record's decoded values equal the live row? An INTEGER PRIMARY KEY column is stored as NULL in
    the record (the rowid holds it); a value cut at the overflow boundary only has to be the start of the live one,
    and the values after it, which did not survive, are not compared."""
    vals, cut = rec.get("_vals"), rec.get("_cut")
    if live is None or vals is None or len(vals) != len(live):
        return False
    for n, (v, lv) in enumerate(zip(vals, live)):
        if cut is not None and n > cut:
            break
        if cut is not None and n == cut:
            if v is None or (isinstance(v, type(lv)) and isinstance(v, (str, bytes)) and lv.startswith(v)):
                continue
            return False
        if v is None and lv == rec.get("rowid"):
            continue
        if isinstance(v, (str, bytes)) != isinstance(lv, (str, bytes)) or v != lv:
            return False
    return True


# ------------------------------------------------------------------ notification json (record header lost)
JSON_STATUS = "json-carved (record header lost)"
JSON_CONFIDENCE = ("only the json text survived: the SQLite record header (and with it rowid, notification_id, "
                   "package_name, post_time, state) was overwritten, so these columns are unknown. Accepted because "
                   "the bytes form one complete JSON object that parses and holds packageName and key; which row it "
                   "was, and when it was deleted, cannot be told")
JSON_TIME_BASIS = "json postTime (Unix ms) - the post_time column (FILETIME) did not survive"
_PKG_RE = re.compile(rb'"packageName"\s*:')
_JSON = json.JSONDecoder()
JSON_MAX_TRIES = 64          # '{' positions tried backwards from one "packageName"


def json_objects(buf):
    """(offset, json text, dict) for every complete JSON object in buf that holds packageName and key.

    Anchored on each "packageName": the '{' before it are tried nearest first, and the first one that opens a
    complete object spanning the anchor, parsing to a dict with packageName and key, is taken (a nested object
    without key is passed over for the one around it). The bytes must be valid UTF-8 and json-strict (no raw
    control characters), so overwritten or cut-off text is not accepted."""
    out, last_end = [], -1
    for m in _PKG_RE.finditer(buf):
        hit = m.start()
        if hit < last_end:
            continue                              # inside the object already taken
        pos = hit
        for _ in range(JSON_MAX_TRIES):
            s = buf.rfind(b"{", 0, pos)
            if s < 0:
                break
            pos = s
            win = bytes(buf[s:]).decode("utf-8", "surrogateescape")
            try:
                obj, end = _JSON.raw_decode(win)
            except ValueError:
                continue
            raw = win[:end].encode("utf-8", "surrogateescape")
            if s + len(raw) <= hit or not isinstance(obj, dict) or "packageName" not in obj or "key" not in obj:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            out.append((s, text, obj))
            last_end = s + len(raw)
            break
    return out


def json_identity(o):
    """Same notification version: json key + Android id + postTime. Another postTime under the same key is another
    version of the notification (the app updated it), so it is kept, not merged."""
    return (str(o.get("key")), str(o.get("id")), str(o.get("postTime")))


def has_notification_json(shapes):
    return any(nm == "notifications" and "json" in cols for lst in shapes.values() for nm, cols, _ in lst)


def _live_json(db_path, errors):
    """json of every live notifications row, or None when they could not be read."""
    c = None
    try:
        c = sqlite3.connect(_ro(db_path), uri=True)
        return [r[0] for r in c.execute('SELECT json FROM "notifications"')]
    except Exception as e:
        errors.append("live json of 'notifications': %s: %s" % (type(e).__name__, e))
        return None
    finally:
        if c is not None:
            c.close()


def _json_place(page, off, hdr, free):
    """Where a json object at offset off of one page image lies: the dead-space kind (free-page, freeblock,
    page-slack), 'not-a-btree-page' (an overflow or freelist page, whose bytes no cell pointer describes), or None
    when it is in the cell area of a b-tree page - inside a cell, which belongs to the record path."""
    if free:
        return "free-page"
    for kind, start, ln in page_regions(page, len(page), 1, set(), hdr):
        if start <= off < start + ln:
            return kind
    if hdr >= len(page) or page[hdr] not in (0x02, 0x05, 0x0A, 0x0D):
        return "not-a-btree-page"
    return None


def _ms_utc(v):
    """postTime is Unix milliseconds."""
    try:
        v = int(v)
        if v <= 0:
            return ""
        from datetime import datetime, timedelta, timezone
        return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=v)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return ""


def carve_notification_json(db_path, data, ps, fl, include_wal, carved, errors, stats=None):
    """Schema-specific pass for notifications.db (+ -wal): notification json whose record header was overwritten.

    Scans every page of the file and every WAL frame for complete json objects, and keeps those not already
    present as a live row or as a record the record carver recovered (same key + id + postTime), each once.
    Only objects in DEAD SPACE are reported (free pages, freeblocks, page slack, and pages that are not b-tree
    pages, such as overflow or freelist pages in a WAL frame). An object in the cell area of a b-tree page is
    inside a cell: the record path decodes that cell with its real rowid and columns (the live row, a superseded,
    earlier, uncommitted or stale version), so it is never reported here as 'record header lost'; such objects are
    counted in stats["json_in_cells"]."""
    stats = {} if stats is None else stats
    stats.setdefault("json_in_cells", 0)
    seen = set()
    live = _live_json(db_path, errors)
    for js in (live or []) + [r.get("json") for r in carved if r.get("table") == "notifications"]:
        try:
            o = json.loads(js)
        except (TypeError, ValueError):
            continue
        if isinstance(o, dict):
            seen.add(json_identity(o))
    out = []

    def take(text, o, src, pno, region, off, frame=None):
        ident = json_identity(o)
        if ident in seen:
            return
        seen.add(ident)
        rec = {"table": "notifications", "type_fit": "", "candidates": "json pass", "source_file": src,
               "page": pno, "region": "json-" + region, "offset": off, "rowid": None, "cols": "",
               "notification_id": None, "package_name": None, "json": text, "post_time": None,
               "state": None, "anonymous_id": None, "status": JSON_STATUS,
               "json_package": o.get("packageName"), "json_key": o.get("key"),
               "json_post_time_utc": _ms_utc(o.get("postTime")), "time_basis": JSON_TIME_BASIS,
               "confidence": JSON_CONFIDENCE}
        if frame is not None:
            rec["wal_frame"] = frame
        if live is None:
            rec["live_check"] = "live rows unreadable - this may be a live row"
        if frame is not None:
            rec["wal_frame_state"] = frame_state
        out.append(rec)

    name = os.path.basename(db_path)
    frame_state = None
    for pno in range(1, len(data) // ps + 1):
        base = (pno - 1) * ps
        page = data[base:base + ps]
        for off, text, o in json_objects(page):
            region = _json_place(page, off, 100 if pno == 1 else 0, pno in fl)
            if region is None:
                stats["json_in_cells"] += 1         # inside a cell: the record path decodes it, with its real rowid
                continue
            take(text, o, name, pno, region, base + off)
    if include_wal:
        wp = db_path + "-wal"
        for f in wal_read(wp, ps):
            idx, pno, page, frame_state = f["index"], f["pno"], f["page"], f["state"]
            frame_data = 32 + idx * (24 + len(page)) + 24      # where this page image starts in the -wal file
            for off, text, o in json_objects(page):
                region = _json_place(page, off, 100 if pno == 1 else 0, False)
                if region is None:
                    stats["json_in_cells"] += 1
                    continue
                take(text, o, os.path.basename(wp), pno, region, frame_data + off, idx)
    return out


NOT_IN_KEY = ("page", "offset", "region", "source_file", "wal_frame", "wal_frame_state")


def classify(db_path, recs, errors):
    """De-duplicate carved records and give each its status against the live table (see the module docstring).

    A record decoded from a live cell of a page version that is not the current one (r["_version"]) is dropped when
    its values equal the live row of its rowid - every live row has such copies - and kept otherwise."""
    seen, uniq = set(), []
    for r in recs:
        key = (r["table"], r["rowid"], tuple(sorted((k, str(v)) for k, v in r.items()
                                                    if k not in NOT_IN_KEY and not k.startswith("_"))))
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    rowids = {}
    for r in uniq:
        if r["table"] not in rowids:
            rowids[r["table"]] = live_rowids(db_path, r["table"], errors)
    values = {}
    for t, ids in rowids.items():
        if ids is not None:
            need = {r["rowid"] for r in uniq if r["table"] == t and r["rowid"] in ids}
            values[t] = live_values(db_path, t, need, errors) if need else {}
    out = []
    for r in uniq:
        ids, version = rowids[r["table"]], r.get("_version")
        if ids is None:
            r["status"] = UNKNOWN_LIVE
        elif r["rowid"] in ids:
            vals = values.get(r["table"])
            # live values unreadable (the error is recorded): treat as the same row, as before values were compared
            same = vals is None or same_as_live(r, vals.get(r["rowid"]))
            if same and version:
                continue          # a copy of the live row as it is now, in an older page version
            if same:
                r["status"] = ALSO_LIVE
            else:
                r["status"] = VERSION_STATUS[version][1] if version else EARLIER_VERSION
            if version and version != "superseded":
                r["rowid_live"] = "yes - the live row's values differ"
        else:
            r["status"] = VERSION_STATUS[version][0] if version else DELETED
            if version and version != "superseded":
                r["rowid_live"] = "no"
        for k in [k for k in r if k.startswith("_")]:
            del r[k]
        out.append(r)
    return out


def carve_db(db_path, include_wal=True, min_text=1, include_plumbing=False, json_pass=True):
    """Carve one database. Returns (records, stats).

    json_pass: for a database with a notifications.json column, also recover notification json whose record header
    was overwritten (carve_notification_json); such rows have status JSON_STATUS."""
    with open(db_path, "rb") as fh:
        data = fh.read()
    ps = db_page_size(data)
    stats = {"file": os.path.basename(db_path), "page_size": ps,
             "pages": 0, "free_pages": 0, "regions": 0, "wal_frames": 0, "records": 0,
             "errors": []}
    if not ps:
        return ([], stats)
    usable = ps - data[20]           # reserved bytes at the end of each page are not cell space
    shapes = table_shapes(db_path, errors=stats["errors"])   # match against ALL tables, incl. plumbing
    npages = len(data) // ps
    fl = free_pages(data, ps)
    stats["pages"], stats["free_pages"] = npages, len(fl)

    recs = []
    for pno in range(1, npages + 1):
        for kind, off, ln in page_regions(data, ps, pno, fl):
            stats["regions"] += 1
            recs += carve_region(data, off, ln, shapes, ps,
                                 os.path.basename(db_path), pno, kind, min_text, usable)

    if include_wal:
        wp = db_path + "-wal"
        frames = wal_read(wp, ps)
        # The current version of a page is its last COMMITTED frame (else the main file's image). Uncommitted, stale
        # and invalid frames never decide what is superseded.
        newest = {f["pno"]: f["index"] for f in frames if f["state"] == COMMITTED}
        # The main file's image of a page the WAL holds a committed version of is an older version too: a row
        # deleted after the last checkpoint, whose freed space in the WAL copy was reused, survives there whole.
        for pno in sorted(newest):
            if pno in fl or pno > npages:
                continue
            base = (pno - 1) * ps
            page = data[base:base + ps]
            for off in leaf_cells(page, 100 if pno == 1 else 0):
                for r in carve_region(page, off, 1, shapes, ps, os.path.basename(db_path), pno,
                                      MAIN_SUPERSEDED_REGION, min_text, usable):
                    r["offset"] = base + off
                    r["_version"] = "superseded"
                    recs.append(r)
        for f in frames:
            idx, pno, page, state = f["index"], f["pno"], f["page"], f["state"]
            stats["wal_frames"] += 1
            stats["wal_" + state.split()[0]] = stats.get("wal_" + state.split()[0], 0) + 1
            tag = {"wal_frame": idx, "wal_frame_state": state}
            # A frame is a standalone page image at buffer offset 0, so walk it as
            # page 1 (base=0) but apply the file-header skip only if it really IS
            # page 1. Getting this wrong misreads every frame's page header.
            hdr_off = 100 if pno == 1 else 0
            for kind, off, ln in page_regions(page, ps, 1, set(), hdr_offset=hdr_off):
                for r in carve_region(page, off, ln, shapes, ps, os.path.basename(wp), pno, "wal-" + kind,
                                      min_text, usable):
                    recs.append(dict(r, **tag))
            if state == COMMITTED:
                if newest[pno] == idx:
                    continue                  # the current version: its cells are the live rows
                version = "superseded"
            else:
                version = FRAME_VERSION[state]
            # An older (or never committed) version of the page: its LIVE cells are rows as they were then, and a row
            # since deleted survives here whole. Each cell is decoded where its pointer says it starts.
            region = SUPERSEDED_REGION if version == "superseded" else "wal-%s-cell" % version
            for off in leaf_cells(page, hdr_off):
                for r in carve_region(page, off, 1, shapes, ps, os.path.basename(wp), pno, region, min_text, usable):
                    r.update(tag, _version=version)
                    recs.append(r)

    # Plumbing tables are matched (so their records are not mislabelled as some
    # other table of the same width) but dropped here unless explicitly wanted.
    if not include_plumbing:
        recs = [r for r in recs if r["table"] not in PLUMBING]

    uniq = classify(db_path, recs, stats["errors"])
    stats["json_carved"] = stats["json_in_cells"] = 0
    if json_pass and has_notification_json(shapes):
        jr = carve_notification_json(db_path, data, ps, fl, include_wal, uniq, stats["errors"], stats)
        stats["json_carved"] = len(jr)
        uniq += jr
    stats["records"] = len(uniq)
    # rows that are not live in any form: copies of live rows and earlier versions of live rows are left out
    stats["deleted_only"] = sum(1 for r in uniq if r["status"] not in (ALSO_LIVE, EARLIER_VERSION, WAL_EARLIER_VERSION))
    stats["earlier_versions"] = sum(1 for r in uniq if r["status"] in (EARLIER_VERSION, WAL_EARLIER_VERSION))
    return (uniq, stats)


if __name__ == "__main__":
    import argparse, csv, sys, collections
    ap = argparse.ArgumentParser(description="Schema-guided SQLite carver for Phone Link databases")
    ap.add_argument("db", nargs="+", help="one or more .db files")
    ap.add_argument("--out", help="write carved_<db>_<table>.csv into this directory")
    ap.add_argument("--min-text", type=int, default=1, help="require at least N non-empty text fields")
    ap.add_argument("--no-wal", action="store_true")
    ap.add_argument("--include-plumbing", action="store_true",
                    help="also carve _change_notifications / content_sequence (noisy churn tables)")
    a = ap.parse_args()
    total = 0
    for p in a.db:
        recs, st = carve_db(p, include_wal=not a.no_wal, min_text=a.min_text,
                            include_plumbing=a.include_plumbing)
        print("%-20s pages=%-6s free=%-4s regions=%-5s wal=%-5s carved=%-5s deleted-only=%s"
              % (st["file"], st["pages"], st["free_pages"], st["regions"],
                 st["wal_frames"], st["records"], st.get("deleted_only", 0)))
        by = collections.Counter(r["table"] for r in recs)
        for t, n in by.most_common():
            print("      %-26s %d" % (t, n))
        total += len(recs)
        if a.out and recs:
            os.makedirs(a.out, exist_ok=True)
            groups = collections.defaultdict(list)
            for r in recs:
                groups[r["table"]].append(r)
            for t, rows in groups.items():
                cols, seen = [], set()
                for r in rows:
                    for k in r:
                        if k not in seen:
                            seen.add(k); cols.append(k)
                fn = os.path.join(a.out, "carved_%s_%s.csv" %
                                  (os.path.splitext(os.path.basename(p))[0], t))
                with open(fn, "w", newline="", encoding="utf-8-sig") as fh:
                    w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
                    w.writeheader()
                    ctrl = re.compile("[" + "".join(chr(c) for c in list(range(0, 9)) + [11, 12] + list(range(14, 32))) + "]")
                    for r in rows:
                        w.writerow({k: (ctrl.sub(chr(0xFFFD), v) if isinstance(v, str) else v)
                                    for k, v in r.items()})
    print("\ntotal carved: %d" % total)
