"""
pldevices.py - every phone ever linked to this PC through Phone Link.

Sources (all under ...\\Microsoft.YourPhone_8wekyb3d8bbwe\\LocalCache\\):

  Indexed\\<GUID>\\                 one folder per pairing. The folder's creation time reflects when that
                                   phone was linked only if the folders' timestamps were preserved when
                                   collected (see _time_check); any databases left inside hold its data. Phone
                                   Link empties a folder when a phone is removed, but the folder (and
                                   its GUID and times) stays - evidence of a pairing even with no data.
  DeviceMetadataStorage.json       phone details per hardware ID: display name, manufacturer, model,
                                   Android version, Phone Link client version, capabilities, linked,
                                   registration and last-seen times. A hardware ID with no entry is a
                                   phone whose details were removed.
  YppCryptoTrustRelationships      trust relationships with partner devices: partner id, trust type,
                                   last access, key expiry, Microsoft account id (MsaCid).
  Indexed\\FullTrust\\phone_apps.db  hosted_app_ids: apps streamed to the PC, per device_id.

Not read: PlatformEncryptedKeyStorage.json (encryption keys, no device details) and certificate
bodies. Only the certificate count is shown.

Everything is copied to a temporary folder first, each copy is re-hashed against its source,
and databases are opened read-only. Standard library only.
"""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile

import plcarve
import plparse

GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
META, TRUST, FULLTRUST = "DeviceMetadataStorage.json", "YppCryptoTrustRelationships", "phone_apps.db"
DATA_TABLES = {"calling.db": ["call_history"], "phone.db": ["message", "conversation", "mms"],
               "contacts.db": ["contact"], "photos.db": ["media"], "phoneapps.db": ["phone_apps"],
               "notifications.db": ["notifications"], "sharedcontent.db": ["sharedcontent", "shareduri"]}
INFER_WINDOW_S = 600   # metadata registration this close before a folder's creation -> inferred same pairing
SNAPSHOT_TRIES = 3     # copies of a file whose source keeps changing, before it is kept and flagged
UNSTABLE = "n/a - source changed during copy"


def find_localcache(path):
    """LocalCache folder for an input path (package root, LocalCache, Indexed or a device folder), or None."""
    p = os.path.abspath(os.path.expandvars(path))
    for _ in range(6):
        for cand in (p, os.path.join(p, "LocalCache")):
            if os.path.isdir(os.path.join(cand, "Indexed")) or os.path.isfile(os.path.join(cand, META)):
                return cand
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _utc_ts(ts):
    try:
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


_ISO = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d+))?\s*(Z|[+-]\d\d(?::?\d\d)?)?")


def _iso(s):
    """'2026-09-21T17:17:49.1234567+05:30' -> ('2026-09-21 11:47:49', epoch seconds).

    Accepts any number of fraction digits (.NET writes 7). A time with no offset is not UTC: it is shown as
    written with ' (no zone)' and no epoch."""
    if not s:
        return "", None
    m = _ISO.fullmatch(str(s).strip())
    if m:
        y, mo, d, h, mi, se = (int(x) for x in m.group(1, 2, 3, 4, 5, 6))
        zone = m.group(8)
        if zone is None:
            return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{se:02d} (no zone)", None
        try:
            if zone == "Z":
                off = 0
            else:
                digits = zone[1:].replace(":", "")
                off = (int(digits[:2]) * 60 + int(digits[2:] or 0)) * (-1 if zone[0] == "-" else 1)
            micro = int((m.group(7) or "")[:6].ljust(6, "0"))
            t = dt.datetime(y, mo, d, h, mi, se, micro, tzinfo=dt.timezone(dt.timedelta(minutes=off)))
            t = t.astimezone(dt.timezone.utc)
            return t.strftime("%Y-%m-%d %H:%M:%S"), t.timestamp()
        except (ValueError, OverflowError):
            pass
    return str(s), None


