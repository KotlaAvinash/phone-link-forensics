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
            self.assertNotIn("copy_sha256", row)
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
        self.assertIn("no metadata entry matches this device folder", md["note"])
        self.assertNotIn("AcmeCorpA", json.dumps(md))
        others = read_acq(self.out, GUID_B)["package_metadata_other_entries"]
        self.assertEqual({e["id"] for e in others}, {GUID_A, "not-a-folder-id"})
        html_b = read_text(os.path.join(self.out, GUID_B, "report.html"))
        dev, sep, pkg = html_b.partition("Package-wide")
        self.assertTrue(sep, "the package's other entries need their own package-wide heading")
        self.assertIn("no metadata entry matches this device folder", dev)
        self.assertNotIn("AcmeCorpA", dev)
        self.assertIn("AcmeCorpA", pkg)

    def test_device_with_entry_gets_only_its_own(self):
        md = read_acq(self.out, GUID_A)["device_metadata"]
        self.assertEqual([e["make"] for e in md["entries"]], ["AcmeCorpA"])
        self.assertNotIn("OtherMakerQ", json.dumps(md))
        html_a = read_text(os.path.join(self.out, GUID_A, "report.html"))
        dev, _, _ = html_a.partition("Package-wide")
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
