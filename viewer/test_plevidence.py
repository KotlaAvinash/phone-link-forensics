"""Tests for the evidence layer: staging, carving and read-only opens. Synthetic data only.

    python -B viewer/test_plevidence.py
"""
import contextlib
import csv
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pldemo  # noqa: E402
import plcarve  # noqa: E402
import plparse as P  # noqa: E402
import plrelate as R  # noqa: E402


def wal_source(folder):
    """A phone.db in WAL mode. Returns the writer connection, kept open so the -wal file survives."""
    os.makedirs(folder, exist_ok=True)
    w = sqlite3.connect(os.path.join(folder, "phone.db"))
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA wal_autocheckpoint=0")
    w.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    w.executemany("INSERT INTO t (v) VALUES (?)", [("row%d" % i,) for i in range(5)])
    w.commit()
    return w


class SnapshotStability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "device")
        self.w = wal_source(self.src)
        self.real_copy = shutil.copy2
        self.db_copies = 0

    def tearDown(self):
        shutil.copy2 = self.real_copy
        self.w.close()
        self.tmp.cleanup()

    def hook(self, after_db_copy):
        def copy2(s, d, *a, **k):
            r = self.real_copy(s, d, *a, **k)
            if os.path.basename(s) == "phone.db":
                self.db_copies += 1
                after_db_copy(self.db_copies)
            return r
        shutil.copy2 = copy2

    def test_quiet_source_is_stable(self):
        ev = R.Evidence(self.src)
        try:
            self.assertEqual(ev.snapshot_note, "")
            self.assertTrue(ev.verify)
            self.assertTrue(all(v["source_stable"] == "yes" for v in ev.verify))
            self.assertEqual(self.db_copies, 0)
        finally:
            ev.close()

    def test_demo_folder_is_stable(self):
        demo = pldemo.build(os.path.join(self.tmp.name, "demo"))
        ev = R.Evidence(demo)
        try:
            self.assertEqual(ev.snapshot_note, "")
        finally:
            ev.close()

    def test_source_that_keeps_changing_is_flagged(self):
        def writer(n):
            self.w.execute("INSERT INTO t (v) VALUES (?)", ("late%d" % n,))
            self.w.commit()
            self.w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.hook(writer)
        ev = R.Evidence(self.src)       # must not raise: the copy is kept, with a warning
        try:
            self.assertEqual(self.db_copies, 3)          # the whole set was retried, then given up
            self.assertIn("phone.db", ev.snapshot_note)
            self.assertTrue(ev.verify)
            self.assertTrue(all(v["source_stable"] == "NO - source changed during copy" for v in ev.verify
                                if v["file"].startswith("phone.db")))
        finally:
            ev.close()

    def test_unstable_set_does_not_claim_a_source_hash(self):
        def writer(n):
            self.w.execute("INSERT INTO t (v) VALUES (?)", ("late%d" % n,))
            self.w.commit()
            self.w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.hook(writer)
        ev = R.Evidence(self.src)
        try:
            row = [a for a in ev.acq if a["file"] == "phone.db"][0]
            self.assertEqual(row["sha256"], "")                  # no source hash was ever settled
            self.assertEqual(row["copy_sha256"], P.sha256(os.path.join(ev.work, "phone.db")))
            for v in ev.verify:
                self.assertEqual(v["source_sha256"], "")
                self.assertEqual(v["copy_matches"], "n/a - source changed during copy")
                self.assertEqual(v["copy_sha256"], P.sha256(os.path.join(ev.work, v["file"])))
            g = R.build(ev)
            view = g.views["P_copies"]
            self.assertEqual(view.cols, ["file", "source_sha256", "copy_sha256", "copy_matches", "source_stable"])
            self.assertTrue(all(r["copy_matches"].startswith("n/a") for r in view.rows))
            self.assertTrue(all(r["source_stable"] == P.UNSTABLE for r in view.rows))
        finally:
            ev.close()

    def test_stable_set_keeps_its_source_hash(self):
        ev = R.Evidence(self.src)
        try:
            row = [a for a in ev.acq if a["file"] == "phone.db"][0]
            self.assertEqual(row["sha256"], P.sha256(os.path.join(self.src, "phone.db")))
            self.assertEqual(row["copy_sha256"], row["sha256"])        # both recorded; the copy was re-hashed
            self.assertEqual(row["copy_matches"], "yes")
            for v in ev.verify:
                self.assertEqual(v["copy_matches"], "yes")
                self.assertTrue(v["source_sha256"])
            g = R.build(ev)
            view = g.views["P_copies"]
            self.assertEqual(view.cols, ["file", "source_sha256", "copy_sha256", "copy_matches", "source_stable"])
            self.assertTrue(all(r["copy_matches"] == "yes" and r["source_stable"] == "yes" for r in view.rows))
        finally:
            ev.close()

    def test_one_off_change_is_retried_and_copy_holds_the_data(self):
        def once(n):
            if n == 1:
                self.w.execute("INSERT INTO t (v) VALUES ('late')")
                self.w.commit()
                self.w.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # WAL emptied between .db and -wal copies
        self.hook(once)
        ev = R.Evidence(self.src)
        try:
            self.assertEqual(self.db_copies, 2)
            self.assertEqual(ev.snapshot_note, "")
            vals = [r["v"] for r in ev.rows("phone.db", "SELECT v FROM t", "t")]
            self.assertIn("late", vals)
            self.assertEqual(len(vals), 6)
        finally:
            ev.close()


class StagedCopyHash(unittest.TestCase):
    """plparse's own staging (the CLI) re-hashes the copy it took and compares it with the source hash."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "device")
        self.w = wal_source(self.src)
        self.real_copy = shutil.copy2

    def tearDown(self):
        shutil.copy2 = self.real_copy
        self.w.close()
        self.tmp.cleanup()

    def test_good_copy_records_both_hashes_and_matches(self):
        acq = []
        P.stage(self.src, os.path.join(self.tmp.name, "work"), acq)
        row = [r for r in acq if r["file"] == "phone.db"][0]
        self.assertEqual(row["source_stable"], "yes")
        self.assertEqual(row["copy_sha256"], row["sha256"])
        self.assertEqual(row["copy_wal_sha256"], row["wal_sha256"])
        self.assertEqual(row["copy_matches"], "yes")

    def corrupt_copies(self):
        def copy2(s, d, *a, **k):
            r = self.real_copy(s, d, *a, **k)
            if os.path.basename(d) == "phone.db":
                with open(d, "r+b") as fh:           # the copy differs from the source, the source is untouched
                    fh.seek(200)
                    fh.write(b"\xAA")
            return r
        shutil.copy2 = copy2

    def test_copy_that_differs_from_its_source_is_flagged(self):
        self.corrupt_copies()
        acq = []
        P.stage(self.src, os.path.join(self.tmp.name, "work"), acq)
        row = [r for r in acq if r["file"] == "phone.db"][0]
        self.assertEqual(row["source_stable"], "yes")
        self.assertNotEqual(row["copy_sha256"], row["sha256"])
        self.assertEqual(row["sha256"], P.sha256(os.path.join(self.src, "phone.db")))
        self.assertTrue(row["copy_matches"].startswith("NO"), row)

    def test_cli_warns_about_a_copy_that_does_not_match(self):
        self.corrupt_copies()
        out = os.path.join(self.tmp.name, "out")
        run_main(["--input", self.src, "--out", out])
        w = read_acq(out, "device")["warnings"]
        self.assertTrue([x for x in w if "phone.db" in x and "does not match" in x], w)


class ChangingShm(unittest.TestCase):
    """The -shm is a shared-memory index a running Phone Link rewrites constantly, so it can change between the copy
    and any later hash of the source. That must not make a good copy look bad."""

    def test_shm_that_changes_after_the_copy_does_not_fail_the_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, src = os.path.join(tmp, "live"), os.path.join(tmp, "src")
            w = wal_source(live)
            os.makedirs(src)
            try:
                for suf in ("", "-wal", "-shm"):
                    shutil.copyfile(os.path.join(live, "phone.db" + suf), os.path.join(src, "phone.db" + suf))
            finally:
                w.close()
            shm = os.path.join(src, "phone.db-shm")
            real = shutil.copy2

            def copy2(s, d, *a, **k):
                r = real(s, d, *a, **k)
                if s.endswith("-shm"):                 # the app touches its index right after we copied it
                    data = bytearray(read_bytes(shm))
                    data[0] ^= 0xFF
                    with open(shm, "wb") as fh:
                        fh.write(data)
                return r
            shutil.copy2 = copy2
            try:
                ev = R.Evidence(src)                   # must not raise "does not match its source hash"
            finally:
                shutil.copy2 = real
            try:
                row = [a for a in ev.acq if a["file"] == "phone.db"][0]
                self.assertEqual(row["source_stable"], "yes")
                self.assertFalse(row.get("shm_sha256"))                      # no source hash claimed for -shm
                self.assertEqual(row["copy_shm_sha256"], P.sha256(os.path.join(ev.work, "phone.db-shm")))
                checked = {v["file"]: v["copy_matches"] for v in ev.verify}
                self.assertEqual(checked, {"phone.db": "yes", "phone.db-wal": "yes"})   # .db and -wal still verified
                self.assertEqual(row["wal_sha256"], P.sha256(os.path.join(src, "phone.db-wal")))
            finally:
                ev.close()


class InputShapes(unittest.TestCase):
    """find_device_dirs accepts all four documented --input shapes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pltest-shapes-")
        self.guid = pldemo.PAIRING_GUIDS[0]
        self.pkg = os.path.join(self.tmp, "Microsoft.YourPhone_8wekyb3d8bbwe")
        self.indexed = os.path.join(self.pkg, "LocalCache", "Indexed")
        self.device = os.path.join(self.indexed, self.guid)
        self.dbdir = os.path.join(self.device, "System", "Database")
        os.makedirs(self.dbdir)
        sqlite3.connect(os.path.join(self.dbdir, "phone.db")).close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def found(self, path):
        return [(label, os.path.normcase(d)) for label, d in P.find_device_dirs(path)]

    def test_every_documented_shape_finds_the_same_device(self):
        want = [(self.guid, os.path.normcase(self.dbdir))]
        for shape in (self.pkg, self.indexed, self.device):
            self.assertEqual(self.found(shape), want, shape)
        self.assertEqual(self.found(self.dbdir), [("Database", os.path.normcase(self.dbdir))])