def _is_link(path):
    """True for a symlink or a Windows junction. These are never followed when copying."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    isj = getattr(os.path, "isjunction", None)   # 3.12+; older Pythons rely on the reparse-point attribute
    reparse = getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(st.st_mode) or bool(reparse) or bool(isj and isj(path))


def _folder_times(path):
    """(creation time, modification time) of a folder as read. On Windows the creation time is st_birthtime
    (3.12+) or st_ctime. A module-level function so tests can inject times the file system cannot fake."""
    st = os.stat(path)
    return getattr(st, "st_birthtime", st.st_ctime), st.st_mtime


# A folder's creation time is the pairing time only on the live package, or on a copy that preserved it.
# A plain file copy (Explorer, copytree, most zip tools) gives every folder a NEW creation time - the copy
# time - while file modification times are usually kept. These checks spot that. Any one of them is enough:
#   1. a folder was created more than COPY_TOLERANCE_S after the newest file inside it was last modified
#      (on the live package the folder exists before its files are written);
#   2. a folder was created more than COPY_TOLERANCE_S after the folder itself was last modified
#      (copytree / robocopy keep the folder's modified time but not its creation time);
#   3. two or more folders, all created within TOGETHER_S of each other (pairings are separate user actions,
#      a copy creates them in one burst);
#   4. every folder was created more than INFER_WINDOW_S after every registration / last-seen time in the
#      metadata (a pairing folder appears when a device registers, so at least one time should be near it).
# They can miss a copy (an empty folder copied slowly by a tool that resets its modified time) and check 4
# can fire on a genuine package whose phones' details were all removed; both only make the tool claim less.
COPY_TOLERANCE_S = 2     # timestamp granularity and clock rounding (FAT keeps 2 s)
TOGETHER_S = 1.0
FOLDER_TIME_CAVEAT = ("folder creation time as read - reflects the pairing only if the folders' timestamps were "
                      "preserved when collected")


def _time_check(folders, meta_times):
    """(warning text or "", {guid: per-folder reason or ""}) for folders = {guid: {"created", "modified",
    "newest_file"}}. meta_times are the metadata's registration / last-seen epochs."""
    reasons, notes = _time_reasons(folders, meta_times)
    if not reasons:
        return "", notes
    return ("WARNING: the pairing folders' creation times look like the time the evidence was copied, not the "
            "pairing times (" + "; ".join(reasons) + "). Pairings are numbered in order of folder creation as read "
            "- not pairing order - and no 'phone linked' times or time matches are taken from them"), notes


def _time_reasons(folders, meta_times):
    """([package-wide reasons the folder times look like copy times], {guid: this folder's own reason or ""})."""
    notes, reasons = {}, []
    for guid, info in folders.items():
        c, why = info["created"], []
        if info.get("newest_file") is not None and c > info["newest_file"] + COPY_TOLERANCE_S:
            why.append(f"created {int(c - info['newest_file'])} s after the newest file inside was last modified")
        if info.get("modified") is not None and c > info["modified"] + COPY_TOLERANCE_S:
            why.append(f"created {int(c - info['modified'])} s after the folder was last modified")
        notes[guid] = "; ".join(why)
    flagged = [g for g, n in notes.items() if n]
    if any("newest file" in notes[g] for g in flagged):
        reasons.append("a folder was created after the newest file inside it was last modified")
    if any("folder was last modified" in notes[g] for g in flagged):
        reasons.append("a folder was created after it was last modified")
    created = [i["created"] for i in folders.values()]
    if len(created) >= 2 and max(created) - min(created) <= TOGETHER_S:
        reasons.append(f"all {len(created)} folders were created within {TOGETHER_S:g} s of each other")
    times = [t for t in meta_times if t is not None]
    if created and times and min(created) > max(times) + INFER_WINDOW_S:
        reasons.append("every folder was created after every registration and last-seen time in the metadata")
    return reasons, notes


