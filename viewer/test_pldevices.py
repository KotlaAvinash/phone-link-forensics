"""Tests for pldevices.py - every phone ever linked to the PC. Synthetic package only.

    python test_pldevices.py
"""
import calendar
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import types
import unittest
import warnings
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pldemo  # noqa: E402
import pldevices as D  # noqa: E402
import plrelate as R  # noqa: E402


def tree_hashes(root):
    out = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


G = ["aaaaaaaa-0000-4000-8000-00000000000%d" % i for i in range(1, 8)]


def make_pkg(root, meta=None, trust=None, folders=()):
    """A tiny synthetic LocalCache: folders = [(guid, {relative file: bytes or [(ddl, row sql), ...]})]."""
    lc = os.path.join(root, "pkg", "LocalCache")
    os.makedirs(os.path.join(lc, "Indexed"), exist_ok=True)
    for guid, files in folders:
        d = os.path.join(lc, "Indexed", guid)
        os.makedirs(d, exist_ok=True)
        for rel, content in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if isinstance(content, bytes):
                with open(p, "wb") as f:
                    f.write(content)
            else:
                con = sqlite3.connect(p)
                for ddl, rows in content:
                    con.execute(ddl)
                    for r in rows:
                        con.execute(r)
                con.commit()
                con.close()
    for name, obj in ((D.META, meta), (D.TRUST, trust)):
        if obj is not None:
            with open(os.path.join(lc, name), "w", encoding="utf-8") as f:
                f.write(obj if isinstance(obj, str) else json.dumps(obj))
    return lc