class OddFolderNames(unittest.TestCase):
    def test_hash_and_percent_in_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "case#1 %41")
            os.makedirs(folder)
            path = os.path.join(folder, "x.db")
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE t (a TEXT)")
            c.execute("INSERT INTO t VALUES ('right row')")
            c.commit()
            c.close()
            before = sorted(os.listdir(folder))
            u = P.ro_uri(path)
            self.assertTrue(u.endswith("?mode=ro"))
            ro = sqlite3.connect(u, uri=True)
            try:
                self.assertEqual(ro.execute("SELECT a FROM t").fetchall(), [("right row",)])
                with self.assertRaises(sqlite3.OperationalError):
                    ro.execute("INSERT INTO t VALUES ('x')")
            finally:
                ro.close()
            self.assertEqual(sorted(os.listdir(folder)), before)

    def test_carve_reads_schema_from_odd_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "case#1 %41")
            os.makedirs(folder)
            path = os.path.join(folder, "x.db")
            make_deleted_notes(path, [("tag", "hello deleted note")] * 40)
            recs, st = plcarve.carve_db(path)
            self.assertTrue(recs, st)
            self.assertEqual({r["table"] for r in recs}, {"notes"})


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def make_deleted_notes(path, rows):
    c = sqlite3.connect(path)
    c.execute("PRAGMA secure_delete = OFF")  # keep deleted bytes: some SQLite builds (e.g. Ubuntu's) default to secure_delete ON
    c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, tag TEXT, body TEXT)")
    c.executemany("INSERT INTO notes (tag, body) VALUES (?, ?)", rows)
    c.commit()
    c.execute("DELETE FROM notes")      # a full delete frees the pages whole, bytes intact
    c.commit()
    c.close()


class Overflow(unittest.TestCase):
    def test_local_payload_size(self):
        # U=4096: X=4061, M=489. A 6,000-byte payload keeps K=489+((6000-489)%4092)=1928 locally.
        self.assertEqual(plcarve.local_payload(4096, 100), 100)
        self.assertEqual(plcarve.local_payload(4096, 4061), 4061)
        self.assertEqual(plcarve.local_payload(4096, 6000), 489 + ((6000 - 489) % 4092))
        self.assertEqual(plcarve.local_payload(4096, 4092 + 489 + 4061 + 10), 489)   # K > X falls back to M

    def test_long_text_is_cut_at_the_local_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "n.db")
            rows = [("long", "A" * 6000), ("other", "B" * 6000)] + [("s", "short %d" % i) for i in range(30)]
            make_deleted_notes(path, rows)
            recs, _ = plcarve.carve_db(path)
            longs = [r for r in recs if r.get("tag") == "long"]
            self.assertTrue(longs, "the overflowed record should still be recovered, truncated")
            for r in longs:
                self.assertEqual(r["overflow_lost"], "yes")
                self.assertEqual(len(r["body"]), 1908)       # local payload 1917 - 9 bytes of record header
                self.assertLessEqual(set(r["body"]), {"A", chr(0)})   # no bytes from another page
            self.assertIn("A" * 1908, [r["body"] for r in longs])
            for r in recs:
                if r.get("tag") == "s":
                    self.assertNotIn("overflow_lost", r)


class UnreadableLiveRows(unittest.TestCase):
    def test_status_is_unknown_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "n.db")
            make_deleted_notes(path, [("t", "gone %d" % i) for i in range(20)])
            recs, st = plcarve.carve_db(path)
            self.assertTrue(recs)
            self.assertEqual({r["status"] for r in recs}, {"deleted_or_overwritten"})   # unchanged when readable
            real_live = plcarve.live_rowids
            plcarve.live_rowids = lambda db, table, errors=None: None     # as when the live table cannot be read
            try:
                recs, st = plcarve.carve_db(path)
            finally:
                plcarve.live_rowids = real_live
            self.assertTrue(recs)
            self.assertEqual({r["status"] for r in recs}, {plcarve.UNKNOWN_LIVE})
            self.assertEqual(plcarve.UNKNOWN_LIVE, "unknown - live rows unreadable")

    def test_live_rowids_returns_none_when_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "n.db")
            make_deleted_notes(path, [("t", "x")])
            errs = []
            self.assertIsNone(plcarve.live_rowids(path, "no_such_table", errs))
            self.assertTrue(errs)
            self.assertEqual(plcarve.live_rowids(path, "notes"), set())


class OverflowOtherPageSizes(unittest.TestCase):
    """local_payload with a 1,024-byte page and with reserved bytes at the end of each page (header byte 20)."""
    TEXT = 3000

    def make(self, folder):
        path = os.path.join(folder, "n.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA secure_delete = OFF")  # keep deleted bytes: some SQLite builds (e.g. Ubuntu's) default to secure_delete ON
        c.execute("PRAGMA page_size=1024")
        c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, tag TEXT, body TEXT)")
        c.executemany("INSERT INTO notes (tag, body) VALUES (?, ?)",
                      [("long", "A" * self.TEXT), ("lone", "B" * self.TEXT)] + [("s", "short %d" % i) for i in range(40)])
        c.commit()
        c.execute("DELETE FROM notes")
        c.commit()
        c.close()
        return path

    def expected_body_len(self, usable):
        hsz = 1 + 1 + 1 + 2                 # header size byte, NULL id, 4-char tag, 3,000-char body (2-byte serial)
        psz = hsz + 4 + self.TEXT
        return plcarve.local_payload(usable, psz) - hsz - 4

    def test_local_payload_values_on_1024_pages(self):
        self.assertEqual(plcarve.local_payload(1024, 989), 989)                # exactly X = U - 35
        self.assertEqual(plcarve.local_payload(1024, 990), 103)                # K = 990 > X, so M = 103
        self.assertEqual(plcarve.local_payload(1024, 2000), 103 + ((2000 - 103) % 1020))   # K = 980 <= X
        self.assertEqual(plcarve.local_payload(1008, 3000), 101)               # reserved 16: M = 101, K = 992 > X = 973
        self.assertNotEqual(plcarve.local_payload(1008, 3000), plcarve.local_payload(1024, 3000))

    def test_carve_cuts_at_the_local_payload_of_a_1024_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            recs, st = plcarve.carve_db(self.make(tmp))
            self.assertEqual(st["page_size"], 1024)
            longs = [r for r in recs if r.get("tag") in ("long", "lone")]
            self.assertTrue(longs)
            for r in longs:
                self.assertEqual(r["overflow_lost"], "yes")
                self.assertEqual(len(r["body"]), self.expected_body_len(1024))
            for r in recs:
                if r.get("tag") == "s":
                    self.assertNotIn("overflow_lost", r)

    def test_carve_honours_reserved_bytes_in_the_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make(tmp)
            shapes = plcarve.table_shapes(path)
            data = bytearray(read_bytes(path))
            data[20] = 16                       # 16 reserved bytes: usable = 1008
            with open(path, "wb") as fh:
                fh.write(data)
            real = plcarve.table_shapes
            plcarve.table_shapes = lambda *a, **k: shapes      # SQLite itself would call the patched page 1 corrupt
            try:
                recs, st = plcarve.carve_db(path)
            finally:
                plcarve.table_shapes = real
            longs = [r for r in recs if r.get("tag") in ("long", "lone")]
            self.assertTrue(longs, st)
            self.assertNotEqual(self.expected_body_len(1008), self.expected_body_len(1024))
            for r in longs:
                self.assertEqual(len(r["body"]), self.expected_body_len(1008))


class CarveNeverCrashes(unittest.TestCase):
    def test_corrupt_schema_name_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pldemo.build(os.path.join(tmp, "device"))
            path = os.path.join(src, "notifications.db")
            data = bytearray(read_bytes(path))
            i = data.find(b"tablenotification_toastmappings") + 5     # the sqlite_master name column
            self.assertGreater(i, 5)
            data[i + 3] = 0xFF                                         # invalid UTF-8
            with open(path, "wb") as fh:
                fh.write(data)
            recs, st = plcarve.carve_db(path)
            self.assertIsInstance(recs, list)
            self.assertTrue(st.get("errors"), st)

    def test_connections_are_closed(self):
        # an unreleased handle would keep this file locked on Windows
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "n.db")
            make_deleted_notes(path, [("t", "x text")] * 5)
            data = bytearray(read_bytes(path))
            i = data.find(b"tablenotes") + 5
            data[i + 2] = 0xFF
            with open(path, "wb") as fh:
                fh.write(data)
            plcarve.carve_db(path)
            os.remove(path)


FILETIME_UNIX_S = 11644473600          # seconds from 1601-01-01 to 1970-01-01


def filetime_from_ms(unix_ms):
    """The same instant as Windows FILETIME (100 ns ticks since 1601), as the notifications.post_time column holds it."""
    return (unix_ms + FILETIME_UNIX_S * 1000) * 10_000


def varint_len(v):
    n = 1
    while v >= 0x80:
        v >>= 7
        n += 1
    return n


JPKG = "com.example.chat"