class PackageCopy:
    """Copy the parts of LocalCache this module reads; verify every copy; never touch the source again.

    Symlinks and junctions are not followed (they could point outside the package): each is listed in
    `skipped` and shown in the copy report. If copying fails, the work folder is removed."""

    def __init__(self, localcache):
        self.src = os.path.abspath(localcache)
        self.work = tempfile.mkdtemp(prefix="pldevices-")
        self.verify = []
        self.skipped = []  # relative paths of symlinks / junctions that were not followed
        self.unstable = []  # relative paths whose source kept changing while it was copied (copy kept, flagged)
        self.unreadable = []  # relative paths whose source could not be read (not copied, error row)
        self.folders = {}  # guid -> {"created": ts, "modified": ts, "files": [...]}
        try:
            self._copy_all()
        except BaseException:
            shutil.rmtree(self.work, ignore_errors=True)
            raise

    @property
    def warnings(self):
        """Plain-English warnings about the copy, for the views' banners."""
        out = []
        if self.unstable:
            out.append("WARNING: the source changed while it was being copied: %s. The copy may mix two moments "
                       "and some recent rows may be missing or inconsistent" % ", ".join(self.unstable))
        if self.unreadable:
            out.append("WARNING: these source files could not be read and were not copied: %s"
                       % ", ".join(self.unreadable))
        return out

    def _copy_all(self):
        idx = os.path.join(self.src, "Indexed")
        for name in (META, TRUST):
            self._copy(os.path.join(self.src, name), os.path.join(self.work, name))
        if os.path.isdir(idx) and not _is_link(idx):
            for g in sorted(os.listdir(idx)):
                sp = os.path.join(idx, g)
                if _is_link(sp):
                    self.skipped.append(os.path.relpath(sp, self.src))
                    continue
                if not os.path.isdir(sp):
                    continue
                created, modified = _folder_times(sp)
                info = {"created": created, "modified": modified, "newest_file": None, "files": []}
                for root, dirs, files in os.walk(sp):
                    for d in [d for d in dirs if _is_link(os.path.join(root, d))]:
                        dirs.remove(d)    # do not descend
                        self.skipped.append(os.path.relpath(os.path.join(root, d), self.src))
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), sp)
                        dst = os.path.join(self.work, "Indexed", g, rel)
                        try:
                            mt = os.stat(os.path.join(root, f)).st_mtime   # taken before the copy, from the source
                        except OSError:
                            mt = None
                        if self._copy(os.path.join(root, f), dst):
                            info["files"].append(rel)
                            if mt is not None:
                                info["newest_file"] = mt if info["newest_file"] is None else max(info["newest_file"], mt)
                self.folders[g] = info
        elif os.path.isdir(idx):
            self.skipped.append("Indexed")
        if any(v["copy_matches"] == "NO" for v in self.verify):
            raise IOError("a copy does not match its source hash")

    def _copy(self, src, dst):
        """Copy one file and check it, the way plparse.stage does for a device folder: the source is hashed
        before and after the copy, and the copy is retried while it keeps changing. A source that never
        settles is kept and marked 'n/a - source changed during copy' (no source hash is claimed; the copy's
        own hash is recorded). A source that cannot be read is an error row and is not copied. A copy that
        differs from a source that did settle is 'NO' and stops the whole copy."""
        rel = os.path.relpath(src, self.src)
        if _is_link(src):
            self.skipped.append(rel)
            return False
        if not os.path.isfile(src):
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        stable = False
        for _ in range(SNAPSHOT_TRIES):
            try:
                before = _sha(src)
            except OSError as e:
                self.unreadable.append(rel)
                self.verify.append({"file": rel, "source_sha256": "", "copy_sha256": "",
                                    "copy_matches": f"error - source could not be read ({_why(e)})"})
                if os.path.isfile(dst):
                    os.remove(dst)     # a partial copy from an earlier try is not evidence
                return False
            shutil.copy2(src, dst)
            try:
                after = _sha(src)
            except OSError:
                after = None
            if after == before:
                stable = True
                break
        mine = _sha(dst)
        if stable:
            self.verify.append({"file": rel, "source_sha256": before, "copy_sha256": "",
                                "copy_matches": "yes" if mine == before else "NO"})
        else:
            self.unstable.append(rel)
            self.verify.append({"file": rel, "source_sha256": "", "copy_sha256": mine,
                                "copy_matches": UNSTABLE})
        return True

    def path(self, *parts):
        return os.path.join(self.work, *parts)

    def close(self):
        """Remove the copies. Returns the folder if it could not be removed (so the caller can warn)."""
        shutil.rmtree(self.work, ignore_errors=True)
        return self.work if os.path.exists(self.work) else None


def _why(e):
    """A short reason for an error row."""
    return f"{type(e).__name__}: {e}"


def _ro(path):
    c = sqlite3.connect(plparse.ro_uri(path), uri=True)
    c.execute("PRAGMA query_only = ON")
    return c