class WorkFolders:
    """Point tempfile at a private folder so the test can count the pldevices-* folders it leaves."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old, tempfile.tempdir = tempfile.tempdir, self.tmp.name
        return self

    def left(self):
        return [n for n in os.listdir(self.tmp.name) if n.startswith("pldevices-")]

    def __exit__(self, *a):
        tempfile.tempdir = self.old
        self.tmp.cleanup()


class IsoTimes(unittest.TestCase):
    def test_seven_digit_fraction_with_offset(self):
        text, ts = D._iso("2026-09-21T17:17:49.1234567+05:30")
        self.assertEqual(text, "2026-09-21 11:47:49")
        self.assertAlmostEqual(ts, calendar.timegm((2026, 9, 21, 11, 47, 49)) + 0.123456, places=5)

    def test_current_format_without_fraction_and_z(self):
        want = calendar.timegm((2026, 9, 21, 11, 47, 49))
        self.assertEqual(D._iso("2026-09-21T17:17:49+05:30"), ("2026-09-21 11:47:49", want))
        self.assertEqual(D._iso("2026-09-21T11:47:49Z"), ("2026-09-21 11:47:49", want))
        self.assertEqual(D._iso("2026-09-21T11:47:49.5+00:00")[0], "2026-09-21 11:47:49")

    def test_no_offset_is_never_presented_as_utc(self):
        self.assertEqual(D._iso("2026-09-21T17:17:49"), ("2026-09-21 17:17:49 (no zone)", None))
        self.assertEqual(D._iso("2026-09-21T17:17:49.1234567"), ("2026-09-21 17:17:49 (no zone)", None))

    def test_junk_and_empty(self):
        self.assertEqual(D._iso(""), ("", None))
        self.assertEqual(D._iso("not a time"), ("not a time", None))


class PairingStatus(unittest.TestCase):
    def test_each_state_is_told_apart(self):
        msg = [("CREATE TABLE message (id INTEGER, timestamp INTEGER)", [])]
        other = [("CREATE TABLE settings (k TEXT)", ["INSERT INTO settings VALUES ('x')"])]
        folders = [(G[0], {}),
                   (G[1], {"notes.txt": b"hello"}),
                   (G[2], {"System/Database/phone.db": b"this is not a database at all, just bytes" * 4}),
                   (G[3], {"System/Database/settings.db": other}),
                   (G[4], {"System/Database/phone.db": msg}),
                   (G[5], {"System/Database/phone.db": msg + [("CREATE TABLE conversation (id INTEGER)",
                                                               ["INSERT INTO conversation VALUES (1)"])]})]
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, folders=folders)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        rows = {r["DeviceFolder"]: r for r in g.views["PC_devices"].rows}
        got = {k: r["Status"] for k, r in rows.items()}
        self.assertEqual(got[G[0]], "folder only - its data was removed")
        self.assertEqual(got[G[1]], "files present but no databases")
        self.assertEqual(got[G[2]], "unreadable database")
        self.assertEqual(got[G[3]], "rows only in tables not counted here: settings.db settings (1)")
        self.assertEqual(got[G[4]], "databases present, no rows")
        self.assertEqual(got[G[5]], "data present")
        # no status claims data next to DataRows 0 without naming where the rows are
        for k, r in rows.items():
            if r["DataRows"] == 0:
                self.assertFalse(r["Status"].startswith(("data present", "data in")), r)

    def test_empty_databases_of_several_kinds_are_no_rows(self):
        # contacts.db, settings.db and sharedcontent.db present, every table empty
        files = {"System/Database/contacts.db": [("CREATE TABLE contact (id INTEGER)", [])],
                 "System/Database/settings.db": [("CREATE TABLE settings (k TEXT)", [])],
                 "System/Database/sharedcontent.db": [("CREATE TABLE sharedcontent (id INTEGER)", []),
                                                      ("CREATE TABLE shareduri (id INTEGER)", [])]}
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, folders=[(G[0], files)])
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        r = g.views["PC_devices"].rows[0]
        self.assertEqual((r["Status"], r["DataRows"]), ("databases present, no rows", 0))


class DemoPackageIsClean(unittest.TestCase):
    def test_build_package_raises_no_deprecation_warning(self):
        with tempfile.TemporaryDirectory() as tmp, warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            pldemo.build_package(os.path.join(tmp, "pkg"))


class OtherTables(unittest.TestCase):
    """Rows only in other tables must not count bookkeeping tables, and the status names the tables counted."""

    def summarize(self, files):
        with WorkFolders(), tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, folders=[(G[0], files)])
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        return g.views["PC_devices"].rows[0]

    def test_only_bookkeeping_rows_is_empty(self):
        row = self.summarize({"System/Database/calling.db": [
            ("CREATE TABLE content_sequence (content_type TEXT, sequence INTEGER)",
             ["INSERT INTO content_sequence VALUES ('call_logs', 7)"]),
            ("CREATE TABLE _change_notifications (id INTEGER PRIMARY KEY, what TEXT)",
             ["INSERT INTO _change_notifications (what) VALUES ('x')", "INSERT INTO _change_notifications (what) VALUES ('y')"]),
            ("CREATE TABLE call_history (id INTEGER, start_time INTEGER)", [])]})
        self.assertEqual(row["Status"], "databases present, no rows")

    def test_rows_in_an_unknown_table_are_named(self):
        row = self.summarize({"System/Database/calling.db": [
            ("CREATE TABLE content_sequence (content_type TEXT)", ["INSERT INTO content_sequence VALUES ('call_logs')"]),
            ("CREATE TABLE mystery (a TEXT)", ["INSERT INTO mystery VALUES ('1')", "INSERT INTO mystery VALUES ('2')"]),
            ("CREATE TABLE emptyone (a TEXT)", [])]})
        self.assertEqual(row["Status"], "rows only in tables not counted here: calling.db mystery (2)")
        self.assertIn("mystery: 2", row["Detail"])
        self.assertNotIn("content_sequence", row["Detail"])
        self.assertNotIn("emptyone", row["Detail"])

    def test_table_name_with_a_double_quote(self):
        row = self.summarize({"System/Database/settings.db": [
            ('CREATE TABLE "we""ird" (a TEXT)', ["INSERT INTO \"we\"\"ird\" VALUES ('1')"])]})
        self.assertEqual(row["Status"], 'rows only in tables not counted here: settings.db we"ird (1)')
        self.assertIn('we"ird: 1', row["Detail"])

    def test_a_table_that_cannot_be_counted_does_not_stop_the_activity_range(self):
        with WorkFolders(), tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "src.db")
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE message (id INTEGER, timestamp INTEGER)")
            con.execute("INSERT INTO message VALUES (1, 1893456000)")
            con.execute("CREATE TABLE junk (a TEXT)")
            con.executemany("INSERT INTO junk VALUES (?)", [("x" * 50,)] * 20)
            con.commit()
            root = con.execute("SELECT rootpage FROM sqlite_master WHERE name='junk'").fetchone()[0]
            ps = con.execute("PRAGMA page_size").fetchone()[0]
            con.close()
            with open(path, "rb") as fh:
                data = bytearray(fh.read())
            data[(root - 1) * ps:root * ps] = bytes([255]) * ps                 # the table's root page is garbage
            lc = make_pkg(tmp, folders=[(G[0], {"System/Database/phone.db": bytes(data)})])
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        row = g.views["PC_devices"].rows[0]
        self.assertEqual(row["DataRows"], 1)
        self.assertTrue(row["FirstActivityUTC"].startswith("2030-01-01"), row)
        self.assertIn("junk", row["Detail"])


class BadSources(unittest.TestCase):
    META = {"DeviceMetadatas": {"HW1": [{"IsLinked": True, "Metadata": {"Id": "id-1", "Metadata": {"DisplayName": "P1"}}}]}}
    TRUST = {"TrustedClients": {"id-1": {"PartnerClientId": "pre_id-1", "TrustType": 3}}}
    OK_FOLDER = (G[0], {"System/Database/phone.db": [("CREATE TABLE message (id INTEGER, timestamp INTEGER)",
                                                      ["INSERT INTO message VALUES (1, 5)"])]})

    def build(self, **kw):
        with WorkFolders(), tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, folders=kw.pop("folders", [self.OK_FOLDER]), **kw)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
            return g

    def assert_error_row(self, g, vid):
        self.assertTrue(any(r.get("Error") or "could not read" in str(r.get("Note", "")) for r in g.views[vid].rows),
                        g.views[vid].rows)

    def test_corrupt_fulltrust_db(self):
        g = self.build(meta=self.META, trust=self.TRUST,
                       folders=[self.OK_FOLDER, ("FullTrust", {"phone_apps.db": b"garbage" * 30})])
        self.assert_error_row(g, "PC_streamed")
        self.assertEqual(len(g.views["PC_devices"].rows), 1)
        self.assertEqual(len(g.views["PC_phones"].rows), 1)
        self.assertEqual(len(g.views["PC_trust"].rows), 1)

    def test_device_metadatas_list_shape(self):
        g = self.build(meta={"DeviceMetadatas": [{"HW1": []}]}, trust=self.TRUST)
        self.assert_error_row(g, "PC_phones")
        self.assertEqual(len(g.views["PC_trust"].rows), 1)
        self.assertEqual(len(g.views["PC_devices"].rows), 1)

    def test_one_odd_entry_does_not_hide_the_others(self):
        meta = {"DeviceMetadatas": {"BAD": {"not": "a list"}, **self.META["DeviceMetadatas"]}}
        g = self.build(meta=meta, trust=self.TRUST)
        notes = {r["HardwareID"]: r for r in g.views["PC_phones"].rows}
        self.assertIn("could not read", notes["BAD"]["Note"])
        self.assertEqual(notes["HW1"]["DisplayName"], "P1")

    def test_truncated_json(self):
        full = json.dumps(self.META)
        g = self.build(meta=full[:len(full) // 2], trust=json.dumps(self.TRUST)[:20])
        self.assert_error_row(g, "PC_phones")
        self.assert_error_row(g, "PC_trust")
        self.assertEqual(len(g.views["PC_devices"].rows), 1)
        self.assertIn("PC_copies", g.views)

    def test_package_copy_closed_when_build_fails(self):
        with WorkFolders() as w, tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=self.META, folders=[self.OK_FOLDER])
            with mock.patch.object(D, "_db_summary", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    D.build(lc, R.Graph(), R.View, R.ts_seconds, R.iso)
            self.assertEqual(w.left(), [])

    def test_package_copy_init_removes_work_folder_when_copy_fails(self):
        with WorkFolders() as w, tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=self.META, trust=self.TRUST, folders=[self.OK_FOLDER])
            real = D.shutil.copy2
            calls = []

            def flaky(a, b, *x, **k):
                calls.append(a)
                if len(calls) == 3:
                    raise OSError("disk full")
                return real(a, b, *x, **k)

            with mock.patch.object(D.shutil, "copy2", flaky):
                with self.assertRaises(OSError):
                    D.PackageCopy(lc)
            self.assertGreaterEqual(len(calls), 3)
            self.assertEqual(w.left(), [])


OLD = calendar.timegm((2026, 3, 1, 9, 0, 0))     # a time well before any test run


def age_tree(root, start=OLD):
    """Give every file and folder under root an old modification time, as on a phone linked months ago."""
    n = 0
    for dirpath, dirs, files in os.walk(root, topdown=False):
        for name in files + dirs:
            n += 1
            os.utime(os.path.join(dirpath, name), (start + n * 60, start + n * 60))


def copy_reversed(src_lc, dst_lc):
    """Copy a LocalCache the way an examiner's file copy does: new creation times, the newest folder first."""
    os.makedirs(os.path.join(dst_lc, "Indexed"))
    for name in os.listdir(src_lc):
        if os.path.isfile(os.path.join(src_lc, name)):
            shutil.copy2(os.path.join(src_lc, name), os.path.join(dst_lc, name))
    for name in sorted(os.listdir(os.path.join(src_lc, "Indexed")), reverse=True):
        shutil.copytree(os.path.join(src_lc, "Indexed", name), os.path.join(dst_lc, "Indexed", name))