def json_note(i, minute):
    """(notification_id, json text, postTime ms) of one synthetic notification with its own key."""
    post_ms = pldemo.ms(pldemo.at(minute)) + 456          # postTime is Unix MILLISECONDS
    js = pldemo.njson(JPKG, "0|%s|%d|null|10001" % (JPKG, 300 + i), minute, id=300 + i, postTime=post_ms,
                      timestamp=post_ms, title="json only %d" % i, text="recovered text %d" % i)
    return "j-%d" % i, js, post_ms


def make_header_lost_notifications(path, live=(0, 1), dead=(2, 3, 4, 5)):
    """notifications.db: every row on one page; the dead rows are deleted (secure_delete OFF), then the start of each
    freed cell - its record header and the leading columns - is overwritten in the file, so a record carver cannot
    recognise them. The json text of each dead row is left intact. Returns {i: (nid, json, postTime ms)}."""
    notes = {i: json_note(i, 10 + i) for i in tuple(live) + tuple(dead)}
    c = sqlite3.connect(path)
    c.execute("PRAGMA secure_delete = OFF")  # keep deleted bytes: some SQLite builds (e.g. Ubuntu's) default to secure_delete ON
    for s in pldemo.DDL["notifications.db"]:
        c.execute(s)
    for i in sorted(notes):
        nid, js, post_ms = notes[i]
        c.execute("INSERT INTO notifications (id, notification_id, package_name, json, post_time) VALUES (?, ?, ?, ?, ?)",
                  (i + 1, nid, JPKG, js, filetime_from_ms(post_ms)))
    c.commit()
    c.executemany("DELETE FROM notifications WHERE notification_id = ?", [(notes[i][0],) for i in dead])
    c.commit()
    c.close()
    data = bytearray(read_bytes(path))
    for i in dead:
        nid, js, post_ms = notes[i]
        body = (nid + JPKG + js).encode()
        off = data.find(body)
        assert off > 0 and data.find(body, off + 1) < 0, "fixture: the freed cell must be on disk exactly once"
        # record header: NULL id, 3 texts, 8-byte FILETIME, state 0 (serial 8), NULL anonymous_id
        types = [0, 13 + 2 * len(nid), 13 + 2 * len(JPKG), 13 + 2 * len(js.encode()), 6, 8, 0]
        hsz = 1 + sum(varint_len(t) for t in types)
        payload = hsz + len(body) + 8
        start = off - hsz - varint_len(i + 1) - varint_len(payload)
        # keep the first 4 bytes (SQLite may have written a freeblock header there), wipe the rest up to the json
        data[start + 4: off + len(nid) + len(JPKG)] = bytes(off + len(nid) + len(JPKG) - start - 4)
    with open(path, "wb") as fh:
        fh.write(data)
    return notes


def iso_ms(unix_ms):
    return R.iso(unix_ms / 1000)


class JsonCarve(unittest.TestCase):
    """Notification json that survives in dead space after its SQLite record header was overwritten."""

    def test_record_carver_alone_misses_them_and_the_json_pass_finds_each_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notifications.db")
            notes = make_header_lost_notifications(path)
            data = read_bytes(path)
            recs, st = plcarve.carve_db(path)
            notif = [r for r in recs if r["table"] == "notifications" and r["status"] != "also_live"]
            self.assertTrue(notif, st)
            # no deleted row came from the record carver: the headers are gone
            self.assertEqual({r["status"] for r in notif}, {plcarve.JSON_STATUS})
            self.assertEqual(plcarve.JSON_STATUS, "json-carved (record header lost)")
            keys = sorted(json.loads(r["json"])["key"] for r in notif)
            self.assertEqual(keys, sorted("0|%s|%d|null|10001" % (JPKG, 300 + i) for i in (2, 3, 4, 5)))
            for r in notif:
                o = json.loads(r["json"])
                i = o["id"] - 300
                self.assertEqual(r["json"], notes[i][1])                         # the whole object, nothing more
                self.assertEqual(data[r["offset"]:r["offset"] + len(r["json"])], r["json"].encode())
                self.assertEqual(r["source_file"], "notifications.db")
                self.assertTrue(r["region"].startswith("json-"), r["region"])
                self.assertIsNone(r["rowid"])
                self.assertIsNone(r["post_time"])                                # the column did not survive
                self.assertEqual(r["json_post_time_utc"], iso_ms(notes[i][2]))
                self.assertIn("Unix ms", r["time_basis"])
                self.assertIn("record header", r["confidence"])
            self.assertEqual(st["json_carved"], 4)

    def test_views_show_each_once_with_the_right_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pldemo.build(os.path.join(tmp, "device"))
            os.remove(os.path.join(src, "notifications.db"))
            notes = make_header_lost_notifications(os.path.join(src, "notifications.db"))
            ev = R.Evidence(src)
            try:
                g = R.build(ev)
            finally:
                ev.close()
            rows = g.views["N_notifications"].rows
            self.assertIn("TimeBasis", g.views["N_notifications"].cols)
            got = sorted((r["Source"], r["Key"]) for r in rows)
            self.assertEqual(got, sorted([("live", "0|%s|%d|null|10001" % (JPKG, 300 + i)) for i in (0, 1)]
                                         + [("deleted", "0|%s|%d|null|10001" % (JPKG, 300 + i)) for i in (2, 3, 4, 5)]))
            for r in rows:
                i = int(r["AndroidID"]) - 300
                want = iso_ms(notes[i][2])
                self.assertEqual(r["JSONPostTimeUTC"], want)
                if r["Source"] == "live":
                    self.assertEqual(r["PostTimeUTC"], want)                    # FILETIME column decoded as FILETIME
                    self.assertIn("FILETIME", r["TimeBasis"])
                else:
                    self.assertEqual(r["PostTimeUTC"], "")
                    self.assertIn("json postTime", r["TimeBasis"])
                    self.assertIn("Unix ms", r["TimeBasis"])
                    self.assertIn(plcarve.JSON_STATUS, r["CarvedFrom"])
                    self.assertEqual(r["Package"], JPKG)
                    self.assertEqual(r["JSONStatus"], "parsed")
                    self.assertEqual(g.time[r["_e"]], notes[i][2] / 1000)        # on the timeline at postTime
            timeline = [x for x in g.views["R22"].rows if x["Kind"] == "notif"]
            self.assertEqual(len(timeline), 6)
            nr4 = [x for x in g.views["NR4"].rows if "json only" in x["Row"] and "deleted" in x["Row"]]
            self.assertFalse([x for x in nr4 if x["Result"] == "DISAGREES"], nr4)

    def test_wal_copies_are_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            live_dir, copy_dir = os.path.join(tmp, "live"), os.path.join(tmp, "copy")
            os.makedirs(live_dir)
            os.makedirs(copy_dir)
            path = os.path.join(live_dir, "notifications.db")
            w = sqlite3.connect(path)
            try:
                w.execute("PRAGMA secure_delete = OFF")  # keep deleted bytes: some SQLite builds default to secure_delete ON
                w.execute("PRAGMA journal_mode=WAL")
                w.execute("PRAGMA wal_autocheckpoint=0")
                for s in pldemo.DDL["notifications.db"]:
                    w.execute(s)
                w.commit()
                notes = {i: json_note(i, 10 + i) for i in range(6)}
                for i in range(6):                     # one commit per row: the same page lands in the WAL many times
                    nid, js, post_ms = notes[i]
                    w.execute("INSERT INTO notifications (notification_id, package_name, json, post_time)"
                              " VALUES (?, ?, ?, ?)", (nid, JPKG, js, filetime_from_ms(post_ms)))
                    w.commit()
                w.execute("DELETE FROM notifications WHERE notification_id IN ('j-2', 'j-3')")
                w.commit()
                for suf in ("", "-wal", "-shm"):
                    shutil.copyfile(path + suf, os.path.join(copy_dir, "notifications.db" + suf))
            finally:
                w.close()
            recs, st = plcarve.carve_db(os.path.join(copy_dir, "notifications.db"))
            notif = [r for r in recs if r["table"] == "notifications" and r["status"] != "also_live"]
            keys = []
            for r in notif:
                try:
                    keys.append(json.loads(r["json"])["key"])
                except (ValueError, KeyError, TypeError):
                    pass
            self.assertEqual(sorted(keys), ["0|%s|302|null|10001" % JPKG, "0|%s|303|null|10001" % JPKG], notif)