def _db_summary(pc, guid, info, ts_seconds):
    """Per-device database summary: databases present, data rows, activity range, SIM."""
    dbs = sorted({os.path.basename(f) for f in info["files"] if f.endswith(".db")})
    rows, other, bad, first, last, sim, details = 0, 0, 0, None, None, "", []
    other_names = []   # '<db> <table> (<rows>)' for rows outside DATA_TABLES - named in the status
    for db in dbs:
        p = None
        for f in info["files"]:
            if os.path.basename(f) == db:
                p = pc.path("Indexed", guid, f)
        try:
            c = _ro(p)
        except sqlite3.Error:
            details.append(f"{db}: unreadable")
            bad += 1
            continue
        try:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            n_db = 0
            for t in DATA_TABLES.get(db, []):
                if t in tables:
                    n = c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                    n_db += n
            rows += n_db
            details.append(f"{db}: {n_db} rows")
            # Rows in any other table are still data - except bookkeeping that is rewritten constantly.
            # Counted on its own so one table that cannot be read does not lose the activity range or the SIM.
            found, failed = [], []
            for t in sorted(tables - set(DATA_TABLES.get(db, [])) - plcarve.PLUMBING):
                if t.startswith("sqlite_"):
                    continue
                try:
                    n = c.execute('SELECT count(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0]
                except sqlite3.Error as e:
                    failed.append(f"{t}: {e}")
                    continue
                other += n
                if n:
                    found.append(f"{t}: {n}")
                    other_names.append(f"{db} {t} ({n})")
            if found:
                details.append(f"{db}: other tables - " + ", ".join(found))
            if failed:
                details.append(f"{db}: could not count " + "; ".join(failed))
                bad += 1
            for t, col in (("call_history", "start_time"), ("message", "timestamp"), ("media", "taken_time")):
                if t in tables:
                    lo, hi = c.execute(f'SELECT min("{col}"), max("{col}") FROM "{t}" WHERE "{col}" > 0').fetchone()
                    lo, hi = ts_seconds(lo), ts_seconds(hi)
                    if lo is not None:
                        first = lo if first is None else min(first, lo)
                    if hi is not None:
                        last = hi if last is None else max(last, hi)
            if "subscription" in tables:
                s = c.execute("SELECT name, number FROM subscription ORDER BY subscription_id LIMIT 1").fetchone()
                if s:
                    sim = " ".join(str(x) for x in s if x)
        except sqlite3.Error as e:
            details.append(f"{db}: {e}")
            bad += 1
        finally:
            c.close()
    if not info["files"]:
        status = "folder only - its data was removed"
    elif not dbs:
        status = "files present but no databases"
    elif rows:
        status = "data present"
    elif other:   # DataRows counts only DATA_TABLES, so say exactly where the rows are
        status = "rows only in tables not counted here: " + ", ".join(other_names)
    elif bad:
        status = "unreadable database"
    else:
        status = "databases present, no rows"
    return dbs, rows, first, last, sim, status, details


def build(localcache, g, View, ts_seconds, iso):
    """Add the 'Linked phones (this PC)' views to graph g. Returns (vids, PackageCopy or None)."""
    if not localcache:
        v = View("PC_devices", "Phone pairings (device folders)", "pc",
                 "only one device folder was given, so the package-level files that list every linked phone are "
                 "not available. Open the Phone Link package folder to see all pairings",
                 ["Note"], [{"Note": "Open ...\\Packages\\Microsoft.YourPhone_8wekyb3d8bbwe to list every linked phone"}])
        g.add_view(v)
        return [v.vid], None
    pc = PackageCopy(localcache)
    try:
        return _build(pc, g, View, ts_seconds, iso), pc
    except BaseException:
        pc.close()   # never leave the copies behind when a view cannot be built
        raise


SOURCE_ERRORS = (sqlite3.Error, OSError, ValueError, AttributeError, TypeError, KeyError)   # bad file or odd shape
META_COLS = ["HardwareID", "Kind", "Linked", "DisplayName", "Manufacturer", "Model", "ModelVersion", "OS", "PhoneLinkApp",
             "Enabled", "ConsentVersion", "Capabilities", "RegisteredUTC", "LastSeenUTC", "MetadataID",
             "DistinguishedDeviceID", "Certificates", "Note"]
# DeviceMetadataStorage.json is not only phones: on a real PC it also held the PC itself ("Windows 10.0.x",
# DisplayName = the PC name). Only an entry whose OS is a phone OS is called a phone or matched to a pairing folder.
KIND_TEXT = {"phone": "phone", "pc": "this or another Windows PC (not a phone)",
             "unknown": "unknown device (OS not recorded)", "other": "other device (not a known phone OS)",
             "placeholder": "empty placeholder - no entries"}
KIND_NOUN = {"phone": "phone", "pc": "Windows PC", "unknown": "device", "other": "device"}
PHONE_OS = re.compile(r"\b(android|ios|iphone|ipados)\b", re.I)


def _device_kind(inner):
    """'phone', 'pc', 'unknown' or 'other' for a metadata entry, from its OS name (or, without one, its client)."""
    said = str(inner.get("OsName") or "").strip() or str(inner.get("ClientType") or "").strip()
    if not said:
        return "unknown"
    if "windows" in said.lower():
        return "pc"
    return "phone" if PHONE_OS.search(said) else "other"


def _build(pc, g, View, ts_seconds, iso):
    vids = []

    def add(vid, title, basis, cols, rows, entity=True):
        v = View(vid, title, "pc", basis, cols, rows)
        g.add_view(v, "_e" if entity else None)
        vids.append(vid)

    # ---------- streamed apps per device (FullTrust)
    streamed, by_dev, streamed_err = [], {}, None
    ft = pc.path("Indexed", "FullTrust", FULLTRUST)
    if os.path.isfile(ft):
        try:
            c = _ro(ft)
            try:
                cols = [r[1] for r in c.execute("PRAGMA table_info(hosted_app_ids)")]
                if cols:
                    for r in c.execute("SELECT * FROM hosted_app_ids"):
                        d = dict(zip(cols, r))
                        e = ("streamed", d.get("row_id"))
                        dev = str(d.get("device_id") or "").lower()
                        by_dev.setdefault(dev, []).append(e)
                        streamed.append({"_e": e, **{k: d[k] for k in cols}})
                        g.label[e] = f"streamed app {d.get('package_id')}"
            finally:
                c.close()
        except SOURCE_ERRORS as ex:
            streamed, by_dev, streamed_err = [], {}, f"could not read {FULLTRUST}: {_why(ex)}"

    # ---------- phone details (metadata)
    meta_rows, mids, meta_err = [], {}, None
    if os.path.isfile(pc.path(META)):
        try:
            with open(pc.path(META), encoding="utf-8-sig") as f:
                meta = json.load(f)
            metas = meta.get("DeviceMetadatas") or {}
            if not isinstance(metas, dict):
                raise TypeError(f"DeviceMetadatas is a {type(metas).__name__}, expected an object keyed by hardware ID")
        except SOURCE_ERRORS as ex:
            metas, meta_err = {}, f"could not read {META}: {_why(ex)}"
        for hw, entries in metas.items():
            if not entries:
                e = ("phone", hw)     # internal key only; the label and Kind say what it is
                shown = hw or "(empty)"
                g.label[e] = f"placeholder {shown} - no entries (details removed or never stored)"
                meta_rows.append({"_e": e, "_kind": "placeholder", **{c: "" for c in META_COLS}, "HardwareID": shown,
                                  "Kind": KIND_TEXT["placeholder"],
                                  "Note": "placeholder - no entries: hardware ID kept, details removed (or never "
                                          "stored) - which device it was is not recorded"})
                continue
            try:
                for x in entries if isinstance(entries, list) else [None]:
                    m = x.get("Metadata") or {}
                    inner = m.get("Metadata") or {}
                    reg, reg_ts = _iso(m.get("RegitrationTime") or m.get("RegistrationTime"))  # Microsoft's spelling
                    seen, seen_ts = _iso(m.get("LastSeenTime"))
                    kind = _device_kind(inner)
                    e = ("phone", m.get("Id") or hw)     # internal key only; the label and Kind say what it is
                    mids[m.get("Id")] = e
                    name = inner.get("DisplayName") or inner.get("ModelName") or hw
                    g.label[e] = {"phone": f"phone {name}",
                                  "pc": f"Windows PC {name} (this PC or a Windows companion - not a phone)",
                                  "unknown": f"device {name} (OS not recorded)",
                                  "other": f"device {name} (OS {inner.get('OsName')} - not a known phone OS)"}[kind]
                    g.time[e] = reg_ts
                    meta_rows.append({
                        "_e": e, "_reg_ts": reg_ts, "_seen_ts": seen_ts, "_kind": kind, "HardwareID": hw,
                        "Kind": KIND_TEXT[kind], "Linked": "yes" if x.get("IsLinked") else "no",
                        "DisplayName": inner.get("DisplayName"), "Manufacturer": inner.get("Manufacture"),
                        "Model": inner.get("ModelName"), "ModelVersion": inner.get("ModelVersion"),
                        "OS": " ".join(str(v) for v in (inner.get("OsName"), inner.get("OsVersion")) if v),
                        "PhoneLinkApp": " ".join(str(v) for v in (inner.get("ClientType"), inner.get("ClientVersion")) if v),
                        "Enabled": inner.get("IsEnabled"), "ConsentVersion": inner.get("ConsentVersion"),
                        "Capabilities": ", ".join(inner.get("Capabilities") or []), "RegisteredUTC": reg,
                        "LastSeenUTC": seen, "MetadataID": m.get("Id"), "DistinguishedDeviceID": m.get("DistinguishedDeviceId"),
                        "Certificates": sum(len(v or []) for v in (x.get("Certificates") or {}).values()),
                        "Note": ""})
            except SOURCE_ERRORS as ex:
                meta_rows.append({"HardwareID": hw, "Linked": "", "DisplayName": "",
                                  "Note": f"could not read this entry: {_why(ex)}"})
    # ---------- device folders
    pairings = {k: v for k, v in pc.folders.items() if GUID.match(k)}
    meta_times = [m.get(k) for m in meta_rows for k in ("_reg_ts", "_seen_ts")]
    copy_warn, time_notes = _time_check(pairings, meta_times)
    copy_why = "; ".join(_time_reasons(pairings, meta_times)[0])
    trusted = not copy_warn        # folder creation times may be read as pairing times
    order = sorted((v["created"], k) for k, v in pairings.items())
    folder_rows = []
    for n, (created, guid) in enumerate(order, 1):
        info = pc.folders[guid]
        dbs, rows, first, last, sim, status, details = _db_summary(pc, guid, info, ts_seconds)
        e = ("pairing", guid)
        if trusted:
            g.label[e] = f"pairing #{n} - folder {guid[:8]} (folder created {_utc_ts(created)[:10]})"
            g.time[e] = created
        else:
            g.label[e] = f"pairing #{n} - folder {guid[:8]} (order of folder creation as read - not pairing order)"
        folder_rows.append({"_e": e, "_created": created, "#": n, "DeviceFolder": guid,
                            "FolderCreatedUTC": _utc_ts(created), "FolderModifiedUTC": _utc_ts(info["modified"]),
                            "TimeCheck": "no sign of copy time" if trusted else
                            f"looks like copy time: {time_notes[guid] or 'the folders as a whole - ' + copy_why}",
                            "Status": status, "Databases": ", ".join(dbs), "DataRows": rows,
                            "FirstActivityUTC": iso(first), "LastActivityUTC": iso(last), "SIM": sim,
                            "StreamedApps": len(by_dev.get(guid.lower(), [])), "Detail": "; ".join(details)})
    add("PC_devices", "Phone pairings (device folders)",
        "".join(w + ". " for w in pc.warnings + ([copy_warn] if copy_warn else [])) +
        f"one folder per pairing under LocalCache\\Indexed, numbered in order of creation. FolderCreatedUTC = "
        f"{FOLDER_TIME_CAVEAT}; TimeCheck says whether it looks like a copy time. Empty folders are pairings whose "
        f"data Phone Link removed. DataRows counts rows in the known data tables (calls, messages, contacts, photos, "
        f"apps, notifications, shared content); rows found only in other tables are named in Status",
        ["#", "DeviceFolder", "FolderCreatedUTC", "FolderModifiedUTC", "TimeCheck", "Status", "Databases", "DataRows",
         "FirstActivityUTC", "LastActivityUTC", "SIM", "StreamedApps", "Detail"], folder_rows)

    if meta_err:
        add("PC_phones", "Linked devices (DeviceMetadataStorage.json)", meta_err, ["Error"], [{"Error": meta_err}])
    else:
        add("PC_phones", "Linked devices (DeviceMetadataStorage.json)",
            "per hardware ID: what kind of device each entry is (Kind, from its OS name: phone / this or another "
            "Windows PC / empty placeholder), its own name, make, model, OS and Phone Link app; registration and "
            "last-seen times. Only phones are matched to pairing folders. Certificate bodies are not shown, only "
            "their count", META_COLS, meta_rows)

    # ---------- trust relationships
    trust_rows, trust_err = [], None
    if os.path.isfile(pc.path(TRUST)):
        try:
            with open(pc.path(TRUST), encoding="utf-8-sig") as f:
                trust = json.load(f)
            for key, t in (trust.get("TrustedClients") or {}).items():
                e = ("trust", key)
                last, last_ts = _iso(t.get("RelationshipLastAccessTime"))
                exp, _ = _iso(t.get("PartnerKeyExpirationTime"))
                g.label[e] = f"trust relationship type {t.get('TrustType')} (last access {last[:10]})"
                g.time[e] = last_ts
                trust_rows.append({"_e": e, "PartnerClientID": t.get("PartnerClientId"), "TrustType": t.get("TrustType"),
                                   "LastAccessUTC": last, "KeyExpiresUTC": exp, "MsaCid": t.get("MsaCid"),
                                   "SelfClientID": t.get("SelfClientId"),
                                   "DcgClientID": (t.get("Attributes") or {}).get("dcg_prod_client_id")})
                # PartnerClientId is stored as '<prefix>_<id>'; it must end with '_' + the phone's metadata Id
                partner = str(t.get("PartnerClientId") or "").lower()
                hit = next((pe for mid, pe in mids.items() if mid and partner.endswith("_" + str(mid).lower())), None)
                if hit:
                    g.link("PR1", hit, e, "Stored link: this trust relationship's partner ID contains the phone's "
                                          "metadata ID")
        except SOURCE_ERRORS as ex:
            trust_rows, trust_err = [], f"could not read {TRUST}: {_why(ex)}"
    if trust_err:
        add("PC_trust", "Trust relationships (YppCryptoTrustRelationships)", trust_err, ["Error"], [{"Error": trust_err}])
    else:
        add("PC_trust", "Trust relationships (YppCryptoTrustRelationships)",
            "cryptographic trust with partner devices: last access, key expiry, trust type and the Microsoft account "
            "id (MsaCid). Certificates are not shown",
            ["PartnerClientID", "TrustType", "LastAccessUTC", "KeyExpiresUTC", "MsaCid", "SelfClientID", "DcgClientID"],
            trust_rows)

    if streamed_err:
        add("PC_streamed", "Apps streamed to this PC, per device (FullTrust phone_apps.db)", streamed_err, ["Error"],
            [{"Error": streamed_err}])
    else:
        add("PC_streamed", "Apps streamed to this PC, per device (FullTrust phone_apps.db)",
            "hosted_app_ids: one row per app a phone streamed to the PC; device_id is the pairing folder's GUID",
            list(streamed[0].keys())[1:] if streamed else ["device_id"], streamed)
    for r in folder_rows:
        for se in by_dev.get(r["DeviceFolder"].lower(), []):
            g.link("PR2", r["_e"], se, "Stored link: hosted_app_ids.device_id is this pairing folder's name")

    # ---------- relations
    # A phone (never a PC or an unknown device) is a candidate for every folder created within the window after
    # it registered. Only a one-to-one pair - the phone has one candidate folder and that folder one candidate
    # phone - is linked. Anything else is listed as ambiguous and not linked. Copy-time folder dates match nothing.
    phones = [m for m in meta_rows if trusted and "_e" in m and m.get("_kind") == "phone" and m.get("_reg_ts") is not None]
    cands = [(m, [r for r in folder_rows if 0 <= r["_created"] - m["_reg_ts"] <= INFER_WINDOW_S]) for m in phones]
    per_folder = {}
    for m, near in cands:
        for r in near:
            per_folder[r["DeviceFolder"]] = per_folder.get(r["DeviceFolder"], 0) + 1
    rel = []
    for m, near in cands:
        reg = m["_reg_ts"]
        for r in sorted(near, key=lambda r: r["_created"]):
            gap = int(r["_created"] - reg)
            n_phones = per_folder[r["DeviceFolder"]]
            if len(near) == 1 and n_phones == 1:
                g.link("PR3", m["_e"], r["_e"], f"Inferred by time: folder created {gap} s after the phone registered")
                basis = "inferred (time) - no stored ID links them"
            else:
                why = [f"{len(near)} folders were created within {INFER_WINDOW_S // 60} min after this phone "
                       f"registered"] if len(near) > 1 else []
                why += [f"{n_phones} phones registered within {INFER_WINDOW_S // 60} min before this folder was "
                        f"created"] if n_phones > 1 else []
                basis = "AMBIGUOUS - not linked: " + "; ".join(why)
            rel.append({"_e": m["_e"], "Phone": g.label[m["_e"]], "Pairing": g.label[r["_e"]],
                        "RegisteredUTC": m["RegisteredUTC"], "FolderCreatedUTC": r["FolderCreatedUTC"],
                        "GapSeconds": gap, "Basis": basis})
    add("PR3", "Phone details -> pairing folder (inferred by time)",
        (copy_warn + ". Nothing is matched by time. " if copy_warn else "") +
        f"the metadata keeps no folder GUID, so a phone is matched to a folder created within "
        f"{INFER_WINDOW_S // 60} minutes after its registration time ({FOLDER_TIME_CAVEAT}). Only phones are matched, "
        f"and only one-to-one: when a phone has several such folders or a folder several such phones, every candidate "
        f"is listed as AMBIGUOUS and none is linked. An inference, not a stored link",
        ["Phone", "Pairing", "RegisteredUTC", "FolderCreatedUTC", "GapSeconds", "Basis"], rel, entity=False)

    rel = [{"_e": e, "Device": g.label[e], "TrustRelationship": g.label[t], "Basis": "stored: PartnerClientId contains metadata Id"}
           for e in mids.values() for rid, t, _ in g.links(e) if rid == "PR1"]
    add("PR1", "Linked device details -> trust relationship (stored link)",
        "YppCryptoTrustRelationships PartnerClientId ('<prefix>_<id>') whose id part equals DeviceMetadataStorage "
        "Metadata.Id",
        ["Device", "TrustRelationship", "Basis"], rel, entity=False)

    rel = [{"_e": r["_e"], "Pairing": g.label[r["_e"]], "StreamedApps": r["StreamedApps"],
            "Basis": "stored: hosted_app_ids.device_id = folder name"} for r in folder_rows if r["StreamedApps"]]
    add("PR2", "Pairing folder -> streamed apps (stored link)",
        "FullTrust phone_apps.db hosted_app_ids.device_id equal to the pairing folder's GUID",
        ["Pairing", "StreamedApps", "Basis"], rel, entity=False)

    events = []   # (epoch or None, what, detail, entity, the time as shown in its own view)
    for r in folder_rows if trusted else []:   # a copy time is not a pairing event
        events.append((r["_created"], "pairing folder created (the phone was linked then, if the folder times were "
                                      "preserved)", g.label[r["_e"]], r["_e"], ""))
    for m in meta_rows:
        noun = KIND_NOUN.get(m.get("_kind"), "device")
        if m.get("RegisteredUTC"):
            events.append((m.get("_reg_ts"), f"{noun} registered (metadata)", g.label[m["_e"]], m["_e"],
                           m["RegisteredUTC"]))
        if m.get("LastSeenUTC"):
            events.append((m.get("_seen_ts"), f"{noun} last seen (metadata)", g.label[m["_e"]], m["_e"],
                           m["LastSeenUTC"]))
    for t in trust_rows:
        events.append((g.time.get(t["_e"]), "trust relationship last used", g.label[t["_e"]], t["_e"],
                       t.get("LastAccessUTC") or ""))
    rows = [{"_e": e, "TimeUTC": _utc_ts(ts), "Event": what, "Detail": detail}
            for ts, what, detail, e, _ in sorted((x for x in events if x[0] is not None), key=lambda x: x[0])]
    # a time written without a zone (or unreadable) has no UTC instant: shown as written, after the ordered rows
    rows += [{"_e": e, "TimeUTC": shown, "Detail": detail,
              "Event": what + (" - time has no zone, not placed in time order" if shown.endswith("(no zone)")
                               else " - not a readable time, not placed in time order")}
             for ts, what, detail, e, shown in events if ts is None and shown]
    add("PR4", "Pairing history (all linked-phone events in time order)",
        (copy_warn + ". Folder creations are left out of the history. " if copy_warn else "") +
        f"folder creations ({FOLDER_TIME_CAVEAT}), registrations, last-seen and trust times from all the sources "
        f"above. Times written without a zone are listed after the others as '(no zone)' - they cannot be put in "
        f"UTC order",
        ["TimeUTC", "Event", "Detail"], rows, entity=False)

    v = View("PC_copies", "Linked phones - copies and hashes", "provenance",
             "".join(w + ". " for w in pc.warnings) +
             f"copied from {pc.src} to {pc.work} (PlatformEncryptedKeyStorage.json deliberately not copied; "
             f"symlinks and junctions are not followed; a file whose source kept changing has no source hash, "
             f"only the hash of the copy taken)",
             ["file", "source_sha256", "copy_sha256", "copy_matches"],
             pc.verify + [{"file": f, "source_sha256": "", "copy_sha256": "",
                           "copy_matches": "skipped - symlink or junction, not followed"} for f in pc.skipped])
    g.add_view(v)
    vids.append(v.vid)
    return vids