class FolderTimeCheck(unittest.TestCase):
    """The heuristics that decide whether folder creation times are copy times. Times are injected."""
    T = calendar.timegm((2026, 9, 1, 12, 0, 0))

    def check(self, folders, meta_times=()):
        return D._time_check(folders, list(meta_times))

    def f(self, created, modified=None, newest=None):
        return {"created": created, "modified": created if modified is None else modified,
                "newest_file": newest, "files": []}

    def test_consistent_live_times_raise_no_warning(self):
        warn, notes = self.check({G[0]: self.f(self.T, newest=self.T + 3600),
                                  G[1]: self.f(self.T + 86400, self.T + 86400 + 5),
                                  G[2]: self.f(self.T + 90000, newest=self.T + 99999)},
                                 [self.T - 5, self.T + 90000 + 100])
        self.assertEqual(warn, "")
        self.assertFalse(any(notes.values()), notes)

    def test_created_after_the_newest_file_inside(self):
        warn, notes = self.check({G[0]: self.f(self.T + 86400, newest=self.T),
                                  G[1]: self.f(self.T, newest=self.T + 10)})
        self.assertIn("newest file", warn)
        self.assertIn("newest file", notes[G[0]])
        self.assertEqual(notes[G[1]], "")

    def test_created_after_the_folder_was_last_modified(self):
        warn, notes = self.check({G[0]: self.f(self.T + 86400, modified=self.T),
                                  G[1]: self.f(self.T + 999999)})
        self.assertIn("last modified", warn)
        self.assertIn("last modified", notes[G[0]])

    def test_all_folders_created_together(self):
        warn, _ = self.check({G[0]: self.f(self.T), G[1]: self.f(self.T + 0.4), G[2]: self.f(self.T + 0.8)})
        self.assertIn("within", warn)
        self.assertEqual(self.check({G[0]: self.f(self.T)})[0], "")      # one folder alone proves nothing

    def test_all_folders_created_after_every_metadata_time(self):
        warn, _ = self.check({G[0]: self.f(self.T + 86400), G[1]: self.f(self.T + 2 * 86400)}, [self.T, self.T + 60])
        self.assertIn("metadata", warn)
        self.assertEqual(self.check({G[0]: self.f(self.T + 86400), G[1]: self.f(self.T + 2 * 86400)},
                                    [self.T, self.T + 86400 - 30])[0], "")   # a phone registered just before one


class CopyTimeEvidence(unittest.TestCase):
    """An evidence copy made by Explorer or copytree: folder creation times are the copy time, not the pairing time."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        src = D.find_localcache(pldemo.build_package(os.path.join(cls.tmp.name, "src")))
        age_tree(os.path.join(src, "Indexed"))
        cls.lc = os.path.join(cls.tmp.name, "copy", "LocalCache")
        copy_reversed(src, cls.lc)
        cls.g = R.Graph()
        with WorkFolders():
            _, pc = D.build(cls.lc, cls.g, R.View, R.ts_seconds, R.iso)
            pc.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_warning_is_shown(self):
        for vid in ("PC_devices", "PR3", "PR4"):
            self.assertIn("look like the time the evidence was copied", self.g.views[vid].basis, vid)

    def test_no_phone_linked_events_from_folder_times(self):
        events = [r["Event"] for r in self.g.views["PR4"].rows]
        self.assertFalse([e for e in events if "linked" in e or "folder created" in e], events)
        self.assertTrue(any("registered" in e for e in events), events)   # metadata times are still history

    def test_no_time_matches_but_the_stored_link_holds(self):
        rows = self.g.views["PR3"].rows
        self.assertEqual([r["Basis"] for r in rows], ["stored: Metadata.Id = pairing folder GUID"])
        self.assertEqual(rows[0]["GapSeconds"], "")
        links = {r["MetadataID"]: [t for rid, t, _ in self.g.links(r["_e"]) if rid == "PR3"]
                 for r in self.g.views["PC_phones"].rows if r.get("MetadataID")}
        self.assertEqual(links.pop(pldemo.DEMO_PHONE_ID), [("pairing", pldemo.PAIRING_GUIDS[2])])
        self.assertEqual(set(map(tuple, links.values())), {()})       # nothing matched by time

    def test_numbering_does_not_claim_pairing_order(self):
        rows = self.g.views["PC_devices"].rows
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertIn("not pairing order", self.g.label[r["_e"]])
            self.assertTrue(r["TimeCheck"].startswith("looks like copy time"), r)
            self.assertNotIn(r["_e"], self.g.time)          # an unreliable time is not used for "close together"


T0 = calendar.timegm((2026, 9, 1, 12, 0, 0))


def at(ts):
    """Epoch seconds -> the metadata's ISO form."""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def entry(mid, name, os_name, reg, os_version="1"):
    return {"IsLinked": True, "Metadata": {"Id": mid, "RegitrationTime": at(reg), "LastSeenTime": at(reg + 30),
                                           "Metadata": {"DisplayName": name, "OsName": os_name, "OsVersion": os_version}}}


def build_with_folder_times(meta, created):
    """Build from a synthetic package whose pairing folders read as created at created[guid] (injected, live-style:
    the folder's modified time equals its creation time and its files are newer)."""
    folders = [(guid, {"System/Database/phone.db": [("CREATE TABLE message (id INTEGER, timestamp INTEGER)", [])]})
               for guid in created]
    with WorkFolders(), tempfile.TemporaryDirectory() as tmp:
        lc = make_pkg(tmp, meta=meta, folders=folders)

        def times(path):
            t = created[os.path.basename(path)]
            return t, t

        with mock.patch.object(D, "_folder_times", times):
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
        pc.close()
    return g


def pr3_links(g):
    return [(a, b) for a in list(g.label) if a[0] == "phone" for rid, b, _ in g.links(a) if rid == "PR3"]