class JsonPassOnlyDeadSpace(unittest.TestCase):
    """A notification json inside a valid cell belongs to the record path, with its real rowid and columns."""

    def edited_notification(self, tmp, checkpoint_first):
        src, dst = os.path.join(tmp, "src.db"), os.path.join(tmp, "notifications.db")
        w = wal_writer(src)
        try:
            for s in pldemo.DDL["notifications.db"]:
                w.execute(s)
            nid, js, post_ms = json_note(1, 10)
            w.execute("INSERT INTO notifications (notification_id, package_name, json, post_time) VALUES (?, ?, ?, ?)",
                      (nid, JPKG, js, filetime_from_ms(post_ms)))
            if checkpoint_first:
                w.execute("PRAGMA wal_checkpoint(TRUNCATE)")      # the main file holds the first version
            _, js2, post2 = json_note(1, 11)                      # the app updates it in place: same rowid and key
            w.execute("UPDATE notifications SET json = ?, post_time = ? WHERE id = 1", (js2, filetime_from_ms(post2)))
            copy_db_set(src, dst)
        finally:
            w.close()
        return dst, js, filetime_from_ms(post_ms)

    def check(self, recs, js, post_time, source_file, region):
        old = [r for r in recs if r["table"] == "notifications" and r.get("json") == js]
        self.assertEqual(len(old), 1, recs)
        self.assertNotEqual(old[0]["status"], plcarve.JSON_STATUS)
        self.assertEqual(old[0]["status"], plcarve.WAL_EARLIER_VERSION)
        self.assertEqual((old[0]["rowid"], old[0]["post_time"], old[0]["notification_id"]), (1, post_time, "j-1"))
        self.assertEqual((old[0]["source_file"], old[0]["region"]), (source_file, region))
        self.assertFalse([r for r in recs if r["status"] == plcarve.JSON_STATUS], recs)

    def test_older_wal_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst, js, pt = self.edited_notification(tmp, checkpoint_first=False)
            recs, st = plcarve.carve_db(dst)
            self.check(recs, js, pt, "notifications.db-wal", plcarve.SUPERSEDED_REGION)
            self.assertEqual(st["json_carved"], 0)

    def test_main_file_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst, js, pt = self.edited_notification(tmp, checkpoint_first=True)
            recs, st = plcarve.carve_db(dst)
            self.check(recs, js, pt, "notifications.db", plcarve.MAIN_SUPERSEDED_REGION)

    def test_json_inside_a_cell_the_record_path_cannot_decode_is_not_called_header_lost(self):
        # json_in_cell: an object inside a valid cell is never reported by the json pass
        with tempfile.TemporaryDirectory() as tmp:
            dst, js, pt = self.edited_notification(tmp, checkpoint_first=False)
            real = plcarve.carve_region
            plcarve.carve_region = lambda *a, **k: []              # as if no record could be decoded at all
            try:
                recs, st = plcarve.carve_db(dst)
            finally:
                plcarve.carve_region = real
            self.assertFalse([r for r in recs if r.get("json") == js], recs)
            self.assertGreaterEqual(st["json_in_cells"], 1)


def make_truncated_in_trunk(path):
    """A database with a long freelist, cut 16 bytes into its first freelist trunk page. Returns the trunk page."""
    c = sqlite3.connect(path)
    c.execute("PRAGMA secure_delete = OFF")  # keep deleted bytes: some SQLite builds (e.g. Ubuntu's) default to secure_delete ON
    c.execute("PRAGMA page_size = 1024")
    c.execute("CREATE TABLE s (k TEXT, v TEXT)")
    c.executemany("INSERT INTO s VALUES (?, ?)", [("k%d" % i, "example value %d " % i * 10) for i in range(200)])
    c.commit()
    c.execute("DELETE FROM s")
    c.commit()
    c.close()
    data = read_bytes(path)
    ps = plcarve.db_page_size(data)
    trunk = int.from_bytes(data[32:36], "big")
    leaves = int.from_bytes(data[(trunk - 1) * ps + 4:(trunk - 1) * ps + 8], "big")
    assert trunk and leaves >= 3, "fixture: the trunk page must list several leaf pages"
    with open(path, "wb") as fh:
        fh.write(data[:(trunk - 1) * ps + 16])
    return trunk


class TruncatedDatabase(unittest.TestCase):
    def test_carving_a_file_cut_inside_a_freelist_trunk_page_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.db")
            trunk = make_truncated_in_trunk(path)
            data = read_bytes(path)
            fl = plcarve.free_pages(data, plcarve.db_page_size(data))
            self.assertIn(trunk, fl)
            self.assertLessEqual(len(fl), 3)            # only the leaf entries that are really in the file: 2
            recs, st = plcarve.carve_db(path)
            self.assertIsInstance(recs, list)

    def test_one_bad_file_does_not_stop_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev = os.path.join(tmp, "dev")
            make_device(dev)                                       # a good settings.db
            make_truncated_in_trunk(os.path.join(dev, "calling.db"))
            out = os.path.join(tmp, "out")
            rc, _ = run_main(["--input", dev, "--out", out, "--carve"])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(os.path.join(out, "dev", "report.html")))

    def test_any_carve_error_becomes_a_warning_naming_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev = os.path.join(tmp, "dev")
            make_device(dev)
            make_deleted_notes(os.path.join(dev, "calling.db"), [("t", "kept note %d" % i) for i in range(30)])
            real = plcarve.carve_db

            def carve(path, *a, **k):
                if os.path.basename(path) == "settings.db":
                    raise RuntimeError("unexpected decoder failure")
                return real(path, *a, **k)
            plcarve.carve_db = carve
            try:
                rc, _ = run_main(["--input", dev, "--out", os.path.join(tmp, "out"), "--carve"])
            finally:
                plcarve.carve_db = real
            self.assertEqual(rc, 0)
            acq = read_acq(os.path.join(tmp, "out"), "dev")
            w = [x for x in acq["warnings"] if "settings.db" in x and "unexpected decoder failure" in x]
            self.assertTrue(w, acq["warnings"])
            self.assertIn("RuntimeError", w[0])
            self.assertGreater(acq["counts"]["carved candidates"], 0)     # the other file was still carved


def demo_dbs(folder, edits=None):
    """pldemo databases {name: path}, with optional SQL edits {db: [sql]}."""
    src = pldemo.build(folder)
    for db, stmts in (edits or {}).items():
        c = sqlite3.connect(os.path.join(src, db))
        for s in stmts:
            c.execute(s)
        c.commit()
        c.close()
    return {n: os.path.join(src, n) for n in P.DB_NAMES if os.path.isfile(os.path.join(src, n))}


class ParseAll(unittest.TestCase):
    def parse(self, edits=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        del P.QUERY_ERRORS[:]
        d = P.parse_all(demo_dbs(os.path.join(tmp, "dev"), edits), None, {}, 10)
        return d, list(P.QUERY_ERRORS)

    def test_aged_out_is_judged_when_messages_were_read(self):
        d, errs = self.parse()
        self.assertFalse([e for e in errs if "message" in e["sql"]])
        t = {r["thread_id"]: r for r in d["threads"]}
        self.assertEqual(t[2]["content_aged_out"], "YES")          # demo thread 2 has no message rows
        self.assertEqual(t[1]["content_aged_out"], "")

    def test_aged_out_is_blank_when_the_message_read_failed(self):
        d, errs = self.parse({"phone.db": ["DROP TABLE message"]})
        self.assertTrue([e for e in errs if "message" in e["sql"]])
        self.assertEqual(len(d["threads"]), 3)
        for t in d["threads"]:
            self.assertEqual(t["content_aged_out"], "", t)        # unknown, not "aged out"
            self.assertEqual(t["bodies_retained"], "", t)


def drop_column(path, table, col):
    """Remove one column from a database: ALTER, or rebuild the table without constraints."""
    c = sqlite3.connect(path)
    try:
        c.execute('ALTER TABLE "%s" DROP COLUMN "%s"' % (table, col))
    except sqlite3.OperationalError:     # unique, indexed or key columns cannot be altered away
        keep = [r for r in c.execute('PRAGMA table_info("%s")' % table) if r[1] != col]
        names = ", ".join('"%s"' % r[1] for r in keep)
        c.execute('CREATE TABLE "_new" (%s)' % ", ".join('"%s" %s' % (r[1], r[2]) for r in keep))
        c.execute('INSERT INTO "_new" (%s) SELECT %s FROM "%s"' % (names, names, table))
        c.execute('DROP TABLE "%s"' % table)
        c.execute('ALTER TABLE "_new" RENAME TO "%s"' % table)
    c.commit()
    c.close()


class ParseSchemaTolerance(unittest.TestCase):
    """A Phone Link version without one column must blank that field, say so once, and parse everything else."""

    def test_dropping_any_single_column_still_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            pristine = pldemo.build(os.path.join(tmp, "device"))
            cases = []
            for db in sorted(os.listdir(pristine)):
                c = sqlite3.connect(os.path.join(pristine, db))
                for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                    cases += [(db, table, r[1]) for r in c.execute('PRAGMA table_info("%s")' % table)]
                c.close()
            self.assertGreater(len(cases), 150)
            failures = []
            for n, (db, table, col) in enumerate(cases):
                copy = os.path.join(tmp, "c%d" % n)
                shutil.copytree(pristine, copy)
                drop_column(os.path.join(copy, db), table, col)
                del P.QUERY_ERRORS[:]
                del P.MISSING_COLUMNS[:]
                try:
                    P.parse_all({x: os.path.join(copy, x) for x in os.listdir(copy)}, None, {}, 10)
                except Exception as e:  # noqa: BLE001 - the point is that nothing may be raised
                    failures.append("%s.%s.%s: %s: %s" % (db, table, col, type(e).__name__, e))
                shutil.rmtree(copy, ignore_errors=True)
            self.assertEqual(failures, [])

    def test_missing_column_is_blank_and_warned_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev = os.path.join(tmp, "dev")
            pldemo.build(dev)
            drop_column(os.path.join(dev, "calling.db"), "call_history", "call_type")
            drop_column(os.path.join(dev, "calling.db"), "call_history", "start_time")   # also the ORDER BY column
            out = os.path.join(tmp, "out")
            rc, _ = run_main(["--input", dev, "--out", out])
            self.assertEqual(rc, 0)
            calls = read_csv_rows(os.path.join(out, "dev", "calls.csv"))
            self.assertEqual(len(calls), 5)                       # every call is still there
            self.assertEqual({r["start_utc"] for r in calls}, {""})
            w = [x for x in read_acq(out, "dev")["warnings"] if "not in this copy" in x]
            self.assertEqual(len(w), 1, w)
            self.assertIn("call_history.call_type", w[0])
            self.assertIn("call_history.start_time", w[0])
            self.assertIn("calling.db", w[0])


class WalSuperseded(unittest.TestCase):
    def test_rows_in_an_older_frame_of_a_page_are_decoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            live_dir, copy_dir = os.path.join(tmp, "live"), os.path.join(tmp, "copy")
            os.makedirs(copy_dir)
            w = wal_source(live_dir)                       # 5 rows, committed, all only in the -wal
            try:
                w.execute("PRAGMA secure_delete = OFF")  # keep deleted bytes: some SQLite builds default to secure_delete ON
                w.execute("DELETE FROM t WHERE id IN (2, 4)")
                w.commit()                                 # a later frame of the same page, without rows 2 and 4
                for suf in ("", "-wal", "-shm"):
                    shutil.copyfile(os.path.join(live_dir, "phone.db" + suf), os.path.join(copy_dir, "phone.db" + suf))
            finally:
                w.close()
            recs, st = plcarve.carve_db(os.path.join(copy_dir, "phone.db"))
            gone = [r for r in recs if r["table"] == "t" and r["status"] != "also_live"]
            self.assertEqual(sorted(r["v"] for r in gone), ["row1", "row3"])        # ids 2 and 4, once each
            for r in gone:
                self.assertEqual(r["status"], plcarve.WAL_SUPERSEDED)
                self.assertEqual(r["region"], "wal-superseded-cell")
                self.assertEqual(r["source_file"], "phone.db-wal")
            self.assertEqual(plcarve.WAL_SUPERSEDED, "wal-superseded")


MSG_DDL = "CREATE TABLE message (message_id INTEGER PRIMARY KEY, thread_id INTEGER, body TEXT, status INTEGER)"


def copy_db_set(src, dst):
    """Copy a .db with its -wal and -shm (while the writer is still open, so the WAL is not checkpointed)."""
    for suf in ("", "-wal", "-shm"):
        if os.path.isfile(src + suf):
            shutil.copyfile(src + suf, dst + suf)


def wal_writer(path):
    w = sqlite3.connect(path, isolation_level=None)
    w.execute("PRAGMA secure_delete = OFF")  # keep deleted bytes: some SQLite builds default to secure_delete ON
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA wal_autocheckpoint=0")
    return w


class EarlierVersions(unittest.TestCase):
    """An older copy of a row that is still live is reported when its values differ, dropped when identical."""

    def test_older_wal_frame_keeps_an_edited_rows_earlier_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "src.db"), os.path.join(tmp, "phone.db")
            w = wal_writer(src)
            try:
                w.execute(MSG_DDL)
                w.execute("INSERT INTO message VALUES (1, 5, 'ORIGINAL BODY: meet at the old place', 1)")
                w.execute("INSERT INTO message VALUES (2, 5, 'unchanged row', 1)")
                w.execute("UPDATE message SET body = 'EDITED BODY: meet at the new place', status = 2"
                          " WHERE message_id = 1")       # same rowid, a later frame of the same page
                copy_db_set(src, dst)
            finally:
                w.close()
            recs, st = plcarve.carve_db(dst)
            msg = [r for r in recs if r["table"] == "message"]
            old = [r for r in msg if "ORIGINAL BODY" in str(r.get("body"))]
            self.assertEqual(len(old), 1, msg)
            self.assertEqual(old[0]["rowid"], 1)
            self.assertEqual(old[0]["status"], plcarve.WAL_EARLIER_VERSION)
            self.assertEqual(plcarve.WAL_EARLIER_VERSION, "wal-superseded (earlier version of live row)")
            self.assertEqual(old[0]["col_status"], 1)          # the row's own status column, before the edit
            self.assertEqual(old[0]["region"], plcarve.SUPERSEDED_REGION)
            self.assertEqual(old[0]["source_file"], "phone.db-wal")
            self.assertEqual(len(msg), 1, "identical copies of live rows (row 2, and row 1 as it is now) are dropped")
            self.assertEqual((st["deleted_only"], st["earlier_versions"]), (0, 1))

    def test_main_file_image_of_a_page_the_wal_supersedes_is_decoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "src.db"), os.path.join(tmp, "phone.db")
            w = wal_writer(src)
            try:
                w.execute(MSG_DDL)
                w.execute("INSERT INTO message VALUES (1, 5, 'KEEP ME', 1)")
                w.execute("INSERT INTO message VALUES (2, 5, 'DELETED SECRET aaaaaaaaaaaaaaaaaaaa', 1)")
                w.execute("INSERT INTO message VALUES (4, 5, 'EDIT ME before', 1)")
                w.execute("PRAGMA wal_checkpoint(TRUNCATE)")      # the main file now holds rows 2 and 4 as live cells
                w.execute("DELETE FROM message WHERE message_id = 2")
                w.execute("UPDATE message SET body = 'EDIT ME after', status = 3 WHERE message_id = 4")
                w.execute("INSERT INTO message VALUES (3, 5, 'replacement text bbbbbbbbbbbbbbbbbbbbbbb', 1)")
                copy_db_set(src, dst)
            finally:
                w.close()
            self.assertIn(b"DELETED SECRET", read_bytes(dst))
            recs, _ = plcarve.carve_db(dst)
            msg = {r["rowid"]: r for r in recs if r["table"] == "message"}
            self.assertEqual(sorted(msg), [2, 4], recs)          # 1 and 3 are live and unchanged
            self.assertEqual(msg[2]["body"], "DELETED SECRET aaaaaaaaaaaaaaaaaaaa")
            self.assertEqual(msg[2]["status"], plcarve.WAL_SUPERSEDED)
            self.assertEqual((msg[2]["source_file"], msg[2]["region"]), ("phone.db", plcarve.MAIN_SUPERSEDED_REGION))
            self.assertEqual(plcarve.MAIN_SUPERSEDED_REGION, "main-superseded-cell")
            self.assertEqual((msg[4]["body"], msg[4]["col_status"]), ("EDIT ME before", 1))
            self.assertEqual(msg[4]["status"], plcarve.WAL_EARLIER_VERSION)

    def test_carver_fields_are_not_overwritten_by_same_named_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "contacts.db")
            c = sqlite3.connect(path)
            c.execute("PRAGMA secure_delete = OFF")  # keep deleted bytes: some SQLite builds default to secure_delete ON
            c.execute("CREATE TABLE postaladdress (id INTEGER PRIMARY KEY, street TEXT, region TEXT, status TEXT)")
            c.executemany("INSERT INTO postaladdress VALUES (?, ?, ?, ?)",
                          [(i, "street %d" % i + "z" * 40, "Example Region", "home") for i in range(1, 4)])
            c.commit()
            c.execute("DELETE FROM postaladdress WHERE id = 3")      # the lowest cell: its bytes stay in page slack
            c.commit()
            c.close()
            recs, _ = plcarve.carve_db(path)
            self.assertTrue(recs)
            for r in recs:
                self.assertIn(r["region"], ("freeblock", "page-slack", "free-page"))
                self.assertIn(r["status"], ("deleted_or_overwritten", "also_live"))
                self.assertEqual((r["col_region"], r["col_status"]), ("Example Region", "home"))

    def test_freeblock_copy_of_a_live_row_with_other_values_is_an_earlier_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "phone.db")
            c = sqlite3.connect(path)
            c.execute("PRAGMA secure_delete = OFF")  # keep deleted bytes: some SQLite builds default to secure_delete ON
            c.execute(MSG_DDL)
            c.execute("INSERT INTO message VALUES (1, 5, 'ORIGINAL BODY: meet at the old place', 1)")
            c.execute("INSERT INTO message VALUES (2, 5, 'second row that is deleted xxxxxxxxxxxxxxxx', 1)")
            c.execute("INSERT INTO message VALUES (3, 5, 'third row stays', 1)")
            c.commit()
            c.execute("DELETE FROM message WHERE message_id = 2")
            # a longer body cannot reuse the freed space: the old cell stays behind in a freeblock
            c.execute("UPDATE message SET body = ?, status = 2 WHERE message_id = 1", ("EDITED BODY: " + "y" * 300,))
            c.commit()
            c.close()
            self.assertIn(b"ORIGINAL BODY", read_bytes(path))
            recs, _ = plcarve.carve_db(path)
            old = [r for r in recs if "ORIGINAL BODY" in str(r.get("body"))]
            self.assertEqual(len(old), 1, recs)
            self.assertEqual(old[0]["region"], "freeblock")
            self.assertEqual(old[0]["status"], plcarve.EARLIER_VERSION)
            self.assertEqual(old[0]["col_status"], 1)
            self.assertEqual(plcarve.EARLIER_VERSION, "earlier version of live row")


def wal_layout(wal):
    """(page size, [frame offsets]) of a -wal file's bytes."""
    ps = int.from_bytes(wal[8:12], "big")
    return ps, list(range(32, len(wal) - 24 - ps + 1, 24 + ps))


def wal_resum(wal, start_frame):
    """Recompute the cumulative checksums from frame start_frame on (fixture helper, written apart from plcarve)."""
    ps, offs = wal_layout(wal)
    big = wal[3] == 0x83
    order = "big" if big else "little"

    def ck(buf, s0, s1):
        for i in range(0, len(buf), 8):
            s0 = (s0 + int.from_bytes(buf[i:i + 4], order) + s1) & 0xFFFFFFFF
            s1 = (s1 + int.from_bytes(buf[i + 4:i + 8], order) + s0) & 0xFFFFFFFF
        return s0, s1
    prev = wal[24:32] if start_frame == 0 else wal[offs[start_frame - 1] + 16:offs[start_frame - 1] + 24]
    s0, s1 = int.from_bytes(prev[:4], "big"), int.from_bytes(prev[4:], "big")
    for o in offs[start_frame:]:
        s0, s1 = ck(bytes(wal[o:o + 8]) + bytes(wal[o + 24:o + 24 + ps]), s0, s1)
        wal[o + 16:o + 24] = s0.to_bytes(4, "big") + s1.to_bytes(4, "big")