class DeviceKinds(unittest.TestCase):
    """DeviceMetadataStorage.json lists this PC (or another Windows companion) as well as phones."""

    def test_windows_entry_is_a_pc_never_matched_by_time_never_called_phone(self):
        meta = {"DeviceMetadatas": {"HWPC": [entry("pc-1", "MYPC", "Windows", T0, "10.0.26200")]}}
        g = build_with_folder_times(meta, {G[0]: T0 + 75})     # the PC registered 75 s before the phone's folder
        row = g.views["PC_phones"].rows[0]
        self.assertIn("Windows PC", row["Kind"])
        label = g.label[row["_e"]]
        self.assertFalse(label.lower().startswith("phone"), label)
        self.assertIn("not a phone", label)
        self.assertEqual(g.views["PR3"].rows, [])
        self.assertEqual(pr3_links(g), [])
        for r in g.views["PR4"].rows:
            if "MYPC" in r["Detail"]:
                self.assertNotIn("phone", r["Event"].lower(), r)
                self.assertTrue(r["Event"].startswith("Windows PC"), r)

    def test_same_times_with_an_android_phone_are_matched(self):
        # control for the test above: the timing alone would have matched
        meta = {"DeviceMetadatas": {"HW1": [entry("ph-1", "P1", "Android", T0)]}}
        g = build_with_folder_times(meta, {G[0]: T0 + 75})
        self.assertEqual(g.views["PC_phones"].rows[0]["Kind"], "phone")
        self.assertEqual(len(g.views["PR3"].rows), 1)

    def test_entry_without_an_os_is_not_called_a_phone_or_matched(self):
        e = entry("x-1", "Thing", None, T0)
        del e["Metadata"]["Metadata"]["OsName"]
        g = build_with_folder_times({"DeviceMetadatas": {"HWX": [e]}}, {G[0]: T0 + 75})
        row = g.views["PC_phones"].rows[0]
        self.assertIn("OS not recorded", row["Kind"])
        self.assertFalse(g.label[row["_e"]].lower().startswith("phone"))
        self.assertEqual(g.views["PR3"].rows, [])

    def test_view_does_not_promise_phone_details(self):
        g = build_with_folder_times({"DeviceMetadatas": {"HWPC": [entry("pc-1", "MYPC", "Windows", T0)]}}, {})
        v = g.views["PC_phones"]
        self.assertEqual(v.title, "Linked devices (DeviceMetadataStorage.json)")
        self.assertNotIn("make, model, Android", v.basis)
        self.assertIn("Kind", v.cols)

    def test_empty_hardware_id_list_is_an_explicit_placeholder_row(self):
        g = build_with_folder_times({"DeviceMetadatas": {"": [], "HW9": []}}, {})
        rows = g.views["PC_phones"].rows
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["Kind"], "empty placeholder - no entries")
            self.assertTrue(all(r.get(c) is not None for c in g.views["PC_phones"].cols), r)
            self.assertNotIn("None", " ".join(str(r[c]) for c in g.views["PC_phones"].cols))
            self.assertFalse(g.label[r["_e"]].lower().startswith("phone"), g.label[r["_e"]])
        self.assertIn("(empty)", [r["HardwareID"] for r in rows])


class AmbiguousTimeMatch(unittest.TestCase):
    """The time match must be one-to-one; anything else is listed as ambiguous and not linked."""

    def test_two_phones_near_one_folder_are_ambiguous(self):
        meta = {"DeviceMetadatas": {"HW1": [entry("ph-1", "P1", "Android", T0 - 240)],
                                    "HW2": [entry("ph-2", "P2", "Android", T0 - 120)]}}
        g = build_with_folder_times(meta, {G[0]: T0})
        self.assertEqual(pr3_links(g), [])
        rows = g.views["PR3"].rows
        self.assertEqual(sorted(r["Phone"] for r in rows), ["phone P1", "phone P2"])
        for r in rows:
            self.assertTrue(r["Basis"].startswith("AMBIGUOUS"), r)
            self.assertIn("2 phones", r["Basis"])

    def test_one_phone_near_two_folders_is_ambiguous(self):
        meta = {"DeviceMetadatas": {"HW1": [entry("ph-1", "P1", "Android", T0)]}}
        g = build_with_folder_times(meta, {G[0]: T0 + 60, G[1]: T0 + 180})
        self.assertEqual(pr3_links(g), [])
        rows = g.views["PR3"].rows
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["Basis"].startswith("AMBIGUOUS") and "2 folders" in r["Basis"] for r in rows), rows)

    def test_separate_pairings_each_match_their_own_folder(self):
        meta = {"DeviceMetadatas": {"HW1": [entry("ph-1", "P1", "Android", T0)],
                                    "HW2": [entry("ph-2", "P2", "Android", T0 + 86400)]}}
        g = build_with_folder_times(meta, {G[0]: T0 + 60, G[1]: T0 + 86460})
        self.assertEqual(sorted((a[1], b[1]) for a, b in pr3_links(g)), [("ph-1", G[0]), ("ph-2", G[1])])
        self.assertTrue(all(r["Basis"].startswith("inferred") for r in g.views["PR3"].rows))


class StoredFolderLink(unittest.TestCase):
    """DeviceMetadataStorage.json Metadata.Id is the pairing folder's GUID: a stored link, not a time guess."""

    def test_metadata_id_equal_to_a_folder_guid_is_a_stored_link(self):
        # registered five days before the folder was created: no time match could make this link
        # (the PC entry registered near the folders keeps the folder times trusted - see _time_check, check 4)
        meta = {"DeviceMetadatas": {"HW1": [entry("{%s}" % G[0].upper(), "P1", "Android", T0 - 5 * 86400)],
                                    "HWPC": [entry("pc-1", "MYPC", "Windows", T0 - 30)]}}
        g = build_with_folder_times(meta, {G[0]: T0, G[1]: T0 + 86400})
        self.assertNotIn("WARNING", g.views["PR3"].basis)
        self.assertEqual([b for a, b in pr3_links(g)], [("pairing", G[0])])
        rows = g.views["PR3"].rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Basis"], "stored: Metadata.Id = pairing folder GUID")
        basis = g.views["PR3"].basis
        self.assertNotIn("keeps no folder GUID", basis)
        self.assertIn("Metadata.Id", basis)
        self.assertIn("fallback", basis)
        self.assertIn("Metadata.Id", g.views["PC_phones"].basis)

    def test_stored_link_survives_copy_time_folder_dates(self):
        meta = {"DeviceMetadatas": {"HW1": [entry(G[1], "P1", "Android", T0)],
                                    "HW2": [entry("ph-2", "P2", "Android", T0 - 60)]}}
        g = build_with_folder_times(meta, {G[0]: T0 + 0.1, G[1]: T0 + 0.2})   # created together: copy times
        self.assertIn("look like the time the evidence was copied", g.views["PR3"].basis)
        self.assertEqual(sorted((a[1], b[1]) for a, b in pr3_links(g)), [(G[1], G[1])])
        row = g.views["PR3"].rows[0]
        self.assertTrue(row["Basis"].startswith("stored"), row)
        self.assertEqual(row["GapSeconds"], "")            # a copy time gives no gap

    def test_time_fallback_only_for_entries_without_a_folder_id_and_only_free_folders(self):
        meta = {"DeviceMetadatas": {"HW1": [entry(G[0], "P1", "Android", T0 + 86400 - 60)],   # stored: G[0]
                                    "HW2": [entry("ph-2", "P2", "Android", T0 - 60)],          # near G[0] only
                                    "HW3": [entry("ph-3", "P3", "Android", T0 + 2 * 86400 - 60)]}}
        g = build_with_folder_times(meta, {G[0]: T0, G[1]: T0 + 86400, G[2]: T0 + 2 * 86400})
        # P1 registered 60 s before G[1] but its Id names G[0]: never matched to G[1] by time
        # P2's only nearby folder is G[0], which the stored link already explains: no match
        self.assertEqual(sorted((a[1], b[1]) for a, b in pr3_links(g)), [(G[0], G[0]), ("ph-3", G[2])])
        rows = {r["Phone"]: r for r in g.views["PR3"].rows}
        self.assertEqual(set(rows), {"phone P1", "phone P3"})
        self.assertTrue(rows["phone P3"]["Basis"].startswith("inferred by time (fallback"), rows)
        self.assertIn("not a GUID", rows["phone P3"]["Basis"])

    def test_guid_id_naming_no_folder_is_never_matched_by_time(self):
        # the Id is the folder name, so this phone's folder is gone: G[0] is another pairing, whatever its time
        meta = {"DeviceMetadatas": {"HW1": [entry("{%s}" % G[5].upper(), "P1", "Android", T0 - 60)]}}
        g = build_with_folder_times(meta, {G[0]: T0})
        self.assertNotIn("WARNING", g.views["PR3"].basis)       # times are trusted: only the rule keeps it out
        self.assertEqual(pr3_links(g), [])
        rows = g.views["PR3"].rows
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["Phone"], "phone P1")
        self.assertEqual(rows[0]["Pairing"], "")
        self.assertEqual(rows[0]["Basis"], D.NO_SUCH_FOLDER)
        self.assertIn("no such folder exists", rows[0]["Basis"])
        self.assertIn("not matched", rows[0]["Basis"])
        self.assertIn("missing or not a GUID", g.views["PR3"].basis)

    def test_guid_id_naming_no_folder_is_said_even_on_copy_time_folders(self):
        meta = {"DeviceMetadatas": {"HW1": [entry(G[5], "P1", "Android", T0)]}}
        g = build_with_folder_times(meta, {G[0]: T0 + 0.1, G[1]: T0 + 0.2})   # created together: copy times
        self.assertEqual([r["Basis"] for r in g.views["PR3"].rows], [D.NO_SUCH_FOLDER])
        self.assertEqual(pr3_links(g), [])

    def test_history_names_the_phone_of_a_stored_folder(self):
        meta = {"DeviceMetadatas": {"HW1": [entry(G[0], "P1", "Android", T0 - 60)]}}
        g = build_with_folder_times(meta, {G[0]: T0, G[1]: T0 + 86400})
        made =[r["Detail"] for r in g.views["PR4"].rows if r["Event"].startswith("pairing folder created")]
        self.assertEqual(len(made), 2)
        self.assertTrue(made[0].startswith("pairing #1"), made)      # G[0], created first
        self.assertIn("phone P1", made[0])
        self.assertIn("Metadata.Id", made[0])
        self.assertIn("no metadata entry has this folder's GUID", made[1])


class SourceChangesDuringCopy(unittest.TestCase):
    """A live package keeps writing: one moving file must not throw away the whole linked-phones section."""
    META = {"DeviceMetadatas": {"HW1": [{"IsLinked": True, "Metadata": {"Id": "id-1", "Metadata": {
        "DisplayName": "P1", "OsName": "Android"}}}]}}

    def test_file_changing_during_copy_is_marked_and_the_section_is_still_built(self):
        with WorkFolders() as w, tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=self.META, folders=[BadSources.OK_FOLDER, (G[1], {"notes.txt": b"hello"})])
            real = D.shutil.copy2

            def growing(a, b, *x, **k):
                out = real(a, b, *x, **k)
                if a.endswith("notes.txt"):          # the source is written to while it is being copied
                    with open(a, "ab") as f:
                        f.write(b"more")
                return out

            with mock.patch.object(D.shutil, "copy2", growing):
                g = R.Graph()
                _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
            self.assertEqual(w.left(), [])
        rows = {r["file"]: r for r in g.views["PC_copies"].rows}
        moving = next(r for f, r in rows.items() if f.endswith("notes.txt"))
        self.assertEqual(moving["copy_matches"], "n/a - source changed during copy")
        self.assertEqual(moving["source_sha256"], "")
        self.assertTrue(moving["copy_sha256"])
        self.assertTrue(all(r["copy_matches"] == "yes" for f, r in rows.items() if not f.endswith("notes.txt")))
        self.assertEqual(len(g.views["PC_devices"].rows), 2)
        self.assertEqual(len(g.views["PC_phones"].rows), 1)
        for vid in ("PC_copies", "PC_devices"):
            self.assertIn("changed while it was being copied", g.views[vid].basis, vid)
            self.assertIn("notes.txt", g.views[vid].basis, vid)

    def test_unreadable_source_is_an_error_row_not_an_error_section(self):
        real = D._sha

        def sha(path):
            if path.endswith("notes.txt") and "pldevices-" not in path:
                raise PermissionError(13, "locked")
            return real(path)

        with WorkFolders() as w, tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=self.META, folders=[BadSources.OK_FOLDER, (G[1], {"notes.txt": b"hello"})])
            with mock.patch.object(D, "_sha", sha):
                g = R.Graph()
                _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
            self.assertEqual(w.left(), [])
        bad = next(r for r in g.views["PC_copies"].rows if r["file"].endswith("notes.txt"))
        self.assertIn("source could not be read", bad["copy_matches"])
        self.assertEqual(len(g.views["PC_devices"].rows), 2)
        self.assertIn("could not be read", g.views["PC_devices"].basis)

    def test_copy_that_differs_from_a_settled_source_still_fails(self):
        real = D._sha

        def sha(path):
            return "0" * 64 if "pldevices-" in path else real(path)   # every copy reads back wrong

        with WorkFolders() as w, tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=self.META, folders=[BadSources.OK_FOLDER])
            with mock.patch.object(D, "_sha", sha):
                with self.assertRaises(IOError):
                    D.PackageCopy(lc)
            self.assertEqual(w.left(), [])


def wal_states(tmp):
    """Two moments of one WAL-mode phone.db, as {'': db bytes, '-wal': wal bytes}:
    s1 - 3 messages, all still in the WAL; s2 - those 3 checkpointed into the .db, then 2 conversations in the WAL.
    An s1 .db with an s2 -wal would show 0 messages and 2 conversations: a set from two moments."""
    src = os.path.join(tmp, "make", "phone.db")
    os.makedirs(os.path.dirname(src))
    c = sqlite3.connect(src)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE message (id INTEGER, timestamp INTEGER)")
    c.execute("CREATE TABLE conversation (id INTEGER)")
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def snap():
        out = {}
        for suf in ("", "-wal"):
            with open(src + suf, "rb") as f:
                out[suf] = f.read()
        return out

    c.executemany("INSERT INTO message VALUES (?, 0)", [(i,) for i in range(3)])
    c.commit()
    s1 = snap()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.executemany("INSERT INTO conversation VALUES (?)", [(i,) for i in range(2)])
    c.commit()
    s2 = snap()
    c.close()
    return s1, s2


def put(db_path, state):
    for suf, data in state.items():
        with open(db_path + suf, "wb") as f:
            f.write(data)