class WalValidation(unittest.TestCase):
    """Which WAL frames count: salts, checksums and commit frames, as SQLite reads them."""

    def two_commits(self, tmp, last_sql):
        """message rows 1 and 2 committed; then last_sql committed as the last frame. Returns the copy's path."""
        src, dst = os.path.join(tmp, "src.db"), os.path.join(tmp, "phone.db")
        w = wal_writer(src)
        try:
            w.execute(MSG_DDL)
            w.execute("INSERT INTO message VALUES (1, 5, 'alpha', 1)")
            w.execute("INSERT INTO message VALUES (2, 5, 'beta', 1)")
            w.execute(last_sql)
            copy_db_set(src, dst)
        finally:
            w.close()
        os.remove(dst + "-shm")          # SQLite rebuilds its WAL index from the -wal bytes themselves
        return dst

    def test_unmodified_wal_frames_are_all_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = self.two_commits(tmp, "UPDATE message SET body = 'gamma' WHERE message_id = 2")
            frames = plcarve.wal_read(dst + "-wal", 4096)
            self.assertGreaterEqual(len(frames), 4)
            self.assertEqual({f["state"] for f in frames}, {plcarve.COMMITTED})     # checksums verify
            self.assertTrue(frames[-1]["commit"])

    def test_checksum_mismatch_ends_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = self.two_commits(tmp, "UPDATE message SET body = 'gamma' WHERE message_id = 2")
            wal = bytearray(read_bytes(dst + "-wal"))
            ps, offs = wal_layout(wal)
            wal[offs[-2] + 24 + ps // 2] ^= 0xFF          # damage the second-last frame's page
            with open(dst + "-wal", "wb") as fh:
                fh.write(wal)
            states = [f["state"] for f in plcarve.wal_read(dst + "-wal", 4096)]
            self.assertEqual(states[-2:], [plcarve.INVALID, plcarve.INVALID])
            self.assertEqual(set(states[:-2]), {plcarve.COMMITTED})

    def test_frame_after_the_last_commit_is_uncommitted_and_does_not_supersede(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = self.two_commits(tmp, "UPDATE message SET body = 'ALPHA PENDING EDIT' WHERE message_id = 1")
            wal = bytearray(read_bytes(dst + "-wal"))
            ps, offs = wal_layout(wal)
            wal[offs[-1] + 4:offs[-1] + 8] = bytes(4)     # its commit marker gone: a transaction never committed
            wal_resum(wal, len(offs) - 1)
            with open(dst + "-wal", "wb") as fh:
                fh.write(wal)
            frames = plcarve.wal_read(dst + "-wal", 4096)
            self.assertEqual(frames[-1]["state"], plcarve.UNCOMMITTED)
            self.assertEqual({f["state"] for f in frames[:-1]}, {plcarve.COMMITTED})
            self.assertEqual(plcarve.live_values(dst, "message", {1})[1][2], "alpha")   # SQLite ignores the frame
            recs, st = plcarve.carve_db(dst)
            pend = [r for r in recs if r.get("body") == "ALPHA PENDING EDIT"]
            self.assertEqual(len(pend), 1, recs)
            self.assertEqual(pend[0]["status"], plcarve.WAL_UNCOMMITTED)
            self.assertEqual(plcarve.WAL_UNCOMMITTED, "wal-uncommitted")
            self.assertEqual(pend[0]["wal_frame_state"], plcarve.UNCOMMITTED)
            self.assertEqual(pend[0]["wal_frame"], len(offs) - 1)
            self.assertTrue(pend[0]["rowid_live"].startswith("yes"))
            # the committed frame holding the live rows is the current version, not a superseded one
            self.assertFalse([r for r in recs if r.get("body") in ("alpha", "beta")
                              and r["status"] not in (plcarve.ALSO_LIVE,)], recs)

    def test_rows_of_a_transaction_still_open_are_uncommitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "src.db"), os.path.join(tmp, "phone.db")
            w = wal_writer(src)
            try:
                w.execute("PRAGMA cache_size=2")          # a tiny cache spills the open transaction into the -wal
                w.execute(MSG_DDL)
                w.execute("INSERT INTO message VALUES (1, 5, 'committed row', 1)")
                w.execute("BEGIN")
                for i in range(2, 400):
                    w.execute("INSERT INTO message VALUES (?, 5, ?, 1)", (i, "PENDING row %d %s" % (i, "x" * 60)))
                for suf in ("", "-wal"):
                    shutil.copyfile(src + suf, dst + suf)
                w.execute("ROLLBACK")
            finally:
                w.close()
            states = [f["state"] for f in plcarve.wal_read(dst + "-wal", 4096)]
            self.assertIn(plcarve.UNCOMMITTED, states)
            self.assertEqual(states, sorted(states, key=lambda s: s == plcarve.UNCOMMITTED))   # all after the commits
            recs, _ = plcarve.carve_db(dst)
            pending = [r for r in recs if "PENDING row" in str(r.get("body"))]
            self.assertTrue(pending)
            self.assertEqual({r["status"] for r in pending if r["region"] == "wal-uncommitted-cell"},
                             {plcarve.WAL_UNCOMMITTED})
            self.assertTrue([r for r in pending if r["region"] == "wal-uncommitted-cell"])
            self.assertEqual({r["wal_frame_state"] for r in pending}, {plcarve.UNCOMMITTED})
            self.assertFalse([r for r in pending if r["status"] in (plcarve.WAL_SUPERSEDED, plcarve.DELETED)])

    def test_frames_of_an_older_log_generation_are_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "src.db"), os.path.join(tmp, "phone.db")
            w = wal_writer(src)
            try:
                w.execute(MSG_DDL)
                w.execute("INSERT INTO message VALUES (1, 5, 'first row', 1)")
                w.execute("INSERT INTO message VALUES (2, 5, 'OLDER GENERATION ROW', 1)")
                # zero the deleted cell, so the row survives only as a live cell of the older-generation frame
                w.execute("PRAGMA secure_delete = ON")
                w.execute("DELETE FROM message WHERE message_id = 2")
                w.execute("PRAGMA secure_delete = OFF")
                w.execute("PRAGMA wal_checkpoint(PASSIVE)")
                w.execute("INSERT INTO message VALUES (3, 5, 'new generation', 1)")   # restarts the log: new salts
                copy_db_set(src, dst)
            finally:
                w.close()
            frames = plcarve.wal_read(dst + "-wal", 4096)
            self.assertEqual(frames[0]["state"], plcarve.COMMITTED)
            self.assertIn(plcarve.STALE, {f["state"] for f in frames[1:]})
            self.assertEqual({f["state"] for f in frames[1:]}, {plcarve.STALE})
            recs, _ = plcarve.carve_db(dst)
            old = [r for r in recs if r.get("body") == "OLDER GENERATION ROW" and r["region"] == "wal-stale-cell"]
            self.assertEqual(len(old), 1, recs)
            self.assertEqual(old[0]["status"], plcarve.WAL_STALE)
            self.assertEqual(plcarve.WAL_STALE, "wal-stale")
            self.assertEqual((old[0]["rowid_live"], old[0]["wal_frame_state"]), ("no", plcarve.STALE))
            self.assertFalse([r for r in recs if r["status"] == plcarve.WAL_SUPERSEDED], recs)


class FailedStaging(unittest.TestCase):
    def test_work_folder_removed_when_stage_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pldemo.build(os.path.join(tmp, "device"))
            work = os.path.join(tmp, "work")
            real = P.stage

            def broken(src_dir, w, acq):
                os.makedirs(w, exist_ok=True)
                shutil.copy2(os.path.join(src_dir, "phone.db"), os.path.join(w, "phone.db"))
                raise RuntimeError("disk full")
            P.stage = broken
            try:
                with self.assertRaises(RuntimeError):
                    R.Evidence(src, work=work)
            finally:
                P.stage = real
            self.assertFalse(os.path.exists(work))

    def test_default_work_folder_removed_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pldemo.build(os.path.join(tmp, "device"))
            made = []
            real_mk, real_stage = R.tempfile.mkdtemp, P.stage

            def mk(*a, **k):
                d = real_mk(*a, **k)
                made.append(d)
                return d

            def broken(src_dir, w, acq):
                shutil.copy2(os.path.join(src_dir, "phone.db"), os.path.join(w, "phone.db"))
                raise RuntimeError("boom")
            R.tempfile.mkdtemp, P.stage = mk, broken
            try:
                with self.assertRaises(RuntimeError):
                    R.Evidence(src)
            finally:
                R.tempfile.mkdtemp, P.stage = real_mk, real_stage
            self.assertEqual(len(made), 1)
            self.assertFalse(os.path.exists(made[0]))