class DatabaseCopiedAsASet(unittest.TestCase):
    """A .db and its -wal are copied as one set, with the before/after check over the set (as plparse.stage)."""

    def run_with(self, change):
        with WorkFolders(), tempfile.TemporaryDirectory() as tmp:
            s1, s2 = wal_states(tmp)
            lc = make_pkg(tmp, folders=[(G[0], {"System/Database/notes.txt": b"x"})])
            db = os.path.join(lc, "Indexed", G[0], "System", "Database", "phone.db")
            put(db, s1)
            real = D.shutil.copy2
            n = {"wal": 0}

            def copy2(a, b, *x, **k):
                if a.endswith("phone.db-wal"):
                    n["wal"] += 1
                    change(n["wal"], db, s1, s2)       # the app writes after the .db was copied, before the -wal
                return real(a, b, *x, **k)

            with mock.patch.object(D.shutil, "copy2", copy2):
                g = R.Graph()
                _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
            return g, s2

    def test_wal_changing_between_the_db_and_the_wal_is_copied_again_as_a_set(self):
        def once(n, db, s1, s2):
            if n == 1:
                put(db, s2)                            # a checkpoint plus new rows, between the two copies
        g, s2 = self.run_with(once)
        row = g.views["PC_devices"].rows[0]
        self.assertEqual(row["DataRows"], 5, row)      # s2 as a whole: 3 messages + 2 conversations
        rows = {r["file"].replace("\\", "/").split("/")[-1]: r for r in g.views["PC_copies"].rows}
        for name, suf in (("phone.db", ""), ("phone.db-wal", "-wal")):
            self.assertEqual(rows[name]["copy_matches"], "yes", rows[name])
            self.assertEqual(rows[name]["source_sha256"], hashlib.sha256(s2[suf]).hexdigest())
        self.assertNotIn("changed while it was being copied", g.views["PC_devices"].basis)

    def test_a_set_that_never_settles_is_flagged_as_a_whole(self):
        def always(n, db, s1, s2):
            put(db, s2 if n % 2 else s1)
        g, _ = self.run_with(always)
        rows = {r["file"].replace("\\", "/").split("/")[-1]: r for r in g.views["PC_copies"].rows}
        for name in ("phone.db", "phone.db-wal"):
            self.assertEqual(rows[name]["copy_matches"], D.UNSTABLE, rows[name])
            self.assertEqual(rows[name]["source_sha256"], "")
            self.assertTrue(rows[name]["copy_sha256"])
        self.assertEqual(rows["notes.txt"]["copy_matches"], "yes")
        basis = g.views["PC_devices"].basis
        self.assertIn("changed while it was being copied", basis)
        self.assertIn("phone.db-wal", basis)

    def test_shm_is_copied_with_its_set_but_never_verified_against_the_source(self):
        with WorkFolders(), tempfile.TemporaryDirectory() as tmp:
            s1, _ = wal_states(tmp)
            lc = make_pkg(tmp, folders=[(G[0], {})])
            db = os.path.join(lc, "Indexed", G[0], "phone.db")
            put(db, s1)
            with open(db + "-shm", "wb") as f:
                f.write(b"\0" * 64)
            real = D.shutil.copy2

            def copy2(a, b, *x, **k):
                out = real(a, b, *x, **k)
                if a.endswith("-shm"):
                    with open(a, "ab") as f:           # the running app rewrites its shared-memory index
                        f.write(b"\1")
                return out

            with mock.patch.object(D.shutil, "copy2", copy2):
                pc = D.PackageCopy(lc)
            try:
                rows = {r["file"].replace("\\", "/").split("/")[-1]: r for r in pc.verify}
                self.assertTrue(os.path.isfile(pc.path("Indexed", G[0], "phone.db-shm")))
            finally:
                pc.close()
        self.assertEqual(rows["phone.db"]["copy_matches"], "yes")
        self.assertEqual(rows["phone.db-wal"]["copy_matches"], "yes")
        self.assertEqual(rows["phone.db-shm"]["source_sha256"], "")
        self.assertTrue(rows["phone.db-shm"]["copy_sha256"])
        self.assertIn("not verified", rows["phone.db-shm"]["copy_matches"])
        self.assertEqual(pc.unstable, [])


class Links(unittest.TestCase):
    def link(self, target, link):
        try:
            import _winapi
            _winapi.CreateJunction(target, link)
            return
        except (ImportError, AttributeError, OSError):
            pass
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("neither a junction nor a symlink can be made here")

    def test_junctions_and_symlinks_are_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside)
            with open(os.path.join(outside, "secret.db"), "wb") as f:
                f.write(b"outside the package")
            ok = (G[0], {"System/Database/phone.db": [("CREATE TABLE message (id INTEGER)", [])]})
            lc = make_pkg(tmp, folders=[ok])
            self.link(outside, os.path.join(lc, "Indexed", G[0], "linked"))     # a link inside a pairing folder
            self.link(outside, os.path.join(lc, "Indexed", G[1]))              # a pairing folder that is a link
            with WorkFolders():
                g = R.Graph()
                _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
                try:
                    copied = [os.path.join(r, f) for r, _, fs in os.walk(pc.work) for f in fs]
                    self.assertFalse([c for c in copied if "secret" in c], copied)
                    self.assertEqual([r["DeviceFolder"] for r in g.views["PC_devices"].rows], [G[0]])
                    report = " | ".join(f"{r['file']} {r['copy_matches']}" for r in g.views["PC_copies"].rows)
                    self.assertIn("linked", report)
                    self.assertIn(G[1], report)
                    self.assertIn("not followed", report)
                finally:
                    pc.close()
            self.assertTrue(os.path.isfile(os.path.join(outside, "secret.db")))   # the target is untouched


class TrustMatch(unittest.TestCase):
    def test_partner_id_must_end_with_underscore_and_id(self):
        meta = {"DeviceMetadatas": {"HW1": [{"Metadata": {"Id": "abc", "Metadata": {"DisplayName": "P1"}}}],
                                    "HW2": [{"Metadata": {"Id": "a_b_c", "Metadata": {"DisplayName": "P2"}}}]}}
        trust = {"TrustedClients": {"t1": {"PartnerClientId": "pre_a_b_c"},      # id contains '_'
                                    "t2": {"PartnerClientId": "pre_xabc"}}}      # only ends with the same letters
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=meta, trust=trust)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        self.assertEqual([t for rid, t, _ in g.links(("phone", "a_b_c")) if rid == "PR1"], [("trust", "t1")])
        self.assertEqual([t for rid, t, _ in g.links(("phone", "abc")) if rid == "PR1"], [])

    def test_braces_and_case_do_not_hide_the_trust_link(self):
        meta = {"DeviceMetadatas": {"HW1": [{"Metadata": {"Id": "{%s}" % G[0].upper(), "Metadata": {"DisplayName": "P1"}}}]}}
        trust = {"TrustedClients": {"t1": {"PartnerClientId": "pre_" + G[0]},
                                    "t2": {"PartnerClientId": "pre_{%s}" % G[0]}}}
        with WorkFolders(), tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=meta, trust=trust)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        phone = g.views["PC_phones"].rows[0]["_e"]
        self.assertEqual(sorted(t for rid, t, _ in g.links(phone) if rid == "PR1"), [("trust", "t1"), ("trust", "t2")])