class CopyRemoval(unittest.TestCase):
    """The staged copy is removed with retries (Windows file locks); a copy that stays behind is reported."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = pldemo.build(os.path.join(self.tmp.name, "device"))
        self.real_rmtree, self.real_sleep = R.shutil.rmtree, R.time.sleep
        self.calls = 0
        R.time.sleep = lambda s: None
        self.addCleanup(self.restore)
        self.addCleanup(lambda: R.LEFT_BEHIND.clear())

    def restore(self):
        R.shutil.rmtree, R.time.sleep = self.real_rmtree, self.real_sleep

    def locked_for(self, n):
        """rmtree that removes nothing for its first n calls, as while another process holds a file open."""
        def rmtree(path, *a, **k):
            self.calls += 1
            if self.calls > n:
                return self.real_rmtree(path, *a, **k)
        R.shutil.rmtree = rmtree

    def test_close_retries_a_locked_copy(self):
        ev = R.Evidence(self.src)
        self.locked_for(2)
        self.assertIsNone(ev.close())
        self.assertFalse(os.path.exists(ev.work))
        self.assertEqual(self.calls, 3)
        self.assertEqual(R.LEFT_BEHIND, [])

    def test_close_reports_a_copy_it_cannot_remove(self):
        ev = R.Evidence(self.src)
        self.locked_for(10 ** 6)
        left = ev.close()
        self.restore()
        self.addCleanup(shutil.rmtree, ev.work, True)
        self.assertEqual(left, ev.work)
        self.assertEqual(ev.left_behind, ev.work)
        self.assertIn(ev.work, R.LEFT_BEHIND)
        self.assertEqual(self.calls, R.RMTREE_TRIES)

    def test_failed_open_reports_the_copy_it_could_not_remove(self):
        work = os.path.join(self.tmp.name, "work")
        real_stage = P.stage

        def broken(src_dir, w, acq):
            os.makedirs(w, exist_ok=True)
            shutil.copy2(os.path.join(src_dir, "phone.db"), os.path.join(w, "phone.db"))
            raise RuntimeError("disk full")
        P.stage = broken
        self.addCleanup(setattr, P, "stage", real_stage)
        self.locked_for(10 ** 6)
        with self.assertRaises(RuntimeError) as cm:
            R.Evidence(self.src, work=work)
        self.restore()
        self.assertTrue(os.path.exists(work))
        self.assertEqual(cm.exception.left_behind, work)
        self.assertIn(work, R.LEFT_BEHIND)
        self.assertTrue(any(work in n for n in getattr(cm.exception, "__notes__", [])))

    def test_failed_open_retries_too(self):
        work = os.path.join(self.tmp.name, "work")
        real_stage = P.stage

        def broken(src_dir, w, acq):
            os.makedirs(w, exist_ok=True)
            raise RuntimeError("disk full")
        P.stage = broken
        self.addCleanup(setattr, P, "stage", real_stage)
        self.locked_for(1)
        with self.assertRaises(RuntimeError):
            R.Evidence(self.src, work=work)
        self.assertFalse(os.path.exists(work))
        self.assertEqual(R.LEFT_BEHIND, [])


GUID_A, GUID_B = pldemo.PAIRING_GUIDS[0], pldemo.PAIRING_GUIDS[1]   # synthetic pairing-folder GUIDs
DELETED_PKG = "com.deleted.from.phone.a."          # prefix of every deleted FullTrust row for device A
ORPHAN_PKG = "com.orphan.no.device."              # deleted FullTrust rows with a blank device_id


def make_device(folder):
    """A device folder holding one small database (any name in plparse.DB_NAMES makes it a device)."""
    os.makedirs(folder, exist_ok=True)
    c = sqlite3.connect(os.path.join(folder, "settings.db"))
    c.execute("CREATE TABLE s (k TEXT, v TEXT)")
    c.execute("INSERT INTO s VALUES ('x', 'y')")
    c.commit()
    c.close()


def make_fulltrust(path, live, deleted):
    """phone_apps.db with hosted_app_ids. `deleted` rows are inserted, then deleted with free space kept."""
    c = sqlite3.connect(path)
    c.execute("PRAGMA secure_delete = OFF")  # keep deleted bytes: some SQLite builds default to secure_delete ON
    c.execute("CREATE TABLE hosted_app_ids (row_id INTEGER PRIMARY KEY AUTOINCREMENT, package_id TEXT,"
              " last_window_position TEXT, device_id TEXT)")
    c.executemany("INSERT INTO hosted_app_ids (package_id, last_window_position, device_id) VALUES (?, ?, ?)",
                  deleted + live)
    c.commit()
    c.executemany("DELETE FROM hosted_app_ids WHERE package_id = ?", [(r[0],) for r in deleted])
    c.commit()
    c.close()


def make_package(root, meta=None, live=None, deleted=None):
    """Two pairing folders {A} and {B} sharing one FullTrust phone_apps.db and one DeviceMetadataStorage.json."""
    lc = os.path.join(root, "Microsoft.YourPhone_8wekyb3d8bbwe", "LocalCache")
    for g in (GUID_A, GUID_B):
        make_device(os.path.join(lc, "Indexed", g, "System", "Database"))
    ft = os.path.join(lc, "Indexed", "FullTrust")
    os.makedirs(ft)
    if deleted is None:
        deleted = ([(DELETED_PKG + "%02d" % i + "x" * 150, "0,0,800,600", GUID_A) for i in range(40)]
                   + [(ORPHAN_PKG + "%02d" % i + "y" * 150, "1,1,2,2", "") for i in range(5)])
    if live is None:
        live = [("com.live.on.a", "10,10,20,20", GUID_A), ("com.live.on.b", "30,30,40,40", GUID_B)]
    make_fulltrust(os.path.join(ft, "phone_apps.db"), live, deleted)
    if meta is None:
        meta = {"ConfigVersion": 1, "DeviceMetadatas": {
            "HW-A": [{"IsLinked": True, "Metadata": {"Id": GUID_A, "Metadata": {
                "Manufacture": "AcmeCorpA", "ModelName": "ZX-9A", "OsName": "Android", "OsVersion": "14"}}}],
            "HW-OTHER": [{"IsLinked": False, "Metadata": {"Id": "not-a-folder-id", "Metadata": {
                "Manufacture": "OtherMakerQ", "ModelName": "Q-1"}}}]}}
    with open(os.path.join(lc, "DeviceMetadataStorage.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    return os.path.join(root, "Microsoft.YourPhone_8wekyb3d8bbwe")


def run_main(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = P.main(args)
    return rc, buf.getvalue()


def read_text(path):
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def read_acq(outdir, label):
    with open(os.path.join(outdir, label, "acquisition.json"), encoding="utf-8") as fh:
        return json.load(fh)


class FullTrustScope(unittest.TestCase):
    """phone_apps.db is package-wide: its carved and vacant-rowid rows belong to a device only by device_id."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pkg = make_package(os.path.join(self.tmp, "in"))
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_other_devices_deleted_rows_are_not_in_this_device(self):
        rc, _ = run_main(["--input", self.pkg, "--out", self.out, "--carve"])
        self.assertEqual(rc, 0)
        b_dir = os.path.join(self.out, GUID_B)
        b_carved = read_text(os.path.join(b_dir, "carved_records.csv"))
        self.assertNotIn(DELETED_PKG, b_carved)
        self.assertNotIn(GUID_A, b_carved)
        self.assertNotIn(ORPHAN_PKG, b_carved)
        self.assertNotIn("phone_apps", read_text(os.path.join(b_dir, "deleted_evidence.csv")))
        acq_b = read_acq(self.out, GUID_B)
        b_rows = read_csv_rows(os.path.join(b_dir, "carved_records.csv"))
        self.assertTrue(all(r["device_id"] == GUID_B for r in b_rows if r.get("table") == "hosted_app_ids"))
        self.assertEqual(acq_b["counts"]["carved not-live"], 0)      # B's only deleted rows were A's
        self.assertEqual(acq_b["counts"]["vacant-rowid signals"], 0)
        self.assertFalse([w for w in acq_b["warnings"] if "hosted_app_ids" in w and "vacant" in w])
        # device A keeps its own deleted rows, and only its own
        a_rows = read_csv_rows(os.path.join(self.out, GUID_A, "carved_records.csv"))
        a_ft = [r for r in a_rows if r.get("table") == "hosted_app_ids"]
        self.assertTrue(a_ft, "device A's own deleted FullTrust rows should be carved into its output")
        self.assertTrue(all(r["device_id"] == GUID_A for r in a_ft))
        self.assertTrue(any(r["package_id"].startswith(DELETED_PKG) for r in a_ft))
        self.assertNotIn(ORPHAN_PKG, read_text(os.path.join(self.out, GUID_A, "carved_records.csv")))

    def test_unattributable_rows_go_to_the_package_output(self):
        run_main(["--input", self.pkg, "--out", self.out, "--carve"])
        pkg_dir = os.path.join(self.out, "_package")
        rows = read_csv_rows(os.path.join(pkg_dir, "carved_records.csv"))
        self.assertTrue(any(r.get("package_id", "").startswith(ORPHAN_PKG) for r in rows))
        self.assertTrue(all(r["scope"] == "package" for r in rows))
        self.assertFalse(any(r.get("device_id") in (GUID_A, GUID_B) for r in rows))
        self.assertIn("hosted_app_ids", read_text(os.path.join(pkg_dir, "deleted_evidence.csv")))

    def test_fulltrust_carve_error_is_reported(self):
        real = plcarve.carve_db

        def carve(path, *a, **k):
            if os.path.basename(path) == "phone_apps.db":
                raise OSError("simulated read error")
            return real(path, *a, **k)
        plcarve.carve_db = carve
        try:
            run_main(["--input", self.pkg, "--out", self.out, "--carve"])
        finally:
            plcarve.carve_db = real
        for g in (GUID_A, GUID_B):
            w = read_acq(self.out, g)["warnings"]
            self.assertTrue([x for x in w if "phone_apps.db" in x and "simulated read error" in x], w)

    def test_label_that_is_not_a_guid_gets_no_fulltrust_rows(self):
        dump = os.path.join(self.tmp, "triage")
        make_device(dump)
        make_fulltrust(os.path.join(dump, "phone_apps.db"),
                       [("com.live.on.a", "1", GUID_A), ("com.live.blank", "2", "")],
                       [(DELETED_PKG + "%02d" % i + "x" * 150, "0", GUID_A) for i in range(40)])
        out = os.path.join(self.tmp, "out2")
        run_main(["--input", dump, "--out", out, "--carve"])
        d = os.path.join(out, "triage")
        self.assertNotIn(DELETED_PKG, read_text(os.path.join(d, "carved_records.csv")))
        self.assertFalse([r for r in read_csv_rows(os.path.join(d, "apps.csv")) if r.get("streamed_to_pc")])
        w = read_acq(out, "triage")["warnings"]
        self.assertTrue([x for x in w if "not a pairing-folder GUID" in x], w)


NO_ENTRY = ("no DeviceMetadataStorage.json entry has this folder's GUID as its Id (Phone Link removes a phone's "
            "entry when it is unlinked)")
OTHERS_HEADING = "Other entries in DeviceMetadataStorage.json (their Id is not this folder's GUID)"


class MetadataScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pkg = make_package(os.path.join(self.tmp, "in"))
        self.out = os.path.join(self.tmp, "out")
        run_main(["--input", self.pkg, "--out", self.out])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_device_without_entry_says_so(self):
        md = read_acq(self.out, GUID_B)["device_metadata"]
        self.assertEqual(md["entries"], [])
        self.assertIn(NO_ENTRY, md["note"])
        self.assertNotIn("AcmeCorpA", json.dumps(md))
        others = read_acq(self.out, GUID_B)["package_metadata_other_entries"]
        self.assertEqual({e["id"] for e in others}, {GUID_A, "not-a-folder-id"})
        html_b = read_text(os.path.join(self.out, GUID_B, "report.html"))
        dev, sep, pkg = html_b.partition(OTHERS_HEADING)
        self.assertTrue(sep, "the package's other entries need their own heading")
        self.assertIn(NO_ENTRY.replace("'", "&#x27;"), dev)
        self.assertNotIn("say nothing about this device", html_b)     # only that their Id is not the folder's
        self.assertNotIn("AcmeCorpA", dev)
        self.assertIn("AcmeCorpA", pkg)

    def test_device_with_entry_gets_only_its_own(self):
        md = read_acq(self.out, GUID_A)["device_metadata"]
        self.assertEqual([e["make"] for e in md["entries"]], ["AcmeCorpA"])
        self.assertNotIn("OtherMakerQ", json.dumps(md))
        html_a = read_text(os.path.join(self.out, GUID_A, "report.html"))
        dev, _, _ = html_a.partition(OTHERS_HEADING)
        self.assertIn("AcmeCorpA", dev)
        self.assertNotIn("OtherMakerQ", dev)

    def test_package_inputs_are_hashed(self):
        lc = os.path.join(self.pkg, "LocalCache")
        ft = os.path.join(lc, "Indexed", "FullTrust", "phone_apps.db")
        js = os.path.join(lc, "DeviceMetadataStorage.json")
        for g in (GUID_A, GUID_B):
            pf = {r["file"]: r for r in read_acq(self.out, g)["package_files"]}
            self.assertEqual(pf["FullTrust/phone_apps.db"]["sha256"], P.sha256(ft))
            self.assertEqual(pf["DeviceMetadataStorage.json"]["sha256"], P.sha256(js))
        with open(os.path.join(self.out, "_package", "acquisition.json"), encoding="utf-8") as fh:
            pkg = json.load(fh)
        self.assertEqual({r["file"] for r in pkg["package_files"]},
                         {"FullTrust/phone_apps.db", "DeviceMetadataStorage.json"})


class MetadataLocation(unittest.TestCase):
    """DeviceMetadataStorage.json is used only at the LocalCache level of a Phone Link layout holding the device."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_meta(self, folder, ident="unrelated-id"):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "DeviceMetadataStorage.json"), "w", encoding="utf-8") as fh:
            json.dump({"DeviceMetadatas": {"HW-X": [{"Metadata": {"Id": ident, "Metadata": {
                "Manufacture": "UnrelatedMakerZ"}}}]}}, fh)

    def test_plain_folder_does_not_pick_up_a_json_above_it(self):
        triage = os.path.join(self.tmp, "case", "exports", "triage")
        make_device(triage)
        self.write_meta(os.path.join(self.tmp, "case", "exports"))
        self.write_meta(os.path.join(self.tmp, "case"))
        self.assertIsNone(P.find_metadata(triage))
        path, note = P.locate_metadata(triage)
        self.assertIsNone(path)
        self.assertIn("not used", note)
        out = os.path.join(self.tmp, "out")
        run_main(["--input", triage, "--out", out])
        acq = read_acq(out, "triage")
        self.assertNotIn("UnrelatedMakerZ", json.dumps(acq))
        self.assertFalse([r for r in acq["package_files"] if r["file"] == "DeviceMetadataStorage.json"])
        self.assertIn("not used", acq["device_metadata"]["note"])

    def test_every_package_input_shape_finds_it(self):
        pkg = make_package(os.path.join(self.tmp, "in"))
        lc = os.path.join(pkg, "LocalCache")
        want = os.path.join(lc, "DeviceMetadataStorage.json")
        dev = os.path.join(lc, "Indexed", GUID_A)
        for inp in (pkg, lc, os.path.join(lc, "Indexed"), os.path.join(dev, "System", "Database")):
            self.assertEqual(P.find_metadata(inp), want, inp)

    def test_json_beside_an_indexed_folder_that_lacks_the_device_is_not_used(self):
        lc = os.path.join(self.tmp, "x", "LocalCache")
        os.makedirs(os.path.join(lc, "Indexed", GUID_B))
        self.write_meta(lc)
        dev = os.path.join(lc, "elsewhere")
        make_device(dev)
        self.assertIsNone(P.find_metadata(dev))


class GuidNormaliser(unittest.TestCase):
    """One normaliser (case + braces) for every engine comparison of device ids and GUIDs."""

    def test_same_guid(self):
        g = GUID_A
        self.assertTrue(P.same_guid("{%s}" % g.upper(), g))
        self.assertTrue(P.same_guid(" %s " % g.upper(), "{%s}" % g))
        self.assertFalse(P.same_guid("", ""))
        self.assertFalse(P.same_guid("not-a-guid", "not-a-guid"))
        self.assertFalse(P.same_guid(GUID_A, GUID_B))
        self.assertEqual(P.norm_guid("{%s}" % g.upper()), g.lower())

    def test_unbalanced_braces_are_not_a_guid(self):
        self.assertTrue(P.is_guid("{%s}" % GUID_A))
        self.assertFalse(P.is_guid("{%s" % GUID_A))
        self.assertFalse(P.is_guid("%s}" % GUID_A))
        self.assertFalse(P.owns_ft_row("{%s" % GUID_A, GUID_A))

    def test_braced_upper_case_ids_match_the_folder_everywhere(self):
        braced = "{%s}" % GUID_A.upper()
        self.assertTrue(P.owns_ft_row(braced, GUID_A))
        mine, others = P.device_metadata(GUID_A, [{"id": braced}, {"id": GUID_B}], "", "x.json")
        self.assertEqual([e["id"] for e in mine["entries"]], [braced])
        self.assertEqual([e["id"] for e in others], [GUID_B])
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        pkg = make_package(os.path.join(tmp, "in"), live=[("com.braced.on.a", "1,1,2,2", braced)], deleted=[])
        out = os.path.join(tmp, "out")
        run_main(["--input", pkg, "--out", out])
        a_apps = read_csv_rows(os.path.join(out, GUID_A, "apps.csv"))
        self.assertEqual([r["package"] for r in a_apps if r["streamed_to_pc"] == "yes"], ["com.braced.on.a"])
        self.assertFalse([r for r in read_csv_rows(os.path.join(out, GUID_B, "apps.csv")) if r["streamed_to_pc"]])
        live = read_csv_rows(os.path.join(out, "_package", "fulltrust_hosted_app_ids.csv"))
        self.assertEqual([r["attributed_to_device_folder"] for r in live], [GUID_A])
        w = read_acq(out, GUID_B)["warnings"]
        self.assertTrue([x for x in w if "case and braces ignored" in x], w)


class Recoverability(unittest.TestCase):
    """secure_delete is a setting of the analysing connection, not of the file. Judge by the file's bytes."""

    def make(self, folder, secure):
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "settings.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA secure_delete = %s" % ("ON" if secure else "OFF"))
        c.execute("CREATE TABLE s (k TEXT, v TEXT)")
        c.executemany("INSERT INTO s VALUES (?, ?)", [("k%d" % i, "secret value %d " % i * 20) for i in range(300)])
        c.commit()
        c.execute("DELETE FROM s")
        c.commit()
        c.close()
        return path

    def test_secure_delete_is_not_reported_from_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make(os.path.join(tmp, "dev"), secure=True)
            h = P.db_health(path)
            self.assertGreater(h["freelist_count"], 0)
            self.assertNotIn("secure_delete", h)
            self.assertEqual(h["free_pages_with_residue"], 0)

    def test_zeroed_free_pages_are_not_called_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make(os.path.join(tmp, "dev"), secure=True)
            out = os.path.join(tmp, "out")
            run_main(["--input", os.path.join(tmp, "dev"), "--out", out])
            acq = read_acq(out, "dev")
            self.assertFalse([w for w in acq["warnings"] if "secure_delete" in w or "recoverable" in w],
                             acq["warnings"])
            self.assertNotIn("secure_delete = 0", read_text(os.path.join(out, "dev", "report.html")))

    def test_free_pages_with_bytes_left_are_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make(os.path.join(tmp, "dev"), secure=False)
            h = P.db_health(path)
            self.assertGreater(h["free_pages_with_residue"], 0)
            out = os.path.join(tmp, "out")
            run_main(["--input", os.path.join(tmp, "dev"), "--out", out])
            w = read_acq(out, "dev")["warnings"]
            self.assertTrue([x for x in w if "settings.db" in x and "non-zero" in x], w)


if __name__ == "__main__":
    unittest.main()