class LastSeenInHistory(unittest.TestCase):
    def test_last_seen_event_is_in_the_history_with_its_utc_time(self):
        meta = {"DeviceMetadatas": {"HW1": [{"Metadata": {
            "Id": "id-1", "RegistrationTime": "2026-09-21T17:00:00+05:30",
            "LastSeenTime": "2026-09-21T17:17:49+05:30", "Metadata": {"DisplayName": "P1", "OsName": "Android"}}}]}}
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=meta)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        self.assertEqual(g.views["PC_phones"].rows[0]["LastSeenUTC"], "2026-09-21 11:47:49")
        seen = [r["TimeUTC"] for r in g.views["PR4"].rows if r["Event"] == "phone last seen (metadata)"]
        self.assertEqual(seen, ["2026-09-21 11:47:49"])

    def test_times_without_a_zone_are_kept_as_no_zone_rows(self):
        meta = {"DeviceMetadatas": {"HW1": [{"Metadata": {
            "Id": "id-1", "RegistrationTime": "2026-09-21T17:00:00",
            "LastSeenTime": "2026-09-21T17:17:49+05:30", "Metadata": {"DisplayName": "P1", "OsName": "Android"}}}]}}
        trust = {"TrustedClients": {"t1": {"PartnerClientId": "pre_id-1", "RelationshipLastAccessTime": "2026-09-22T08:00:00"}}}
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=meta, trust=trust)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        rows = g.views["PR4"].rows
        reg = [r for r in rows if r["Event"].startswith("phone registered")]
        self.assertEqual([r["TimeUTC"] for r in reg], ["2026-09-21 17:00:00 (no zone)"])
        self.assertIn("no zone", reg[0]["Event"])
        last = [r for r in rows if r["Event"].startswith("trust relationship last used")]
        self.assertEqual([r["TimeUTC"] for r in last], ["2026-09-22 08:00:00 (no zone)"])
        # zoned rows first, in time order; the zone-less ones after them (they cannot be placed in UTC order)
        self.assertEqual(rows[0]["TimeUTC"], "2026-09-21 11:47:49")
        self.assertTrue(all("(no zone)" in r["TimeUTC"] for r in rows[1:]), rows)


class MetadataEntriesWithoutId(unittest.TestCase):
    def test_two_entries_under_one_hardware_id_stay_two_entities(self):
        meta = {"DeviceMetadatas": {"HW1": [{"Metadata": {"Metadata": {"DisplayName": "P1", "OsName": "Android"}}},
                                            {"Metadata": {"Metadata": {"DisplayName": "P2", "OsName": "Android"}}}]}}
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=meta)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        rows = g.views["PC_phones"].rows
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["_e"], rows[1]["_e"])
        self.assertEqual({g.label[r["_e"]] for r in rows}, {"phone P1", "phone P2"})
        self.assertEqual({g.home[r["_e"]] for r in rows}, {("PC_phones", 0), ("PC_phones", 1)})


class StreamedAppsWithoutAPairingFolder(unittest.TestCase):
    def test_unmatched_device_id_is_flagged(self):
        apps = [("CREATE TABLE hosted_app_ids (row_id INTEGER, device_id TEXT, package_id TEXT)",
                 [f"INSERT INTO hosted_app_ids VALUES (1, '{G[0].upper()}', 'com.example.one')",
                  f"INSERT INTO hosted_app_ids VALUES (2, '{G[6]}', 'com.example.two')"])]
        folders = [(G[0], {"System/Database/phone.db": [("CREATE TABLE message (id INTEGER)", [])]}),
                   ("FullTrust", {"phone_apps.db": apps})]
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, folders=folders)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        v = g.views["PC_streamed"]
        self.assertIn("PairingFolder", v.cols)
        got = {r["package_id"]: r["PairingFolder"] for r in v.rows}
        self.assertTrue(got["com.example.one"].startswith("pairing #1"), got)
        self.assertTrue(got["com.example.two"].startswith("NO pairing folder"), got)
        self.assertIn("NO pairing folder", v.basis)

    def test_braced_device_id_is_the_same_folder(self):
        # plparse.owns_ft_row ignores case and braces; pldevices must credit the same row to the same folder
        apps = [("CREATE TABLE hosted_app_ids (row_id INTEGER, device_id TEXT, package_id TEXT)",
                 [f"INSERT INTO hosted_app_ids VALUES (1, '{{{G[0].upper()}}}', 'com.example.one')"])]
        folders = [(G[0], {"System/Database/phone.db": [("CREATE TABLE message (id INTEGER)", [])]}),
                   ("FullTrust", {"phone_apps.db": apps})]
        with WorkFolders(), tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, folders=folders)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        self.assertTrue(D.plparse.owns_ft_row("{%s}" % G[0].upper(), G[0]))
        self.assertTrue(g.views["PC_streamed"].rows[0]["PairingFolder"].startswith("pairing #1"),
                        g.views["PC_streamed"].rows)
        self.assertEqual(g.views["PC_devices"].rows[0]["StreamedApps"], 1)
        pairing = g.views["PC_devices"].rows[0]["_e"]
        self.assertEqual(len([t for rid, t, _ in g.links(pairing) if rid == "PR2"]), 1)

    def test_one_normaliser_for_ids(self):
        self.assertEqual(D.norm_id(" {%s} " % G[0].upper()), G[0])
        self.assertEqual(D.norm_id(None), "")


class LinkDetection(unittest.TestCase):
    def test_reparse_point_attribute_is_a_link_even_without_isjunction(self):
        # Python 3.8-3.11 has no os.path.isjunction and lstat does not flag junctions: the attribute must
        fake = types.SimpleNamespace(st_mode=0o040000, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        plain = types.SimpleNamespace(st_mode=0o040000, st_file_attributes=stat.FILE_ATTRIBUTE_DIRECTORY)
        with mock.patch.object(D.os, "lstat", return_value=fake),                 mock.patch.object(D.os.path, "isjunction", None, create=True):
            self.assertTrue(D._is_link("x"))
        with mock.patch.object(D.os, "lstat", return_value=plain),                 mock.patch.object(D.os.path, "isjunction", None, create=True):
            self.assertFalse(D._is_link("x"))


class LinkedPhones(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = pldemo.build_package(cls.tmp.name)
        cls.lc = D.find_localcache(cls.root)
        cls.before = tree_hashes(cls.root)
        cls.g = R.Graph()
        cls.vids, cls.pc = D.build(cls.lc, cls.g, R.View, R.ts_seconds, R.iso)
        cls.work = cls.pc.work

    @classmethod
    def tearDownClass(cls):
        cls.pc.close()   # the class-level package copy, or every run leaves one in %TEMP%
        cls.tmp.cleanup()

    def rows(self, vid):
        return self.g.views[vid].rows

    def test_finds_localcache_from_any_input_shape(self):
        idx = os.path.join(self.lc, "Indexed")
        dbdir = os.path.join(idx, pldemo.PAIRING_GUIDS[2], "System", "Database")
        for p in (self.root, self.lc, idx, dbdir):
            self.assertEqual(os.path.normcase(D.find_localcache(p)), os.path.normcase(self.lc), p)
        with tempfile.TemporaryDirectory() as other:
            self.assertIsNone(D.find_localcache(other))

    def test_copies_verified_keys_not_copied_source_untouched(self):
        self.assertTrue(self.pc.verify)
        self.assertTrue(all(v["copy_matches"] == "yes" for v in self.pc.verify))
        self.assertFalse(any("PlatformEncryptedKeyStorage" in v["file"] for v in self.pc.verify))
        self.assertFalse(os.path.exists(os.path.join(self.work, "PlatformEncryptedKeyStorage.json")))
        self.assertEqual(tree_hashes(self.root), self.before)

    def test_every_pairing_in_order_with_status(self):
        got = [(r["#"], r["DeviceFolder"], r["Status"]) for r in self.rows("PC_devices")]
        self.assertEqual(got, [(1, pldemo.PAIRING_GUIDS[0], "folder only - its data was removed"),
                               (2, pldemo.PAIRING_GUIDS[1], "databases present, no rows"),
                               (3, pldemo.PAIRING_GUIDS[2], "data present")])
        cur = self.rows("PC_devices")[2]
        self.assertGreater(cur["DataRows"], 0)
        self.assertEqual(cur["SIM"], "Example Carrier +1-555-0100")
        self.assertEqual(cur["StreamedApps"], 2)
        self.assertTrue(cur["FirstActivityUTC"] <= cur["LastActivityUTC"])
        self.assertTrue(cur["FirstActivityUTC"].startswith("2030-01-01"))

    def test_phone_details_including_removed_one(self):
        phones = {r["HardwareID"]: r for r in self.rows("PC_phones")}
        self.assertIn("details removed", phones["00000000EXAMPLE1"]["Note"])
        p = phones["00000000EXAMPLE2"]
        self.assertEqual((p["DisplayName"], p["Manufacturer"], p["Model"], p["OS"], p["Linked"]),
                         ("Example Phone", "ExampleCorp", "EX-1", "Android 99", "yes"))
        self.assertEqual(p["Certificates"], 1)
        self.assertNotIn("example-cert", str(p))  # certificate bodies are never shown
        self.assertTrue(p["RegisteredUTC"])
        # as on a real package: the phone's Metadata.Id is its pairing folder's GUID
        self.assertEqual(p["MetadataID"], pldemo.PAIRING_GUIDS[2])
        self.assertEqual(pldemo.DEMO_PHONE_ID, pldemo.PAIRING_GUIDS[2])
        pc = phones[pldemo.DEMO_PC_HW]
        self.assertIn("Windows PC", pc["Kind"])
        self.assertNotIn(D.norm_id(pc["MetadataID"]), [D.norm_id(x) for x in pldemo.PAIRING_GUIDS])
        old = phones[pldemo.DEMO_OLD_PHONE_HW]
        self.assertEqual(old["Kind"], "phone")
        self.assertFalse(D.GUID.match(old["MetadataID"]), old)     # not a GUID: only the time fallback can place it

    def test_engine_credits_the_demo_phone_to_its_own_folder(self):
        entries, err = D.plparse.read_metadata(os.path.join(self.lc, D.META), [])
        self.assertEqual(err, "")
        mine, _ = D.plparse.device_metadata(pldemo.PAIRING_GUIDS[2], entries, err, "x")
        self.assertEqual([e["model"] for e in mine["entries"]], ["EX-1"])
        self.assertNotIn("no metadata entry matches", mine["note"])
        for guid in pldemo.PAIRING_GUIDS[:2]:      # the older pairings' entries were removed (or name no folder)
            self.assertEqual(D.plparse.device_metadata(guid, entries, err, "x")[0]["entries"], [])

    def test_trust_and_stored_links(self):
        t = self.rows("PC_trust")[0]
        self.assertEqual((t["TrustType"], t["MsaCid"]), (3, "demo-msa-0001"))
        phone = ("phone", pldemo.DEMO_PHONE_ID)
        self.assertEqual([x for rid, x, _ in self.g.links(phone) if rid == "PR1"], [("trust", pldemo.DEMO_PHONE_ID)])
        pairing = ("pairing", pldemo.PAIRING_GUIDS[2])
        self.assertEqual(len([x for rid, x, _ in self.g.links(pairing) if rid == "PR2"]), 2)

    def test_stored_link_for_the_phone_and_time_fallback_for_the_entry_without_a_folder_id(self):
        links = {a[1]: b[1] for a in list(self.g.label) if a[0] == "phone"
                 for rid, b, _ in self.g.links(a) if rid == "PR3"}
        self.assertEqual(links, {pldemo.DEMO_PHONE_ID: pldemo.PAIRING_GUIDS[2],
                                 pldemo.DEMO_OLD_PHONE_ID: pldemo.PAIRING_GUIDS[1]})
        rel = {r["Phone"]: r for r in self.rows("PR3")}
        self.assertEqual(len(rel), 2, rel)
        stored = rel["phone Example Phone"]
        self.assertIn(pldemo.PAIRING_GUIDS[2][:8], stored["Pairing"])
        self.assertEqual(stored["Basis"], "stored: Metadata.Id = pairing folder GUID")
        fallback = rel["phone " + pldemo.DEMO_OLD_PHONE_NAME]
        self.assertIn(pldemo.PAIRING_GUIDS[1][:8], fallback["Pairing"])
        self.assertTrue(fallback["Basis"].startswith("inferred by time (fallback"), fallback)
        self.assertLessEqual(fallback["GapSeconds"], D.INFER_WINDOW_S)
        self.assertFalse([r for r in rel.values() if "Windows" in r["Phone"]])   # the PC is never matched

    def test_history_in_time_order(self):
        t = [r["TimeUTC"] for r in self.rows("PR4")]
        self.assertEqual(t, sorted(t))
        folder = [r["Event"] for r in self.rows("PR4") if r["Event"].startswith("pairing folder created")]
        self.assertEqual(len(folder), 3)
        self.assertTrue(all("if the folder times were preserved" in e for e in folder), folder)

    def test_live_folder_times_raise_no_copy_warning(self):
        for vid in ("PC_devices", "PR3", "PR4"):
            self.assertNotIn("WARNING", self.g.views[vid].basis, vid)
        self.assertTrue(all(r["TimeCheck"] == "no sign of copy time" for r in self.rows("PC_devices")))
        self.assertTrue(all(r["_e"] in self.g.time for r in self.rows("PC_devices")))

    def test_folder_creation_time_is_worded_as_read(self):
        basis = self.g.views["PC_devices"].basis
        self.assertIn("folder creation time as read", basis)
        self.assertIn("reflects the pairing only if the folders' timestamps were preserved when collected", basis)
        self.assertNotIn("= when that phone was linked", basis)

    def test_close_removes_copies(self):
        g = R.Graph()
        _, pc = D.build(self.lc, g, R.View, R.ts_seconds, R.iso)
        self.assertIsNone(pc.close())
        self.assertFalse(os.path.exists(pc.work))

    def test_single_device_folder_input(self):
        g = R.Graph()
        vids, pc = D.build(None, g, R.View, R.ts_seconds, R.iso)
        self.assertIsNone(pc)
        self.assertEqual(vids, ["PC_devices"])
        self.assertIn("package", g.views["PC_devices"].basis)


if __name__ == "__main__":
    unittest.main()
