"""GUI tests for plmapview.py on the synthetic demo (window kept hidden).

    python test_plmapview.py
"""
import csv
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pldemo  # noqa: E402
import plfilter as F  # noqa: E402
import plmapview as V  # noqa: E402


class ViewerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = pldemo.build(os.path.join(self.tmp.name, "device"))
        self.app = V.Viewer([("demo", src)])
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        work = self.app.ev.work
        self.app.on_close()
        self.assertFalse(os.path.exists(work), "temporary copies should be removed on close")
        self.tmp.cleanup()

    def select(self, vid, index):
        self.app.show(vid, select_index=index)
        self.app.on_row()

    def test_every_view_opens_and_rows_select(self):
        for vid, v in self.app.g.views.items():
            self.app.show(vid)
            self.assertEqual(self.app.cur_vid, vid)
            self.assertEqual(len(self.app.table.get_children()), len(v.rows), vid)
            if v.rows:
                self.select(vid, 0)
                self.assertTrue(self.app.links.get_children(), f"{vid}: links panel empty")

    def test_jump_from_call_to_contact(self):
        self.app.show_cross.set(True)  # call -> contact is a cross-database link
        calls = self.app.g.views["E_calls"].rows
        idx = next(i for i, r in enumerate(calls) if r["CallID"] == 1)
        self.select("E_calls", idx)
        target = next(iid for iid, t in self.app.link_targets.items() if t == ("contact", 1))
        self.app.links.selection_set(target)
        self.app.on_jump()
        self.assertEqual(self.app.cur_vid, "E_contacts")
        row = self.app.g.views["E_contacts"].rows[int(self.app.table.selection()[0])]
        self.assertEqual(row["Name"], "Alice Example")

    def test_relation_row_links_to_its_entities(self):
        self.app.show_cross.set(True)
        self.select("R20", 0)  # photo taken during a call
        row = self.app.g.views["R20"].rows[0]
        mine = self.app.row_entities(row)
        self.assertIn(("call", 1), mine)              # the row is about call 1 and photo 1 ...
        self.assertIn(("photo", 1), mine)
        targets = set(self.app.link_targets.values())
        self.assertFalse(set(mine) & targets)         # ... which are not listed as "linked rows"
        self.assertIn(("contact", 1), targets)        # what they link to is

    def test_rcs_thread_does_not_link_to_unrelated_sms_thread(self):
        rcs = self.app.g.views["E_rcs"].rows
        idx = next(i for i, r in enumerate(rcs) if r["RCSThreadID"] == 51)  # fallback missing
        self.select("E_rcs", idx)
        self.assertFalse([t for t in self.app.link_targets.values() if t[0] == "thread"])

    def test_filter_sort_rebuild_export(self):
        self.app.show("E_calls")
        self.app.filter_var.set("Alice")
        self.assertEqual(len(self.app.table.get_children()), 2)
        self.app.sort_by("CallID")
        self.app.sort_by("CallID")
        before = len(self.app.g.views["R21"].rows)
        self.app.window_min.set(1)
        self.app.rebuild()
        self.assertLessEqual(len(self.app.g.views["R21"].rows), before)
        out = os.path.join(self.tmp.name, "export.csv")
        V.filedialog.asksaveasfilename = lambda **k: out
        self.app.show("E_calls")
        self.app.filter_var.set("Alice")
        self.app.export()
        with open(out, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], self.app.g.views["E_calls"].cols)
        self.assertEqual(len(rows), 3)

    def test_sort_orders_table_keeps_filter_selection_and_export_follows(self):
        self.app.show("E_calls")
        self.app.filter_var.set("")
        v = self.app.g.views["E_calls"]
        shown = lambda: [v.rows[int(k)]["CallID"] for k in self.app.table.get_children()]
        first = self.app.table.get_children()[0]
        self.app.table.selection_set(first)
        self.app.sort_by("CallID")
        self.app.sort_by("CallID")              # second click: descending
        ids = shown()
        self.assertEqual(ids, sorted(ids, key=lambda x: float(x), reverse=True))
        self.assertEqual(self.app.table.selection(), (first,))
        out = os.path.join(self.tmp.name, "sorted.csv")
        V.filedialog.asksaveasfilename = lambda **k: out
        self.app.export()
        with open(out, encoding="utf-8-sig") as f:
            exported = [r[v.cols.index("CallID")] for r in list(csv.reader(f))[1:]]
        self.assertEqual(exported, [str(x) for x in ids])
        self.app.filter_var.set("Alice")        # a filter still applies after a sort
        self.assertEqual(len(self.app.table.get_children()), 2)


class DatabaseFirstTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = V.Viewer([("demo", pldemo.build(os.path.join(self.tmp.name, "device")))])
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        self.app.on_close()
        self.tmp.cleanup()

    def nav_ids(self, parent=""):
        out = []
        for i in self.app.nav.get_children(parent):
            out += [i] + self.nav_ids(i)
        return out

    def test_opens_on_notifications_grouped_by_database(self):
        self.assertEqual(self.app.cur_vid, "N_notifications")
        ids = self.nav_ids()
        self.assertIn("grp:notifications", ids)
        self.assertTrue(self.app.nav.item("grp:notifications", "open"))
        self.assertNotIn("R13", ids)  # cross-database links hidden by default
        self.app.show_cross.set(True)
        self.app.fill_nav()
        self.assertIn("R13", self.nav_ids())

    def test_view_list_uses_plain_names(self):
        self.app.show_cross.set(True)
        self.app.fill_nav()
        views = [i for i in self.nav_ids() if i in self.app.g.views]
        self.assertEqual(sorted(views), sorted(self.app.g.views))          # every view is listed once
        for vid in views:
            text = self.app.nav.item(vid, "text")
            plain = re.sub(r"\s*\(content_sequence\)$", "", text)   # the one raw name the owner asked to show
            self.assertNotRegex(plain, r"^[A-Z]{1,3}\d+\b|\.db\b|_|->", vid)  # no codes, files, raw names
            v = self.app.g.views[vid]
            self.assertEqual(self.app.nav.set(vid, "rows"), V.nav_count_text(v, len(v.rows)))
            if getattr(v, "problems", None) is None and getattr(v, "matched", None) is None:
                self.assertEqual(self.app.nav.set(vid, "rows"), f"{len(v.rows):,}")
        unnamed = [v for v in self.app.g.views if v not in V.NAV and not v.startswith("M_")]
        self.assertEqual(unnamed, [], "give these views a plain name in plmapview.NAV")
        self.app.show("R03")
        self.assertEqual(self.app.title_lbl.cget("text"), "Conversation → its messages")
        self.assertIn("Technical name: R03", self.app.basis_lbl.cget("text"))   # nothing technical is lost
        self.app.nav.item("sub:messages:links", open=True)
        self.app.fill_nav()                                                     # a rebuild keeps what was open
        self.assertTrue(self.app.nav.item("sub:messages:links", "open"))

    def test_notification_links_are_inside_its_database(self):
        rows = self.app.g.views["N_notifications"].rows
        idx = next(i for i, r in enumerate(rows) if r["Source"] == "live" and r["NotificationID"] == "n-1")
        self.app.show("N_notifications", select_index=idx)
        self.app.on_row()
        scopes = {self.app.links.set(i, "scope") for i in self.app.links.get_children()}
        self.assertEqual(scopes, {"same table", "other table, same database"})
        self.assertIn("Raw JSON", self.app.detail.get("1.0", "end"))
        toast = next(i for i, t in self.app.link_targets.items() if t[0] == "toast")
        self.app.links.selection_set(toast)
        self.app.on_jump()
        self.assertEqual(self.app.cur_vid, "N_toasts")


class LinkedPhonesViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pldemo.build_package(self.tmp.name)
        self.app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root))
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        pc_work = self.app.pc.work
        self.app.on_close()
        self.assertFalse(os.path.exists(pc_work))
        self.tmp.cleanup()

    def test_linked_phones_section_and_links(self):
        self.assertIn("PC_devices", self.app.nav.get_children("grp:linked"))
        rows = self.app.g.views["PC_devices"].rows
        idx = next(i for i, r in enumerate(rows) if r["Status"] == "data present")
        self.app.show("PC_devices", select_index=idx)
        self.app.on_row()
        kinds = {t[0] for t in self.app.link_targets.values()}
        self.assertEqual(kinds, {"streamed", "phone"})
        target = next(i for i, t in self.app.link_targets.items() if t[0] == "phone")
        self.app.links.selection_set(target)
        self.app.on_jump()
        self.assertEqual(self.app.cur_vid, "PC_phones")



class ConnectionsViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pldemo.build_package(self.tmp.name)
        self.app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root),
                            connections=V.C.demo_source())
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        self.app.on_close()
        self.tmp.cleanup()

    def test_connections_section_summary_first_and_links(self):
        kids = self.app.nav.get_children("grp:conn")
        self.assertEqual(kids[0], "C_phones")
        rows = self.app.g.views["C_usb"].rows
        idx = next(i for i, r in enumerate(rows) if r["PhoneEvidence"])
        self.app.show("C_usb", select_index=idx)
        self.app.on_row()
        kinds = {t[0] for t in self.app.link_targets.values()}
        self.assertTrue({"wpd", "autoplay", "install", "event"} <= kinds)
        target = next(i for i, t in self.app.link_targets.items() if t[0] == "install")
        self.app.links.selection_set(target)
        self.app.on_jump()
        self.assertEqual(self.app.cur_vid, "C_install")

    def test_hidden_without_a_source(self):
        app = V.Viewer(self.app.devices, localcache=self.app.localcache)
        app.withdraw()
        try:
            self.assertFalse(app.nav.exists("grp:conn"))
        finally:
            app.on_close()



def tkfont_measure(widget, text):
    import tkinter.font as tkfont
    return tkfont.Font(font=widget.cget("font")).measure(text)


class WindowFixesTest(unittest.TestCase):
    """The review fixes for the window: toolbar, cross toggle, nearby box, export, device switch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = pldemo.build(os.path.join(self.tmp.name, "device"))
        self.apps = []
        self.app = self.make([("demo", self.src)])

    def make(self, devices, withdraw=True, **kw):
        app = V.Viewer(devices, **kw)
        if withdraw:
            app.withdraw()
        app.update()
        self.apps.append(app)
        return app

    def tearDown(self):
        for a in self.apps:
            try:
                a.on_close(warn=False)
            except Exception:
                pass
        self.tmp.cleanup()

    # ---- 1 toolbar
    def test_toolbar_controls_stay_usable_with_a_long_source_path(self):
        d2 = pldemo.build(os.path.join(self.tmp.name, "device2"))
        app = self.make([("a" * 10 + "p" * 140, self.src), ("second", d2)], withdraw=False)
        app.geometry("1256x675")
        app.update()
        controls = {"device picker": app.dev_combo, "Nearby box": app.nearby_spin, "inferred tick": app.cross_chk,
                    "Open folder": app.open_btn, "Export": app.export_btn}
        for name, w in controls.items():
            self.assertTrue(w.winfo_ismapped(), name)
            self.assertGreaterEqual(w.winfo_width(), w.winfo_reqwidth(), name)

    def test_long_source_line_is_shortened_in_the_middle_to_fit(self):
        app = self.make([("a" * 10 + "p" * 140, self.src)], withdraw=False)
        app.geometry("600x500")
        app.update()
        text = app.src_lbl.cget("text")
        self.assertIn("…", text)
        self.assertLess(len(text), 100)
        self.assertTrue(text.startswith("aaaa") and text.endswith(self.src[-6:]))
        self.assertLessEqual(tkfont_measure(app.src_lbl, text), app.src_lbl.winfo_width())

    def test_elide_middle(self):
        self.assertEqual(V.elide_middle("short", 20), "short")
        out = V.elide_middle("a" * 50 + "TAIL", 21)
        self.assertEqual(len(out), 21)
        self.assertTrue(out.startswith("aaaa") and out.endswith("TAIL") and "…" in out)

    # ---- 2 inferred toggle
    def test_unticking_inferred_matches_leaves_an_inferred_view(self):
        app = self.app
        app.show_cross.set(True)
        app.fill_nav()
        app.show("R13")
        self.assertEqual(app.g.views["R13"].kind, "cross")
        app.show_cross.set(False)
        app.on_cross_toggle()
        self.assertNotEqual(app.g.views[app.cur_vid].kind, "cross")
        self.assertEqual(app.cur_vid, "N_notifications")
        self.assertFalse(app.nav.exists("R13"))

    def test_toggling_inferred_matches_refreshes_the_linked_rows(self):
        app = self.app
        calls = app.g.views["E_calls"].rows
        idx = next(i for i, r in enumerate(calls) if r["CallID"] == 1)
        scopes = lambda: {app.links.set(i, "scope") for i in app.links.get_children()}
        app.show_cross.set(True)
        app.show("E_calls", select_index=idx)
        app.on_row()
        self.assertIn("other database (inferred)", scopes())
        app.show_cross.set(False)
        app.on_cross_toggle()
        self.assertEqual(app.cur_vid, "E_calls")
        self.assertNotIn("other database (inferred)", scopes())

    # ---- 3 nearby minutes
    def test_nearby_minutes_is_validated_and_clamped(self):
        app = self.app
        for text, want in (("30", 30), ("", 30), ("abc", 30), ("-5", 1), ("99999", 1440), (" 7 ", 7), ("1.5", 7)):
            app.window_min.set(text)
            self.assertEqual(app.nearby(), want, repr(text))
            app.show(app.cur_vid)            # never raises, whatever is in the box
            app.fill_nav()
            app.friendly("R21")

    def test_rebuild_normalises_the_nearby_box(self):
        app = self.app
        app.window_min.set("99999")
        app.rebuild()
        self.assertEqual(app.window_min.get(), "1440")
        app.window_min.set("")
        app.rebuild()
        self.assertEqual(app.window_min.get(), "1440")

    # ---- 4/5 export
    def export_to(self, path, vid="E_calls"):
        self.app.show(vid)
        with mock.patch.object(V.filedialog, "asksaveasfilename", return_value=path):
            self.app.export()

    def test_export_failure_is_shown_and_not_a_success_message(self):
        self.export_to(os.path.join(self.tmp.name, "ok.csv"))
        self.assertIn("Exported", self.app.status.cget("text"))
        bad = os.path.join(self.tmp.name, "no_such_folder", "x.csv")
        with mock.patch.object(V.messagebox, "showerror") as err:
            self.export_to(bad)
        self.assertEqual(err.call_count, 1)
        self.assertNotIn("Exported", self.app.status.cget("text"))
        self.assertIn("FAILED", self.app.status.cget("text"))
        self.assertFalse(os.path.exists(bad))

    def test_export_is_atomic(self):
        out = os.path.join(self.tmp.name, "keep.csv")
        with open(out, "w") as f:
            f.write("old content")
        calls = {"n": 0}
        real = V.csv_cell

        def boom(x):
            calls["n"] += 1
            if calls["n"] > 8:
                raise OSError("disk full")
            return real(x)
        with mock.patch.object(V, "csv_cell", boom), mock.patch.object(V.messagebox, "showerror"):
            self.export_to(out)
        with open(out) as f:
            self.assertEqual(f.read(), "old content")
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")], [])
        self.assertIn("FAILED", self.app.status.cget("text"))

    def test_csv_cells_are_excel_safe(self):
        c = V.csv_cell
        for danger in ("=1+1", "@SUM(A1)", "\tx", "\rx", "+cmd|x", "-cmd", "+ 1", "-A1",
                       "-2+3+cmd|' /C calc'!A0", "+1+1", "+1-555-0150", "+1 555", "-5-5", "-5.", "+5.5", "- 5", "-",
                       "+", "-5\n", "--5", "+-5", "-٣"):
            self.assertEqual(c(danger), "'" + danger, danger)
        for same in ("+15550100", "-5", "-5.5", "-0.5", "3.14", "hello", "", "a=b", "12", "0", "-0"):
            self.assertEqual(c(same), same, same)
        self.assertEqual(c(None), "")
        self.assertEqual(c(42), "42")
        self.assertEqual(c(-3), "-3")
        self.assertEqual(c(b"\x00\x01\x02"), "<3 bytes>")
        self.assertEqual(c("a\x00b"), "ab")
        self.assertEqual(c("\x00=1"), "'=1")      # NULs go first, then the formula check

    def test_export_writes_safe_cells_and_says_so(self):
        v = self.app.g.views["E_calls"]
        v.rows[0]["ContactName"] = "=HYPERLINK(1)"
        v.rows[1]["ContactName"] = b"\xff\xfe"
        v.rows[2]["ContactName"] = "+15550100"
        out = os.path.join(self.tmp.name, "safe.csv")
        self.export_to(out)
        with open(out, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        col = v.cols.index("ContactName")
        got = [r[col] for r in rows[1:4]]
        self.assertEqual(got, ["'=HYPERLINK(1)", "<2 bytes>", "+15550100"])
        text = self.app.status.cget("text")
        self.assertIn("Excel-safe", text)
        self.assertIn("unless the whole cell is a plain number", text)     # the rule, stated
        self.assertIn("-5", text)
        self.assertIn("<N bytes>", text)

    # ---- 6 failed device switch
    def bad_device(self, name="bad"):
        bad = pldemo.build(os.path.join(self.tmp.name, name))
        with open(os.path.join(bad, "calling.db"), "wb") as f:
            f.write(b"this is not a database" * 100)
        return bad

    def test_failed_device_switch_keeps_the_previous_device(self):
        app = self.make([("good", self.src), ("bad", self.bad_device())])
        old_ev, old_g, old_work = app.ev, app.g, app.ev.work
        before = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("plrelate-")}
        app.dev_combo.current(1)
        with mock.patch.object(V.messagebox, "showerror") as err:
            app.load_device(1)
        self.assertEqual(err.call_count, 1)
        self.assertIs(app.ev, old_ev)
        self.assertIs(app.g, old_g)
        self.assertTrue(os.path.isdir(old_work), "the previous device's copies must stay")
        self.assertEqual(app.dev_combo.current(), 0)
        self.assertIn("good", app.src_lbl.cget("text"))
        self.assertNotIn("bad", app.src_lbl.cget("text"))
        after = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("plrelate-")}
        self.assertEqual(after - before, set(), "the failed device's partial copy must be removed")
        app.show("E_calls")                   # still fully usable
        self.assertTrue(app.table.get_children())

    def test_successful_device_switch_removes_the_old_copies(self):
        d2 = pldemo.build(os.path.join(self.tmp.name, "device2"))
        app = self.make([("one", self.src), ("two", d2)])
        old = app.ev.work
        app.load_device(1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(app.ev.work))
        self.assertEqual(app.dev_combo.current(), 1)

    def test_failed_open_folder_keeps_the_previous_device_list(self):
        app = self.app
        folder = os.path.join(self.tmp.name, "other")
        bad = self.bad_device("other")
        old_devices = app.devices
        with mock.patch.object(V.filedialog, "askdirectory", return_value=bad), \
                mock.patch.object(V.messagebox, "showerror") as err:
            app.open_folder()
        self.assertEqual(err.call_count, 1)
        self.assertIs(app.devices, old_devices)
        self.assertEqual(app.cur_vid, "N_notifications")
        self.assertIn("demo", app.src_lbl.cget("text"))

    # ---- 8 this-PC detection
    def test_this_pc_detection_is_the_exact_package_folder(self):
        base = os.path.join(self.tmp.name, "Local")
        pkg = os.path.join(base, "Packages", "Microsoft.YourPhone_8wekyb3d8bbwe")
        f = lambda p: V.is_this_pc_package(p, base)
        self.assertTrue(f(pkg))
        self.assertTrue(f(os.path.join(pkg, "LocalCache", "Indexed")))
        if os.name == "nt":                       # Windows paths ignore case; Linux paths do not
            self.assertTrue(f(pkg.upper()))
        self.assertFalse(f(os.path.join(base, "Temp", "case", "export")))
        self.assertFalse(f(base + "Low"))
        self.assertFalse(f(os.path.join(base + "Low", "Packages", "Microsoft.YourPhone_8wekyb3d8bbwe")))
        self.assertFalse(f(pkg + "_copy"))
        self.assertFalse(f(os.path.join(base, "Packages", "Other")))

    # ---- 9 callback errors
    def test_callback_errors_are_shown_and_printed(self):
        import contextlib
        import io
        err = io.StringIO()
        with mock.patch.object(V.messagebox, "showerror") as box, contextlib.redirect_stderr(err):
            self.app.after(0, lambda: 1 / 0)
            self.app.update()
        self.assertEqual(box.call_count, 1)
        self.assertIn("ZeroDivisionError", box.call_args[0][1])
        self.assertIn("Traceback", err.getvalue())

    # ---- 10 small fixes
    def test_handlers_do_nothing_without_a_loaded_device(self):
        with mock.patch.object(V.messagebox, "showerror"):
            app = self.make([("bad", self.bad_device())])
        self.assertIsNone(app.g)
        with mock.patch.object(V.messagebox, "showerror") as err, \
                mock.patch.object(V.filedialog, "asksaveasfilename", return_value="x.csv"):
            for fn in (app.on_row, app.on_jump, app.on_nav, app.export, app.rebuild, app.populate, app.fill_nav,
                       app.on_cross_toggle, lambda: app.sort_by("x"), lambda: app.show("N_notifications")):
                fn()
        self.assertEqual(err.call_count, 0)

    def test_sort_direction_is_remembered_per_view(self):
        app = self.app
        ids = lambda: [float(app.g.views[app.cur_vid].rows[int(k)]["CallID"]) for k in app.table.get_children()]
        app.show("E_calls")
        app.sort_by("CallID")                  # E_calls is now sorted ascending; its next click is descending
        self.assertEqual(ids(), sorted(ids()))
        other = next(v for v in app.g.views if v != "E_calls" and "CallID" in app.g.views[v].cols
                     and len(app.g.views[v].rows) > 1)
        app.show(other)
        app.sort_by("CallID")                  # first click in this view: ascending, whatever E_calls did
        self.assertEqual(ids(), sorted(ids()))

    def test_selected_row_box_is_read_only_but_selectable(self):
        app = self.app
        app.show("E_calls", select_index=0)
        app.on_row()
        self.assertEqual(app.detail.cget("state"), "disabled")
        text = app.detail.get("1.0", "end").strip()
        self.assertTrue(text)
        app.detail.tag_add("sel", "1.0", "end")
        self.assertTrue(app.detail.tag_ranges("sel"))
        app.detail.insert("end", "typed")      # a disabled Text ignores inserts
        self.assertEqual(app.detail.get("1.0", "end").strip(), text)
        app.populate()                         # clearing on refill still works
        self.assertEqual(app.detail.get("1.0", "end").strip(), "")

    def test_enter_on_a_linked_row_jumps(self):
        app = self.make([("demo", self.src)], withdraw=False)    # key events need a mapped window
        app.show_cross.set(True)
        calls = app.g.views["E_calls"].rows
        idx = next(i for i, r in enumerate(calls) if r["CallID"] == 1)
        app.show("E_calls", select_index=idx)
        app.update()                          # let the table's selection event run first
        app.on_row()
        target = next(iid for iid, t in app.link_targets.items() if t == ("contact", 1))
        app.links.selection_set(target)
        self.assertTrue(app.links.bind("<Return>"))
        app.links.focus_force()
        app.links.event_generate("<Return>")
        app.update()
        self.assertEqual(app.cur_vid, "E_contacts")

    def test_linked_rows_panel_names_r21_with_the_current_window(self):
        app = self.app
        app.window_min.set("17")
        app.rebuild()
        app.show_cross.set(True)
        app.g.link("R21", ("call", 1), ("call", 2), "")        # a link with no note of its own: the panel names the view
        idx = next(i for i, r in enumerate(app.g.views["E_calls"].rows) if r["CallID"] == 1)
        app.show("E_calls", select_index=idx)
        app.on_row()
        hows = [app.links.set(k, "how") for k in app.links.get_children()]
        self.assertIn("Things within 17 min of each other", hows)

    # ---- 11 snapshot warning
    def test_snapshot_note_is_a_warning_in_the_status_bar(self):
        class Noted(V.R.Evidence):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.snapshot_note = "SNAPSHOT-NOTE-TEXT"
        self.assertNotIn("WARNING", self.app.status.cget("text"))
        with mock.patch.object(V.R, "Evidence", Noted):
            self.app.load_device(0)
        self.assertIn("WARNING", self.app.status.cget("text"))
        self.assertIn("SNAPSHOT-NOTE-TEXT", self.app.status.cget("text"))


class LinkedPhonesFailureTest(unittest.TestCase):
    def test_package_copy_is_closed_when_the_connections_build_fails(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as scratch:
            root = pldemo.build_package(tmp)
            old_tempdir, tempfile.tempdir = tempfile.tempdir, scratch      # count only the folders this test makes
            app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root),
                           connections=V.C.demo_source())
            app.withdraw()
            app.update()
            try:
                def left():
                    return [n for n in os.listdir(scratch) if n.startswith("pldevices-")]
                g = V.R.build(app.ev)
                kept = len(left())               # the one the open window holds
                with mock.patch.object(V.C, "build", side_effect=KeyError("odd shape")):
                    with self.assertRaises(KeyError):
                        app._add_linked_phones(g)
                self.assertEqual(len(left()), kept)      # the new package copy was closed, not leaked
            finally:
                app.on_close(warn=False)
                tempfile.tempdir = old_tempdir

    def test_failed_load_with_a_broken_connections_build_keeps_the_old_device(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as scratch:
            root = pldemo.build_package(tmp)
            old_tempdir, tempfile.tempdir = tempfile.tempdir, scratch
            app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root),
                           connections=V.C.demo_source())
            app.withdraw()
            app.update()
            try:
                before = sorted(os.listdir(scratch))
                with mock.patch.object(V.C, "build", side_effect=KeyError("odd shape")),                         mock.patch.object(V.messagebox, "showerror") as err:
                    self.assertFalse(app.load_device(0))
                self.assertEqual(err.call_count, 1)
                self.assertEqual(sorted(os.listdir(scratch)), before)
            finally:
                app.on_close(warn=False)
                tempfile.tempdir = old_tempdir


class OpenFolderSourcesTest(unittest.TestCase):
    """Open folder... must re-decide the package-level views (LocalCache) and the live this-PC views for the NEW
    input, and commit them only when the new input loads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.apps = []

    def tearDown(self):
        for a in self.apps:
            try:
                a.on_close(warn=False)
            except Exception:
                pass
        self.tmp.cleanup()

    def make(self, devices, **kw):
        app = V.Viewer(devices, **kw)
        app.withdraw()
        app.update()
        self.apps.append(app)
        return app

    def package(self, name, phone):
        """A synthetic package whose one linked phone is called `phone`. Returns (package root, LocalCache)."""
        root = pldemo.build_package(os.path.join(self.tmp.name, name))
        lc = V.D.find_localcache(root)
        meta = os.path.join(lc, "DeviceMetadataStorage.json")
        with open(meta, encoding="utf-8") as f:
            text = f.read()
        with open(meta, "w", encoding="utf-8") as f:
            f.write(text.replace("Example Phone", phone))
        return root, lc

    def open(self, app, folder):
        with mock.patch.object(V.filedialog, "askdirectory", return_value=folder), \
                mock.patch.object(V.messagebox, "showerror") as err:
            app.open_folder()
        return err

    def phones(self, app):
        # every demo package also lists the same Windows PC and older phone entry; they cannot tell cases apart
        same_in_every_case = {pldemo.DEMO_PC_NAME, pldemo.DEMO_OLD_PHONE_NAME}
        names = {r.get("DisplayName") for r in app.g.views["PC_phones"].rows if r.get("DisplayName")}
        self.assertTrue(same_in_every_case <= names, names)
        return names - same_in_every_case

    def test_opening_a_second_package_shows_its_own_linked_phones(self):
        root_a, lc_a = self.package("case-a", "CASE A PHONE")
        root_b, lc_b = self.package("case-b", "CASE B PHONE")
        app = self.make(V.P.find_device_dirs(root_a), localcache=lc_a)
        self.assertEqual(self.phones(app), {"CASE A PHONE"})
        old_pc_work = app.pc.work
        err = self.open(app, root_b)
        self.assertEqual(err.call_count, 0)
        self.assertEqual(self.phones(app), {"CASE B PHONE"})
        copies = app.g.views["PC_copies"].basis
        self.assertIn(lc_b, copies)
        self.assertNotIn(lc_a, copies)
        self.assertEqual(app.localcache, lc_b)
        self.assertIn(root_b, app.src_full)
        self.assertNotIn(root_a, app.src_full)
        self.assertFalse(os.path.exists(old_pc_work), "case A's package copy must be released")

    def test_opening_a_package_after_a_bare_device_folder_adds_the_linked_phones(self):
        bare = pldemo.build(os.path.join(self.tmp.name, "bare", "device"))
        app = self.make([("bare", bare)])
        self.assertIsNone(app.pc)
        self.assertNotIn("PC_phones", app.g.views)
        root_b, lc_b = self.package("case-b", "CASE B PHONE")
        self.open(app, root_b)
        self.assertEqual(app.localcache, lc_b)
        self.assertIsNotNone(app.pc)
        self.assertEqual(self.phones(app), {"CASE B PHONE"})
        self.assertIn("PC_phones", app.nav.get_children("grp:linked"))

    def test_live_views_are_dropped_for_a_folder_that_is_not_this_pc(self):
        root_a, lc_a = self.package("case-a", "CASE A PHONE")
        root_b, _ = self.package("case-b", "CASE B PHONE")
        app = self.make(V.P.find_device_dirs(root_a), localcache=lc_a, connections=V.C.demo_source())
        self.assertTrue(app.nav.exists("grp:conn"))
        with mock.patch.object(V.C, "LiveSource", side_effect=AssertionError("never read the real PC in tests")):
            self.open(app, root_b)
        self.assertIsNone(app.connections)
        self.assertFalse(app.nav.exists("grp:conn"))
        self.assertFalse([v for v in app.g.views.values() if v.kind == "conn"])
        status = app.status.cget("text")
        self.assertIn("USB/Bluetooth views hidden", status)
        self.assertIn("--this-pc", status)

    def test_this_pc_flag_keeps_the_live_views(self):
        root_a, lc_a = self.package("case-a", "CASE A PHONE")
        root_b, _ = self.package("case-b", "CASE B PHONE")
        app = self.make(V.P.find_device_dirs(root_a), localcache=lc_a, connections=V.C.demo_source(), this_pc=True)
        src = app.connections
        self.open(app, root_b)
        self.assertIs(app.connections, src)
        self.assertTrue(app.nav.exists("grp:conn"))
        self.assertNotIn("USB/Bluetooth views hidden", app.status.cget("text"))

    def test_opening_this_pcs_package_adds_the_live_views(self):
        root_a, lc_a = self.package("case-a", "CASE A PHONE")
        root_b, _ = self.package("case-b", "CASE B PHONE")
        app = self.make(V.P.find_device_dirs(root_a), localcache=lc_a, live_source=V.C.demo_source)
        self.assertFalse(app.nav.exists("grp:conn"))
        with mock.patch.object(V, "is_this_pc_package", lambda p, *a: os.path.normcase(p) == os.path.normcase(root_b)):
            self.open(app, root_b)
        self.assertIsNotNone(app.connections)
        self.assertTrue(app.nav.exists("grp:conn"))

    def test_failed_open_keeps_the_previous_linked_phones_and_live_views(self):
        root_a, lc_a = self.package("case-a", "CASE A PHONE")
        root_b, lc_b = self.package("case-b", "CASE B PHONE")
        for dirpath, _, files in os.walk(os.path.join(lc_b, "Indexed")):     # every database in case B is corrupt
            for f in files:
                if f.endswith(".db"):
                    with open(os.path.join(dirpath, f), "wb") as fh:
                        fh.write(b"this is not a database" * 100)
        app = self.make(V.P.find_device_dirs(root_a), localcache=lc_a, connections=V.C.demo_source())
        old_pc, old_conn, old_g = app.pc, app.connections, app.g
        before = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith(("plrelate-", "pldevices-"))}
        err = self.open(app, root_b)
        self.assertEqual(err.call_count, 1)
        self.assertEqual(app.localcache, lc_a)
        self.assertIs(app.connections, old_conn)
        self.assertIs(app.pc, old_pc)
        self.assertIs(app.g, old_g)
        self.assertTrue(os.path.isdir(old_pc.work), "case A's package copy must stay")
        self.assertEqual(self.phones(app), {"CASE A PHONE"})
        self.assertTrue(app.nav.exists("grp:conn"))
        self.assertNotIn("USB/Bluetooth views hidden", app.status.cget("text"))
        after = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith(("plrelate-", "pldevices-"))}
        self.assertEqual(after - before, set(), "the failed case's partial copies must be removed")
        app.window_min.set("7")
        app.rebuild()                             # a rebuild still uses case A's package
        self.assertEqual(self.phones(app), {"CASE A PHONE"})


class DemoRedactionTest(unittest.TestCase):
    def test_demo_mode_never_shows_a_local_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pldemo.build_package(tmp)
            app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root),
                           connections=V.C.demo_source(), redact=True)
            app.withdraw()
            app.update()
            try:
                needles = {os.path.normcase(x) for x in (tmp, tempfile.gettempdir(), app.ev.work, app.pc.work)}
                texts = {"status": app.status.cget("text"), "source": app.src_lbl.cget("text")}
                app.show_cross.set(True)
                app.fill_nav()
                for vid in app.g.views:
                    app.show(vid)
                    texts["banner " + vid] = app.basis_lbl.cget("text")
                    texts["status " + vid] = app.status.cget("text")
                for what, text in texts.items():
                    low = os.path.normcase(text)
                    for n in needles:
                        self.assertNotIn(n, low, what)
                self.assertIn("synthetic demo folder", texts["banner P_copies"])
                self.assertIn("synthetic demo folder", texts["banner PC_copies"])
                shown = []
                with mock.patch.object(V.messagebox, "showerror", lambda t, m: shown.append(m)), \
                        mock.patch.object(V.filedialog, "asksaveasfilename",
                                          return_value=os.path.join(tmp, "nope", "x.csv")):
                    app.export()
                self.assertTrue(shown)
                self.assertNotIn(os.path.normcase(tmp), os.path.normcase(shown[0]))
                self.assertNotIn(os.path.normcase(tmp), os.path.normcase(app.status.cget("text")))
            finally:
                app.on_close(warn=False)

    def test_empty_folder_dialog_is_redacted_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pldemo.build_package(tmp)
            app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root),
                           connections=V.C.demo_source(), redact=True)
            app.withdraw()
            app.update()
            try:
                empty = os.path.join(tmp, "somebody-else-case")
                os.makedirs(empty)
                shown = []
                with mock.patch.object(V.filedialog, "askdirectory", return_value=empty),                         mock.patch.object(V.messagebox, "showerror", lambda t, m: shown.append(m)):
                    app.open_folder()
                self.assertEqual(len(shown), 1)
                self.assertNotIn("somebody-else-case", shown[0])
                self.assertNotIn(os.path.normcase(tmp), os.path.normcase(shown[0]))
                self.assertIn("synthetic demo folder", shown[0])
            finally:
                app.on_close(warn=False)

    def test_empty_folder_dialog_names_the_folder_outside_demo_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pldemo.build(os.path.join(tmp, "device"))
            app = V.Viewer([("demo", src)])
            app.withdraw()
            app.update()
            try:
                empty = os.path.join(tmp, "empty-case")
                os.makedirs(empty)
                shown = []
                with mock.patch.object(V.filedialog, "askdirectory", return_value=empty),                         mock.patch.object(V.messagebox, "showerror", lambda t, m: shown.append(m)):
                    app.open_folder()
                self.assertIn("empty-case", shown[0])
            finally:
                app.on_close(warn=False)


# ==================================================================== plfilter: pure sorting and filtering logic
class FilterParseTest(unittest.TestCase):
    def test_blank_is_empty_or_none_only(self):
        for b in (None, "", b""):
            self.assertTrue(F.is_blank(b), repr(b))
        for nb in (" ", "‎", 0, "0", "x"):
            self.assertFalse(F.is_blank(nb), repr(nb))

    def test_parse_number(self):
        for text, want in (("1,234", 1234.0), ("-5", -5.0), ("3.14", 3.14), (12, 12.0), (2.5, 2.5), ("+7", 7.0),
                           (" 42 ", 42.0), ("1,234,567.5", 1234567.5), ("-0.5", -0.5)):
            self.assertEqual(F.parse_number(text), want, repr(text))
        for bad in ("", None, "abc", "+1-555-0101", "1,23", "12a", "1.2.3", "inf", "nan", "1e5x", True):
            self.assertIsNone(F.parse_number(bad), repr(bad))

    def test_parse_time_formats(self):
        from datetime import datetime
        want = datetime(2030, 9, 22, 11, 22, 12)
        for text in ("2030-09-22 11:22:12", "2030-09-22T11:22:12", "2030-09-22T11:22:12Z", "2030-09-22 11:22:12Z",
                     "2030-09-22T11:22:12+05:30", "2030-09-22T11:22:12-0700", "2030-09-22 11:22:12 (UTC)",
                     " 2030-09-22 11:22:12 "):
            self.assertEqual(F.parse_time(text), want, text)     # as written: the offset is not applied
        self.assertIsNone(F.parse_time("2030-09-22 11:22:12 (no zone)"))    # no zone: undated
        self.assertEqual(F.parse_time("2030-09-22 11:22:12.123"), datetime(2030, 9, 22, 11, 22, 12, 123000))
        self.assertEqual(F.parse_time("2030-09-22T11:22:12.1234567Z"), datetime(2030, 9, 22, 11, 22, 12, 123456))
        self.assertEqual(F.parse_time("2030-09-22"), datetime(2030, 9, 22))
        self.assertEqual(F.parse_time("2030-09-22 11:22"), datetime(2030, 9, 22, 11, 22))
        for bad in ("", None, "2030-13-01 00:00:00", "2030-02-30", "yesterday", "22/09/2030", 1900000000, "2030-09"):
            self.assertIsNone(F.parse_time(bad), repr(bad))

    def test_parse_when_gives_the_span_of_what_was_typed(self):
        from datetime import datetime
        self.assertEqual(F.parse_when("2030-09-15"), (datetime(2030, 9, 15), datetime(2030, 9, 16)))
        self.assertEqual(F.parse_when("2030-09-15 10:30"), (datetime(2030, 9, 15, 10, 30), datetime(2030, 9, 15, 10, 31)))
        self.assertEqual(F.parse_when("2030-09-15T10:30:05"),
                         (datetime(2030, 9, 15, 10, 30, 5), datetime(2030, 9, 15, 10, 30, 6)))
        self.assertEqual(F.parse_when("2030-12-31"), (datetime(2030, 12, 31), datetime(2031, 1, 1)))
        for bad in ("", "15/09/2030", "2030-09-31", "soon"):
            self.assertIsNone(F.parse_when(bad), bad)

    def test_type_detection(self):
        self.assertEqual(F.detect_type(["2030-09-22 11:22:12", "2030-09-23 10:00:00.5", "", None], "StartUTC"), F.DATE)
        self.assertEqual(F.detect_type(["2030-09-22T11:22:12Z"] * 3, "When"), F.DATE)
        self.assertEqual(F.detect_type(["1", "2", "1,234", 7, None], "Count"), F.NUMBER)
        self.assertEqual(F.detect_type(["Alice", "Bob", "12"], "Name"), F.TEXT)
        self.assertEqual(F.detect_type(["+1-555-0101", "+1-555-0102"], "Number"), F.TEXT)
        self.assertEqual(F.detect_type([None, ""], "LastSeenUTC"), F.DATE)       # empty: the name decides
        self.assertEqual(F.detect_type([None, ""], "Name"), F.TEXT)
        # a time-named column that is mostly times is a date column even with a few odd cells
        self.assertEqual(F.detect_type(["2030-09-22 11:22:12"] * 6 + ["unknown"] * 4, "PostTimeUTC"), F.DATE)
        self.assertEqual(F.detect_type(["2030-09-22 11:22:12"] * 6 + ["unknown"] * 4, "Notes"), F.TEXT)

    def test_sort_labels_follow_the_type_like_excel(self):
        self.assertEqual(F.sort_labels(F.TEXT), ("Sort A → Z", "Sort Z → A"))
        self.assertEqual(F.sort_labels(F.NUMBER), ("Sort Smallest → Largest", "Sort Largest → Smallest"))
        self.assertEqual(F.sort_labels(F.DATE), ("Sort Oldest → Newest", "Sort Newest → Oldest"))


class FilterSortTest(unittest.TestCase):
    def order(self, values, ctype, desc=False):
        rows = [{"c": v} for v in values]
        return [rows[i]["c"] for i in F.sort_indices(range(len(rows)), rows, "c", ctype, desc)]

    def test_blanks_last_in_both_directions(self):
        vals = ["b", None, "a", "", "c"]
        self.assertEqual(self.order(vals, F.TEXT), ["a", "b", "c", None, ""])
        self.assertEqual(self.order(vals, F.TEXT, True), ["c", "b", "a", None, ""])
        nums = [3, None, "1,234", "", 2]
        self.assertEqual(self.order(nums, F.NUMBER), [2, 3, "1,234", None, ""])
        self.assertEqual(self.order(nums, F.NUMBER, True), ["1,234", 3, 2, None, ""])

    def test_text_is_case_insensitive_natural_order(self):
        vals = ["img10.jpg", "IMG2.jpg", "img1.jpg", "Bob", "alice", "Alice"]
        self.assertEqual(self.order(vals, F.TEXT), ["alice", "Alice", "Bob", "img1.jpg", "IMG2.jpg", "img10.jpg"])

    def test_numbers_numerically_and_dates_chronologically(self):
        self.assertEqual(self.order(["10", "9", "1,000", "-1", "2.5"], F.NUMBER), ["-1", "2.5", "9", "10", "1,000"])
        dates = ["2030-09-22 11:22:12", "2030-01-02 03:04:05 (no zone)", "2030-09-22T11:22:12.5Z", "2029-12-31"]
        self.assertEqual(self.order(dates, F.DATE),      # a time without a zone cannot be ordered: after the others
                         ["2029-12-31", "2030-09-22 11:22:12", "2030-09-22T11:22:12.5Z", "2030-01-02 03:04:05 (no zone)"])
        self.assertEqual(self.order(dates, F.DATE, True)[0], "2030-09-22T11:22:12.5Z")
        self.assertEqual(self.order(dates, F.DATE, True)[-1], "2030-01-02 03:04:05 (no zone)")   # also last

    def test_odd_cells_sort_after_typed_ones_but_before_blanks(self):
        self.assertEqual(self.order(["x", 5, None, 1], F.NUMBER), [1, 5, "x", None])
        self.assertEqual(self.order(["?", "2030-01-01", "", "2029-01-01"], F.DATE), ["2029-01-01", "2030-01-01", "?", ""])

    def test_sort_is_stable(self):
        rows = [{"k": "a", "n": i} for i in range(5)] + [{"k": "A", "n": 9}]
        got = F.sort_indices(range(6), rows, "k", F.TEXT, False)
        self.assertEqual(got, [0, 1, 2, 3, 4, 5])
        got = F.sort_indices(range(6), rows, "k", F.TEXT, True)
        self.assertEqual(got, [0, 1, 2, 3, 4, 5])      # equal keys keep their order in both directions


class FilterConditionTest(unittest.TestCase):
    def cond(self, ctype, op, a, b=""):
        return F.make_condition(ctype, op, a, b)

    def test_text_conditions(self):
        t = lambda op, a, v: self.cond(F.TEXT, op, a).test(v)
        self.assertTrue(t("contains", "ANA", "Banana"))
        self.assertFalse(t("contains", "x", "Banana"))
        self.assertTrue(t("does not contain", "x", "Banana"))
        self.assertTrue(t("equals", "alice example", "Alice Example"))
        self.assertFalse(t("equals", "alice", "Alice Example"))
        self.assertTrue(t("begins with", "ali", "Alice"))
        self.assertTrue(t("ends with", "CE", "Alice"))
        self.assertFalse(t("ends with", "ali", "Alice"))
        self.assertEqual(sorted(F.OPS[F.TEXT]), sorted(["contains", "does not contain", "equals", "begins with",
                                                        "ends with"]))

    def test_number_conditions(self):
        t = lambda op, a, v, b="": self.cond(F.NUMBER, op, a, b).test(v)
        self.assertTrue(t("equals", "1,234", 1234))
        self.assertTrue(t("does not equal", "5", "6"))
        self.assertTrue(t("does not equal", "5", "abc"))    # not a number: certainly not 5
        self.assertTrue(t("greater than", "5", "5.5"))
        self.assertFalse(t("greater than", "5", "5"))
        self.assertFalse(t("greater than", "5", "abc"))
        self.assertTrue(t("less than", "5", "-1"))
        self.assertTrue(t("between", "1", 1, "3"))
        self.assertTrue(t("between", "1", "3", "3"))
        self.assertFalse(t("between", "1", "4", "3"))

    def test_invalid_input_is_an_error_with_a_message(self):
        for ctype, op, a, b in ((F.NUMBER, "greater than", "abc", ""), (F.NUMBER, "between", "1", ""),
                                (F.NUMBER, "between", "5", "1"), (F.DATE, "after", "15/09/2030", ""),
                                (F.DATE, "between", "", ""), (F.DATE, "between", "2030-09-15", "2030-09-01"),
                                (F.TEXT, "contains", "", ""), (F.TEXT, "bogus", "x", "")):
            with self.assertRaises(F.FilterError, msg=(ctype, op, a, b)) as cm:
                self.cond(ctype, op, a, b)
            self.assertTrue(str(cm.exception))

    def test_date_conditions(self):
        t = lambda op, a, v, b="": self.cond(F.DATE, op, a, b).test(v)
        self.assertTrue(t("on", "2030-09-15", "2030-09-15 23:59:59"))
        self.assertFalse(t("on", "2030-09-15", "2030-09-16 00:00:00"))
        self.assertTrue(t("before", "2030-09-15", "2030-09-14 23:59:59"))
        self.assertFalse(t("before", "2030-09-15", "2030-09-15 00:00:00"))
        self.assertTrue(t("after", "2030-09-15", "2030-09-16 00:00:00"))
        self.assertFalse(t("after", "2030-09-15", "2030-09-15 23:00:00"))     # after the whole day
        self.assertTrue(t("after", "2030-09-15 10:00", "2030-09-15 10:01:00"))
        self.assertTrue(t("between", "2030-09-01", "2030-09-15 23:59:59", "2030-09-15"))   # To is inclusive
        self.assertTrue(t("between", "2030-09-01", "2030-09-01 00:00:00", "2030-09-15"))
        self.assertFalse(t("between", "2030-09-01", "2030-09-16 00:00:00", "2030-09-15"))
        self.assertTrue(t("between", "", "2020-01-01", "2030-09-15"))        # open From
        self.assertTrue(t("between", "2030-09-01", "2099-01-01", ""))         # open To
        self.assertIsNone(t("after", "2030-09-15", "not a time"))             # undated: not decided by the condition

    def test_presets_are_relative_to_the_newest_value_not_now(self):
        vals = ["2020-03-10 12:00:00", "2020-03-09 13:00:00", "2020-03-01 00:00:00", None, "?", "2020-02-01"]
        self.assertEqual(F.newest(vals).isoformat(" "), "2020-03-10 12:00:00")
        frm, to = F.preset_range(vals, "Last 24 h of data")
        self.assertEqual((frm, to), ("2020-03-09 12:00:00", "2020-03-10 12:00:00"))
        c = self.cond(F.DATE, "between", frm, to)
        kept = [v for v in vals if c.test(v)]
        self.assertEqual(kept, ["2020-03-10 12:00:00", "2020-03-09 13:00:00"])
        self.assertEqual(F.preset_range(vals, "Last 7 days of data")[0], "2020-03-03 12:00:00")
        self.assertEqual(F.preset_range(vals, "Last 30 days of data")[0], "2020-02-09 12:00:00")
        self.assertEqual(F.preset_range([None, "x"], "Last 7 days of data"), None)
        self.assertEqual(list(F.PRESETS), ["Last 24 h of data", "Last 7 days of data", "Last 30 days of data"])


class ColumnFilterTest(unittest.TestCase):
    ROWS = [{"Name": "Alice", "When": "2030-09-01 10:00:00", "N": 1},
            {"Name": "Bob", "When": "2030-09-10 10:00:00", "N": 2},
            {"Name": "", "When": None, "N": 3},
            {"Name": "Banana", "When": "2030-09-20 10:00:00", "N": None},
            {"Name": None, "When": "garbled", "N": 5}]

    def test_values_and_blanks(self):
        f = F.ColumnFilter(F.TEXT, values={"Alice", "Banana"})
        self.assertEqual(F.filter_indices(self.ROWS, [("Name", f)]), [0, 2, 3, 4])   # blanks kept by default
        f = F.ColumnFilter(F.TEXT, values={"Alice", "Banana"}, blanks=False)
        self.assertEqual(F.filter_indices(self.ROWS, [("Name", f)]), [0, 3])
        self.assertFalse(F.ColumnFilter(F.TEXT).active)
        self.assertTrue(F.ColumnFilter(F.TEXT, blanks=False).active)

    def test_filters_on_several_columns_combine_with_and(self):
        a = F.ColumnFilter(F.TEXT, cond=F.make_condition(F.TEXT, "contains", "b"), blanks=False)
        b = F.ColumnFilter(F.NUMBER, cond=F.make_condition(F.NUMBER, "less than", "3"), blanks=False)
        self.assertEqual(F.filter_indices(self.ROWS, [("Name", a)]), [1, 3])
        self.assertEqual(F.filter_indices(self.ROWS, [("N", b)]), [0, 1])
        self.assertEqual(F.filter_indices(self.ROWS, [("Name", a), ("N", b)]), [1])

    def test_date_between_keeps_blank_and_undated_rows_unless_unticked(self):
        c = F.make_condition(F.DATE, "between", "2030-09-05", "2030-09-15")
        kept = F.ColumnFilter(F.DATE, cond=c)
        self.assertEqual(F.filter_indices(self.ROWS, [("When", kept)]), [1, 2, 4])     # Bob + blank + undated
        dropped = F.ColumnFilter(F.DATE, cond=c, blanks=False)
        self.assertEqual(F.filter_indices(self.ROWS, [("When", dropped)]), [1])
        self.assertIn("blanks kept", kept.describe("When"))
        self.assertIn("between 2030-09-05 and 2030-09-15", kept.describe("When"))
        self.assertIn("blanks removed", dropped.describe("When"))

    def test_date_value_keys_are_days(self):
        f = F.ColumnFilter(F.DATE, values={"2030-09-10"})
        self.assertEqual(F.filter_indices(self.ROWS, [("When", f)]), [1, 2])
        self.assertEqual(F.value_key("2030-09-10 10:00:00", F.DATE), "2030-09-10")
        self.assertEqual(F.value_key("garbled", F.DATE), "garbled")
        self.assertEqual(F.value_key(12, F.NUMBER), "12")

    def test_text_search_over_all_columns(self):
        self.assertEqual(F.filter_indices(self.ROWS, [], text="ban", cols=["Name", "When"]), [3])
        self.assertEqual(F.filter_indices(self.ROWS, [], text="2030-09-1", cols=["Name", "When"]), [1])

    def test_distinct_values_and_counts(self):
        rows = [{"c": v} for v in ("b", "a", "b", None, "", "A")]
        items, blanks = F.distinct([r["c"] for r in rows], F.TEXT)
        self.assertEqual(items, [("a", 1), ("A", 1), ("b", 2)])
        self.assertEqual(blanks, 2)
        items, blanks = F.distinct(["2030-09-01 10:00:00", "2030-09-01 11:00:00", "2029-01-01", "odd"], F.DATE)
        self.assertEqual(items, [("2029-01-01", 1), ("2030-09-01", 2), ("odd", 1)])

    def test_describe_all(self):
        filters = [("Name", F.ColumnFilter(F.TEXT, cond=F.make_condition(F.TEXT, "contains", "ana"), blanks=False)),
                   ("StartUTC", F.ColumnFilter(F.DATE, cond=F.make_condition(F.DATE, "between", "2030-09-01",
                                                                            "2030-09-15")))]
        text = F.describe_all(filters, shown=112, total=686)
        self.assertEqual(text, "Name contains 'ana' (blanks removed); StartUTC between 2030-09-01 and 2030-09-15 "
                               "(blanks kept) — 112 of 686 rows")
        self.assertEqual(F.describe_all([], shown=686, total=686), "No filters — 686 rows")
        self.assertIn("text search 'x'", F.describe_all([], text="x", shown=1, total=2))
        values = F.ColumnFilter(F.TEXT, values={"Bob", "Alice"})
        self.assertEqual(values.describe("Name"), "Name is one of 'Alice', 'Bob'")
        many = F.ColumnFilter(F.TEXT, values={str(i) for i in range(20)})
        self.assertEqual(many.describe("Name"), "Name is one of 20 ticked values")


class InvisibleCharTest(unittest.TestCase):
    def test_invisible_and_control_characters_are_shown(self):
        self.assertEqual(F.visible("‎"), "⟨U+200E⟩")
        self.assertEqual(F.visible("a​b"), "a⟨U+200B⟩b")
        for cp in list(range(0x200B, 0x2010)) + list(range(0x202A, 0x202F)) + list(range(0x2060, 0x2065)) + \
                [0xFEFF, 0x00, 0x07, 0x1F, 0x7F, 0x80, 0x9F, 0x00AD, 0x115F, 0x3164]:
            ch = chr(cp)
            self.assertEqual(F.visible(ch), f"⟨U+{cp:04X}⟩", hex(cp))
        self.assertEqual(F.visible("plain text, café नमस्ते"),
                         "plain text, café नमस्ते")   # real letters untouched
        self.assertEqual(F.visible("a\nb\tc"), "a⟨U+000A⟩b⟨U+0009⟩c")
        self.assertEqual(F.visible("a\nb\tc", keep_breaks=True), "a\nb\tc")
        self.assertEqual(F.visible(""), "")

    def test_cell_text(self):
        self.assertEqual(F.cell_text(None), "")
        self.assertEqual(F.cell_text(5), "5")
        self.assertEqual(F.cell_text(b"\x00\x01"), "<2 bytes>")


class ChecklistTest(unittest.TestCase):
    def test_select_all_search_and_result(self):
        cl = F.Checklist([("Alice", 2), ("Bob", 1), ("Carol", 3)], blanks=2)
        self.assertEqual(cl.result(), (None, True))           # nothing changed: no filter
        cl.set("Bob", False)
        self.assertEqual(cl.result(), (frozenset({"Alice", "Carol"}), True))
        self.assertEqual(cl.all_state(), "partial")
        cl.set_all(True)
        self.assertEqual(cl.all_state(), "on")
        cl.search("AR")                                        # only Carol matches: OK keeps only what is ticked of it
        self.assertEqual(cl.shown()[0], ["Carol"])
        self.assertEqual(cl.result(), (frozenset({"Carol"}), True))
        cl.search("")
        cl.blanks = False
        self.assertEqual(cl.result(), (None, False))
        cl.set_all(False)
        self.assertEqual(cl.result(), (frozenset(), False))

    def test_shown_list_is_capped_but_search_covers_all(self):
        items = [(f"v{i:05d}", 1) for i in range(F.SHOWN_CAP + 50)]
        cl = F.Checklist(items, blanks=0)
        keys, capped, total = cl.shown()
        self.assertEqual(len(keys), F.SHOWN_CAP)
        self.assertTrue(capped)
        self.assertEqual(total, F.SHOWN_CAP + 50)
        cl.search("v10049")
        self.assertEqual(cl.shown(), (["v10049"], False, 1))
        self.assertEqual(cl.result(), (frozenset({"v10049"}), True))

    def test_date_tree_groups_year_month_day_with_tri_state(self):
        cl = F.Checklist([("2029-12-31", 1), ("2030-09-01", 2), ("2030-09-02", 1), ("2030-10-01", 1), ("odd", 1)],
                         blanks=0)
        tree = F.group_days(cl.shown()[0])
        self.assertEqual(tree["years"], [("2029", [("2029-12", ["2029-12-31"])]),
                                         ("2030", [("2030-09", ["2030-09-01", "2030-09-02"]),
                                                   ("2030-10", ["2030-10-01"])])])
        self.assertEqual(tree["other"], ["odd"])
        cl.set("2030-09-01", False)
        self.assertEqual(cl.group_state(["2030-09-01", "2030-09-02"]), "partial")
        self.assertEqual(cl.group_state(["2030-10-01"]), "on")
        cl.set("2030-09-02", False)
        self.assertEqual(cl.group_state(["2030-09-01", "2030-09-02"]), "off")
        self.assertEqual(F.month_label("2030-09"), "September")


class PipelineTest(unittest.TestCase):
    def test_shown_indices_filters_then_sorts_and_keeps_the_sort(self):
        rows = [{"Name": n, "N": i} for i, n in enumerate(["d", "b", "a", "c", None])]
        f = {"Name": F.ColumnFilter(F.TEXT, values={"a", "b", "c"})}
        got = F.shown_indices(rows, ["Name", "N"], {"Name": F.TEXT, "N": F.NUMBER}, f, sort=("Name", True))
        self.assertEqual([rows[i]["Name"] for i in got], ["c", "b", "a", None])
        got = F.shown_indices(rows, ["Name", "N"], {"Name": F.TEXT, "N": F.NUMBER}, f, sort=("Name", True), text="b")
        self.assertEqual(got, [1])
        rng = ("N", F.ColumnFilter(F.NUMBER, cond=F.make_condition(F.NUMBER, "less than", "3")))
        got = F.shown_indices(rows, ["Name", "N"], {"Name": F.TEXT, "N": F.NUMBER}, {}, extra=[rng])
        self.assertEqual(got, [0, 1, 2])


# ==================================================================== the AutoFilter panel and the filter UI
class AutoFilterTest(unittest.TestCase):
    """Excel-style heading panels on the synthetic demo (window hidden unless a test needs key events)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = pldemo.build(os.path.join(self.tmp.name, "device"))
        self.apps = []
        self.app = self.make([("demo", self.src)])

    def make(self, devices, withdraw=True, **kw):
        app = V.Viewer(devices, **kw)
        if withdraw:
            app.withdraw()
        app.update()
        self.apps.append(app)
        return app

    def tearDown(self):
        for a in self.apps:
            try:
                a.on_close(warn=False)
            except Exception:
                pass
        self.tmp.cleanup()

    def col(self, name, app=None):
        app = app or self.app
        v = app.g.views[app.cur_vid]
        return [v.rows[int(k)].get(name) for k in app.table.get_children()]

    # ---- markers and panel contents
    def test_every_heading_has_a_dropdown_marker(self):
        for vid in ("E_contacts", "E_calls", "N_notifications", "R23"):
            self.app.show(vid)
            for c in self.app.g.views[vid].cols:
                text = self.app.table.heading(c, "text")
                self.assertTrue(text.startswith(c), (vid, c, text))
                self.assertIn(V.DROP_MARK, text, (vid, c))
                self.assertNotIn(V.FILTER_MARK, text)

    def test_text_column_panel_tick_values_and_clear(self):
        app = self.app
        app.show("E_contacts")
        p = app.open_filter("Name")
        self.assertIs(app.panel, p)
        self.assertEqual(p.ctype, F.TEXT)
        self.assertEqual([b.cget("text") for b in p.sort_btns], ["Sort A \u2192 Z", "Sort Z \u2192 A"])
        self.assertEqual(str(p.clear_btn.cget("state")), "disabled")           # not filtered yet
        self.assertEqual(list(p.op_combo.cget("values")), ["(none)"] + F.OPS[F.TEXT])
        items = p.list.get_children()
        self.assertEqual(items[0], "all")
        self.assertIn("(Select All)", p.list.item("all", "text"))
        self.assertIn("v:Alice Example", items)
        self.assertIn("(1)", p.list.item("v:Alice Example", "text"))           # its row count
        p.toggle_item("v:Alice Example")
        self.assertIn(V.UNCHECKED, p.list.item("v:Alice Example", "text"))
        p.ok()
        self.assertIsNone(app.panel)
        self.assertNotIn("Alice Example", self.col("Name"))
        self.assertEqual(len(app.table.get_children()), 5)
        self.assertIn(V.FILTER_MARK, app.table.heading("Name", "text"))
        self.assertEqual(app.nav.set("E_contacts", "rows"), "5 of 6")
        self.assertIn("5 of 6 rows", app.filter_lbl.cget("text"))
        self.assertIn("Name is one of", app.filter_lbl.cget("text"))
        p = app.open_filter("Name")
        self.assertEqual(str(p.clear_btn.cget("state")), "normal")
        self.assertIn('"Name"', p.clear_btn.cget("text"))
        p.clear()
        self.assertEqual(len(app.table.get_children()), 6)
        self.assertNotIn(V.FILTER_MARK, app.table.heading("Name", "text"))
        self.assertEqual(app.nav.set("E_contacts", "rows"), "6")

    def test_search_narrows_the_value_list_and_ok_keeps_the_matches(self):
        app = self.app
        app.show("E_contacts")
        p = app.open_filter("Name")
        p.search_var.set("bob")
        values = [i for i in p.list.get_children() if i.startswith("v:")]
        self.assertEqual(values, ["v:Bob Example"])
        p.ok()
        self.assertEqual(self.col("Name"), ["Bob Example"])

    def test_sort_from_the_panel_shows_an_arrow_and_survives_filtering(self):
        app = self.app
        app.show("E_calls")
        p = app.open_filter("StartUTC")
        self.assertEqual([b.cget("text") for b in p.sort_btns], ["Sort Oldest \u2192 Newest", "Sort Newest \u2192 Oldest"])
        p.sort(descending=True)
        self.assertIsNone(app.panel)
        self.assertIn(V.SORT_DOWN, app.table.heading("StartUTC", "text"))
        self.assertEqual(self.col("CallID"), [5, 4, 3, 2, 1])
        p = app.open_filter("Type")
        p.op_var.set("does not contain")
        p.a_var.set("miss")
        p.ok()
        self.assertEqual(self.col("CallID"), [5, 3, 2, 1])                # the sort order stays after filtering
        app.filter_var.set("Alice")
        self.assertEqual(self.col("CallID"), [2, 1])                      # and after a text search
        app.sort_by("CallID")
        self.assertIn(V.SORT_UP, app.table.heading("CallID", "text"))
        self.assertNotIn(V.SORT_DOWN, app.table.heading("StartUTC", "text"))   # one sorted column at a time
        self.assertIn("sorted by CallID", app.filter_lbl.cget("text"))

    def test_number_condition_and_invalid_input_is_shown_inline(self):
        app = self.app
        app.show("E_calls")
        p = app.open_filter("CallID")
        self.assertEqual(p.ctype, F.NUMBER)
        self.assertEqual(p.sort_btns[0].cget("text"), "Sort Smallest \u2192 Largest")
        p.op_var.set("greater than")
        p.a_var.set("abc")
        p.ok()
        self.assertIs(app.panel, p, "bad input keeps the panel open")
        self.assertIn("not a number", p.error_lbl.cget("text"))
        self.assertEqual(len(app.table.get_children()), 5)                 # nothing applied
        p.a_var.set("2")
        p.ok()
        self.assertIsNone(app.panel)
        self.assertEqual(self.col("CallID"), [3, 4, 5])
        self.assertIn("CallID greater than 2", app.filter_lbl.cget("text"))

    def test_date_panel_tree_between_and_blanks_kept_until_unticked(self):
        app = self.app
        v = app.g.views["E_calls"]
        v.rows.append({"CallID": 99, "StartUTC": None, "Type": "Missed"})     # an undated call
        app.show("E_calls")
        p = app.open_filter("StartUTC")
        self.assertEqual(p.ctype, F.DATE)
        self.assertEqual(list(p.op_combo.cget("values")), ["(none)"] + F.OPS[F.DATE])
        self.assertIn("y:2030", p.list.get_children())                        # Year > Month > Day
        self.assertEqual(p.list.get_children("y:2030"), ("m:2030-01",))
        self.assertEqual(p.list.get_children("m:2030-01"), ("v:2030-01-01",))
        self.assertIn("January", p.list.item("m:2030-01", "text"))
        self.assertEqual(p.list.get_children()[-1], "blanks")
        self.assertIn("(Blanks)", p.list.item("blanks", "text"))
        p.op_var.set("between")
        p.a_var.set("2030-01-01 13:00")
        p.b_var.set("2030-01-01 17:00")
        p.refresh_note()
        self.assertIn("(Blanks: 1 row kept)", p.blanks_chk.cget("text"))
        p.ok()
        self.assertEqual(self.col("CallID"), [2, 3, 99])                       # the undated call is kept
        self.assertIn("StartUTC between 2030-01-01 13:00 and 2030-01-01 17:00 (blanks kept)",
                      app.filter_lbl.cget("text"))
        p = app.open_filter("StartUTC")
        self.assertEqual(p.op_var.get(), "between")                            # the panel shows the filter in force
        self.assertEqual(p.a_var.get(), "2030-01-01 13:00")
        p.toggle_item("blanks")
        self.assertFalse(p.blanks_var.get())
        self.assertIn("removed", p.blanks_chk.cget("text"))
        p.ok()
        self.assertEqual(self.col("CallID"), [2, 3])

    def test_date_tree_parent_is_tri_state(self):
        app = self.app
        app.show("E_photos")
        p = app.open_filter("TakenUTC")
        self.assertEqual(p.list.get_children("m:2030-01"), ("v:2030-01-01", "v:2030-01-04"))
        p.toggle_item("v:2030-01-04")
        self.assertIn(V.PARTIAL, p.list.item("m:2030-01", "text"))
        self.assertIn(V.PARTIAL, p.list.item("y:2030", "text"))
        self.assertIn(V.PARTIAL, p.list.item("all", "text"))
        p.toggle_item("m:2030-01")                                            # a partial month ticks all its days
        self.assertIn(V.CHECKED, p.list.item("v:2030-01-04", "text"))
        p.toggle_item("y:2030")
        self.assertIn(V.UNCHECKED, p.list.item("v:2030-01-01", "text"))
        p.toggle_item("v:2030-01-01")
        p.ok()
        self.assertEqual(self.col("MediaID"), [1, 2])

    def test_presets_are_relative_to_the_newest_value_in_the_column(self):
        app = self.app
        app.show("E_photos")
        p = app.open_filter("TakenUTC")
        p.preset("Last 24 h of data")
        self.assertEqual(p.op_var.get(), "between")
        self.assertEqual((p.a_var.get(), p.b_var.get()), ("2030-01-03 23:20:00", "2030-01-04 23:20:00"))
        p.ok()
        self.assertEqual(self.col("MediaID"), [3])

    def test_filters_on_several_columns_combine_with_and(self):
        app = self.app
        app.show("E_calls")
        p = app.open_filter("Type")
        p.toggle_item("v:Outgoing")
        p.ok()
        p = app.open_filter("ContactName")
        values = [i for i in p.list.get_children() if i.startswith("v:")]
        self.assertNotIn("v:" + "", values)
        p.search_var.set("alice")
        self.assertIn("blanks", p.list.get_children())          # (Blanks) stays listed and ticked while searching
        p.ok()
        self.assertEqual(self.col("CallID"), [2, 4])               # call 4 has no contact name: a blank, kept
        p = app.open_filter("ContactName")
        p.toggle_item("blanks")
        p.ok()
        self.assertEqual(self.col("CallID"), [2])
        self.assertEqual(app.nav.set("E_calls", "rows"), "1 of 5")

    def test_cancel_changes_nothing(self):
        app = self.app
        app.show("E_contacts")
        p = app.open_filter("Name")
        p.toggle_item("all")
        p.cancel()
        self.assertIsNone(app.panel)
        self.assertEqual(len(app.table.get_children()), 6)
        self.assertNotIn(V.FILTER_MARK, app.table.heading("Name", "text"))

    def test_escape_cancels_and_enter_applies(self):
        app = self.make([("demo", self.src)], withdraw=False)
        app.show("E_contacts")
        p = app.open_filter("Name")
        p.toggle_item("v:Alice Example")
        app.update()
        p.search_entry.focus_force()
        app.update()
        p.search_entry.event_generate("<Escape>")
        app.update()
        self.assertIsNone(app.panel)
        self.assertEqual(len(app.table.get_children()), 6)
        p = app.open_filter("Name")
        p.toggle_item("v:Alice Example")
        p.search_entry.focus_force()
        app.update()
        p.search_entry.event_generate("<Return>")
        app.update()
        self.assertIsNone(app.panel)
        self.assertEqual(len(app.table.get_children()), 5)

    def test_value_list_is_capped_at_10000_with_a_note_and_search_covers_all(self):
        app = self.app
        rows = [{"Val": f"v{i:05d}"} for i in range(F.SHOWN_CAP + 50)]
        app.g.add_view(V.R.View("T_big", "big synthetic table", "table", "synthetic", ["Val"], rows))
        app.show("T_big")
        p = app.open_filter("Val")
        values = [i for i in p.list.get_children() if i.startswith("v:")]
        self.assertEqual(len(values), F.SHOWN_CAP)
        self.assertIn("10,000", p.cap_lbl.cget("text"))
        p.search_var.set("v10049")
        self.assertEqual([i for i in p.list.get_children() if i.startswith("v:")], ["v:v10049"])
        p.ok()
        self.assertEqual(self.col("Val"), ["v10049"])

    # ---- persistence
    def test_filters_and_sort_are_kept_per_view_and_after_a_rebuild(self):
        app = self.app
        app.show("E_calls")
        app.open_filter("DurationSec").sort(descending=False)
        p = app.open_filter("Type")
        p.toggle_item("v:Missed")
        p.ok()
        want = self.col("CallID")
        self.assertEqual(want, [5, 2, 3, 1])
        app.show("E_contacts")
        self.assertEqual(len(app.table.get_children()), 6)              # filters belong to their own view
        app.show("E_calls")
        self.assertEqual(self.col("CallID"), want)
        self.assertIn(V.SORT_UP, app.table.heading("DurationSec", "text"))
        self.assertIn(V.FILTER_MARK, app.table.heading("Type", "text"))
        app.window_min.set("17")
        app.rebuild()                                                   # new graph, same filters and sort
        self.assertEqual(self.col("CallID"), want)
        self.assertEqual(app.nav.set("E_calls", "rows"), "4 of 5")

    def test_clear_all_filters(self):
        app = self.app
        app.show("E_contacts")
        p = app.open_filter("Name")
        p.toggle_item("v:Alice Example")
        p.ok()
        app.show("E_calls")
        p = app.open_filter("Type")
        p.toggle_item("v:Missed")
        p.ok()
        app.range_from.set("2030-01-01 13:00")
        app.apply_range()
        app.filter_var.set("Bob")
        self.assertEqual(len(app.table.get_children()), 1)
        app.clear_btn.invoke()
        self.assertEqual(len(app.table.get_children()), 5)
        self.assertEqual(app.filter_var.get(), "")
        self.assertEqual(app.range_from.get(), "")
        self.assertEqual(app.nav.set("E_contacts", "rows"), "6")
        self.assertIn("No filters", app.filter_lbl.cget("text"))

    def test_jump_to_a_row_hidden_by_filters_clears_that_views_filters(self):
        app = self.app
        app.show_cross.set(True)
        app.show("E_contacts")
        p = app.open_filter("Name")
        p.toggle_item("v:Alice Example")
        p.ok()
        idx = next(i for i, r in enumerate(app.g.views["E_calls"].rows) if r["CallID"] == 1)
        app.show("E_calls", select_index=idx)
        app.on_row()
        target = next(iid for iid, t in app.link_targets.items() if t == ("contact", 1))
        app.links.selection_set(target)
        app.on_jump()
        self.assertEqual(app.cur_vid, "E_contacts")
        sel = app.table.selection()
        self.assertTrue(sel)
        self.assertEqual(app.g.views["E_contacts"].rows[int(sel[0])]["Name"], "Alice Example")
        self.assertIn("cleared", app.filter_lbl.cget("text"))

    # ---- case-wide date range
    def test_case_wide_date_range_applies_to_every_views_time_column(self):
        app = self.app
        app.g.views["E_calls"].rows.append({"CallID": 99, "StartUTC": "", "Type": "Missed"})
        app.show("E_calls")
        self.assertEqual(app.main_time_col("E_calls"), "StartUTC")
        self.assertIsNone(app.main_time_col("E_sims"))
        app.range_from.set("2030-01-01 13:00")
        app.range_to.set("2030-01-01 17:00")
        app.apply_range()
        self.assertEqual(self.col("CallID"), [2, 3, 99])                # undated kept
        self.assertEqual(app.nav.set("E_calls", "rows"), "3 of 6")
        self.assertEqual(app.nav.set("E_sms", "rows"), "0 of 3")
        self.assertEqual(app.nav.set("E_sims", "rows"), "1")            # no time column: untouched
        self.assertIn("between 2030-01-01 13:00 and 2030-01-01 17:00", app.filter_lbl.cget("text"))
        app.range_keep.set(False)
        app.apply_range()
        self.assertEqual(self.col("CallID"), [2, 3])
        p = app.open_filter("CallID")                                   # column filters apply on top
        p.op_var.set("less than")
        p.a_var.set("3")
        p.ok()
        self.assertEqual(self.col("CallID"), [2])
        app.clear_range()
        self.assertEqual(self.col("CallID"), [1, 2])

    def test_case_wide_range_picker_and_bad_input(self):
        app = self.app
        app.show("E_photos")
        self.assertEqual(list(app.range_col_combo.cget("values")), ["TakenUTC", "SyncedUTC"])
        self.assertEqual(app.range_col_combo.get(), "TakenUTC")
        app.range_to.set("2030-01-01 12:30")
        app.apply_range()
        self.assertEqual(self.col("MediaID"), [1])
        app.range_col_combo.set("SyncedUTC")
        app.on_range_col()
        self.assertEqual(self.col("MediaID"), [1, 2, 3])
        self.assertEqual(app.main_time_col("E_photos"), "SyncedUTC")
        app.range_from.set("31/12/2030")
        app.apply_range()
        self.assertIn("not a date", app.range_err.cget("text"))
        self.assertEqual(self.col("MediaID"), [1, 2, 3])                 # the old range still applies, not a half one

    def test_case_wide_presets_are_relative_to_the_newest_time_in_the_case(self):
        app = self.app
        app.range_preset("Last 24 h of data")
        newest = max(F.parse_time(r.get(app.main_time_col(vid)))
                     for vid, v in app.g.views.items() if app.main_time_col(vid)
                     for r in v.rows if F.parse_time(r.get(app.main_time_col(vid))))
        self.assertEqual(app.range_to.get(), newest.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertTrue(app.range_from.get())

    # ---- counts
    def test_nav_count_text(self):
        R = V.R
        plain = R.View("X", "x", "table", "", ["a"], [{}] * 686)
        self.assertEqual(V.nav_count_text(plain, 686), "686")
        self.assertEqual(V.nav_count_text(plain, 112), "112 of 686")
        check = R.View("C", "c", "check", "", ["a"], [{}] * 28)
        check.problems = 2
        self.assertEqual(V.nav_count_text(check, 28), "2 issues / 28")
        self.assertEqual(V.nav_count_text(check, 5), "2 issues / 5 of 28")
        check.problems = 1
        self.assertEqual(V.nav_count_text(check, 28), "1 issue / 28")
        check.problems = 0
        self.assertEqual(V.nav_count_text(check, 28), "0 issues / 28")
        rel = R.View("L", "l", "in-db", "", ["a"], [{}] * 91)
        rel.matched = 69
        self.assertEqual(V.nav_count_text(rel, 91), "69 of 91 linked")
        self.assertEqual(V.nav_count_text(rel, 40), "69 of 91 linked (40 shown)")
        rel.matched = None
        self.assertEqual(V.nav_count_text(rel, 91), "91")

    def test_contents_shows_issues_for_checks(self):
        app = self.app
        app.g.views["NR4"].problems = 2
        app.fill_nav()
        self.assertEqual(app.nav.set("NR4", "rows"), f"2 issues / {len(app.g.views['NR4'].rows)}")

    # ---- export
    def test_export_writes_the_shown_rows_in_order_and_a_sidecar(self):
        app = self.app
        app.show("E_calls")
        app.open_filter("StartUTC").sort(descending=True)
        p = app.open_filter("Type")
        p.toggle_item("v:Missed")
        p.ok()
        out = os.path.join(self.tmp.name, "calls.csv")
        with mock.patch.object(V.filedialog, "asksaveasfilename", return_value=out):
            app.export()
        with open(out, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        v = app.g.views["E_calls"]
        self.assertEqual(rows[0], v.cols)
        self.assertEqual([r[0] for r in rows[1:]], ["5", "3", "2", "1"])
        self.assertFalse([r for r in rows if r and r[0].startswith("#")])     # no comment lines inside the CSV
        with open(out + ".filter.txt", encoding="utf-8-sig") as f:
            side = f.read()
        self.assertIn("E_calls", side)
        self.assertIn("calls.csv", side)
        self.assertIn("4 of 5", side)
        self.assertIn("StartUTC, Newest \u2192 Oldest", side)
        self.assertIn("Type is one of", side)
        self.assertNotIn(self.tmp.name, side)                                  # no local paths
        self.assertIn(".filter.txt", app.status.cget("text"))

    # ---- display and toolbar
    def test_toolbar_labels_sit_left_of_their_own_controls(self):
        d2 = pldemo.build(os.path.join(self.tmp.name, "device2"))
        app = self.make([("one", self.src), ("two", d2)], withdraw=False)
        app.geometry("1400x700")
        app.update()
        pairs = [(app.dev_lbl, app.dev_combo), (app.nearby_lbl, app.nearby_spin),
                 (app.from_lbl, app.from_entry), (app.to_lbl, app.to_entry), (app.on_lbl, app.range_col_combo)]
        controls = [c for _, c in pairs]
        for lbl, ctl in pairs:
            self.assertTrue(lbl.winfo_ismapped() and ctl.winfo_ismapped(), lbl.cget("text"))
            self.assertLess(lbl.winfo_x(), ctl.winfo_x(), lbl.cget("text"))
            if lbl.master is not ctl.master:
                continue
            gap = ctl.winfo_x() - (lbl.winfo_x() + lbl.winfo_width())
            self.assertGreaterEqual(gap, 0, lbl.cget("text"))
            for other in controls:
                if other is ctl or other.master is not lbl.master:
                    continue
                d = min(abs(other.winfo_x() - (lbl.winfo_x() + lbl.winfo_width())),
                        abs(lbl.winfo_x() - (other.winfo_x() + other.winfo_width())))
                self.assertLess(gap, d, f"{lbl.cget('text')} is closer to another control")

    def test_hover_hints(self):
        app = self.app
        hint = app.hints[app.nearby_spin]
        for words in ("within", "minutes", "1-1440", "default 5"):
            self.assertIn(words, hint)
        hint = app.hints[app.cross_chk]
        for words in ("last 10 digits", "package names", "time", "timeline", "People"):
            self.assertIn(words, hint)

    def test_invisible_characters_are_shown_but_raw_values_are_kept(self):
        app = self.app
        v = app.g.views["E_contacts"]
        idx = next(i for i, r in enumerate(v.rows) if r["Name"] == "Frank Example")
        v.rows[idx]["Name"] = "\u200e"
        app.show("E_contacts", select_index=idx)
        app.on_row()
        self.assertEqual(app.table.set(str(idx), "Name"), "\u27e8U+200E\u27e9")
        self.assertIn("\u27e8U+200E\u27e9", app.detail.get("1.0", "end"))
        p = app.open_filter("Name")
        self.assertIn("\u27e8U+200E\u27e9", p.list.item("v:\u200e", "text"))
        p.cancel()
        out = os.path.join(self.tmp.name, "inv.csv")
        with mock.patch.object(V.filedialog, "asksaveasfilename", return_value=out):
            app.export()
        with open(out, encoding="utf-8-sig") as f:
            names = [r[1] for r in csv.reader(f)]
        self.assertIn("\u200e", names)

    def test_clearer_view_names(self):
        app = self.app
        self.assertEqual(app.nav.item("N_notifications", "text"), "Notifications (live and recovered)")
        self.assertEqual(V.NAV["R25"][2], "Check: sync counters (content_sequence)")
        self.assertEqual(V.NAV["C_wpd"][2], "Portable devices (MTP: phones, cameras, drives)")
        self.assertEqual(V.NAV["C_install"][2], "First driver install per device")

    def test_typing_in_the_filter_box_is_debounced(self):
        app = self.make([("demo", self.src)], withdraw=False)
        app.show("E_calls")
        calls = []
        real = app.populate
        app.populate = lambda *a, **k: (calls.append(1), real(*a, **k))
        self.assertTrue(app.filter_entry.bind("<Key>"), "the box marks keystrokes")
        for ch in "Alic":
            # a real keystroke: the box's <Key> binding runs, then Tk's Entry binding inserts the character.
            # Done here in that order (whether a generated key event inserts depends on window focus).
            app._key_typed()
            app.filter_entry.insert("end", ch)
        app.update_idletasks()
        app.update()
        self.assertEqual(app.filter_var.get(), "Alic")
        self.assertEqual(calls, [], "no refill while typing")
        import time
        time.sleep(0.4)
        app.update()
        self.assertEqual(calls, [1], "one refill after the pause")
        self.assertEqual(len(app.table.get_children()), 2)

    def test_keyboard_shortcuts(self):
        app = self.make([("demo", self.src)], withdraw=False)
        app.show("E_calls", select_index=0)
        app.update()

        def key(widget, seq):
            widget.focus_force()
            app.update()
            widget.event_generate(seq)
            app.update()
        key(app.table, "<Control-f>")
        self.assertIs(app.focus_get(), app.filter_entry)
        with mock.patch.object(app, "export") as ex:
            key(app.table, "<Control-e>")
        self.assertEqual(ex.call_count, 1)
        app.nav.focus_force()
        app.update()
        seen = []
        for _ in range(4):
            app.event_generate("<F6>")
            app.update()
            seen.append(app.focus_get())
        self.assertEqual(seen, [app.table, app.links, app.detail, app.nav])
        self.assertTrue(int(str(app.detail.cget("takefocus")) or 0))
        app.focus_col["E_calls"] = "Type"
        key(app.table, "<Alt-Down>")
        self.assertIsNotNone(app.panel)
        self.assertEqual(app.panel.col, "Type")
        app.panel.toggle_item("v:Missed")
        app.panel.ok()
        self.assertEqual(len(app.table.get_children()), 4)
        key(app.table, "<Control-Shift-F>")
        self.assertEqual(len(app.table.get_children()), 5)


class RebuildKeepsPackageCopyTest(unittest.TestCase):
    def test_nearby_change_does_not_copy_the_localcache_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pldemo.build_package(tmp)
            app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root),
                           connections=V.C.demo_source())
            app.withdraw()
            app.update()
            try:
                pc, work = app.pc, app.pc.work
                phones = [r.get("HardwareID") for r in app.g.views["PC_phones"].rows]
                with mock.patch.object(V.D, "PackageCopy", side_effect=AssertionError("copied again")):
                    app.window_min.set("23")
                    app.rebuild()
                self.assertEqual(app.built_min, 23)
                self.assertIs(app.pc, pc)
                self.assertTrue(os.path.isdir(work))
                self.assertEqual([r.get("HardwareID") for r in app.g.views["PC_phones"].rows], phones)
                self.assertIn("C_usb", app.g.views)
            finally:
                app.on_close(warn=False)


# ==================================================================== theme: dark mode, contrast, DPI, labels
import json  # noqa: E402
import pltheme as T  # noqa: E402

COLOUR_OPTS = ("background", "foreground", "activebackground", "activeforeground", "selectcolor",
               "highlightbackground", "highlightcolor", "insertbackground", "selectbackground", "selectforeground",
               "disabledforeground", "disabledbackground", "readonlybackground", "buttonbackground", "troughcolor",
               "inactiveselectbackground")
TTK_CLASSES = {"TFrame", "Treeview", "TScrollbar", "TCombobox", "TSeparator", "TLabel", "TButton", "TEntry",
               "TCheckbutton", "TPanedwindow", "TNotebook", "TSpinbox", "TProgressbar", "TScale", "TLabelframe",
               "TMenubutton", "TRadiobutton", "TSizegrip"}


class TkPath:
    """A widget by its Tk path: also the ones Tk makes itself (a Combobox drop-down), which Python does not list."""

    def __init__(self, app, path):
        self.app, self.path = app, str(path)

    def __str__(self):
        return self.path

    def __eq__(self, other):
        return str(other) == self.path

    def __hash__(self):
        return hash(self.path)

    def winfo_class(self):
        return str(self.app.tk.call("winfo", "class", self.path))

    def keys(self):
        opts = (self.app.tk.splitlist(o) for o in self.app.tk.splitlist(self.app.tk.call(self.path, "configure")))
        return [str(o[0])[1:] for o in opts if len(o) == 5]

    def cget(self, opt):
        return self.app.tk.call(self.path, "cget", "-" + opt)


def walk(w, app=None):
    app = app or w.winfo_toplevel()
    yield TkPath(app, w)
    for c in app.tk.splitlist(app.tk.call("winfo", "children", str(w))):
        yield from walk(c, app)


def norm(app, colour):
    """'#rrggbb' for any Tk colour name, so 'white' and '#ffffff' compare equal."""
    r, g, b = app.winfo_rgb(colour)
    return f"#{r >> 8:02x}{g >> 8:02x}{b >> 8:02x}"


class ThemePaletteTest(unittest.TestCase):
    """Pure checks of the two palettes: no Tk needed."""

    def test_contrast_formula(self):
        self.assertAlmostEqual(T.contrast("#000000", "#ffffff"), 21.0, places=2)
        self.assertAlmostEqual(T.contrast("#ffffff", "#ffffff"), 1.0, places=2)
        self.assertAlmostEqual(T.contrast("#e08a00", "#ffffff"), 2.69, places=2)   # the old inferred orange

    def test_both_palettes_have_the_same_roles(self):
        self.assertEqual(set(T.PALETTES), {"light", "dark"})
        self.assertEqual(set(T.PALETTES["light"]), set(T.PALETTES["dark"]))
        for name, pal in T.PALETTES.items():
            for role, c in pal.items():
                self.assertRegex(c, r"^#[0-9a-f]{6}$", f"{name}.{role}")
        for role in ("bg", "field", "panel", "panel_fg", "ink", "muted", "faint", "accent", "on_accent", "select_bg",
                     "select_fg", "heading_bg", "warn", "status", "deleted", "on_kind"):
            self.assertIn(role, T.PALETTES["light"], role)
        for k in V.KIND:
            self.assertIn("kind." + k, T.PALETTES["light"], k)

    def test_every_text_pair_meets_wcag_aa(self):
        self.assertTrue(T.TEXT_PAIRS)
        for k in V.KIND:      # every kind colour is drawn as text on the page and in tables, and under badge text
            for pair in ((f"kind.{k}", "bg"), (f"kind.{k}", "field"), ("on_kind", f"kind.{k}")):
                self.assertIn(pair, T.TEXT_PAIRS)
        for name in T.PALETTES:
            bad = T.failing_pairs(name)
            self.assertEqual(bad, [], f"{name}: pairs under {T.AA}:1 - " +
                             ", ".join(f"{f} on {b} {r:.2f}" for f, b, r in bad))

    def test_meanings_stay_distinguishable(self):
        for name, pal in T.PALETTES.items():
            hues = {k: T.hue(pal["kind." + k]) for k in ("check", "cross", "in-table")}
            self.assertTrue(hues["check"] >= 345 or hues["check"] <= 15, f"{name}: check is not red ({hues})")
            for a, b in (("check", "cross"), ("check", "in-table"), ("cross", "in-table")):
                d = abs(hues[a] - hues[b])
                self.assertGreaterEqual(min(d, 360 - d), 20, f"{name}: {a} and {b} look alike")


class ThemeSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"APPDATA": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_round_trip_in_appdata(self):
        path = T.settings_path()
        self.assertEqual(path, os.path.join(self.tmp.name, "phone-link-forensics", "viewer.json"))
        self.assertIsNone(T.saved_mode())                 # nothing saved yet
        self.assertTrue(T.save_mode("dark"))
        self.assertEqual(T.saved_mode(), "dark")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["theme"], "dark")
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n != "viewer.json"], [],
                         "the atomic write leaves no temporary file")
        T.save_settings({"theme": "light", "other": 1})
        self.assertTrue(T.save_mode("system"))
        self.assertEqual(T.load_settings(), {"theme": "system", "other": 1})   # other keys are kept

    def test_corrupt_or_odd_file_falls_back(self):
        path = T.settings_path()
        os.makedirs(os.path.dirname(path))
        for text in ("{not json", "[1, 2]", '{"theme": "purple"}', "\x00\xff"):
            with open(path, "w", encoding="latin-1") as f:
                f.write(text)
            self.assertIsNone(T.saved_mode(), text)
        self.assertTrue(T.save_mode("light"))            # a corrupt file is replaced
        self.assertEqual(T.saved_mode(), "light")

    def test_unwritable_place_is_not_an_error(self):
        blocker = os.path.join(self.tmp.name, "phone-link-forensics")
        with open(blocker, "w") as f:                    # a FILE where the folder should be
            f.write("x")
        self.assertFalse(T.save_mode("dark"))
        self.assertIsNone(T.saved_mode())


class ThemeWindowsReaderTest(unittest.TestCase):
    """Follow Windows: AppsUseLightTheme through an injected reader - the real registry is never read here."""

    def test_reader_values(self):
        with mock.patch.object(T, "read_apps_use_light_theme", side_effect=AssertionError("real registry read")):
            self.assertEqual(T.windows_theme(lambda: 0), "dark")
            self.assertEqual(T.windows_theme(lambda: 1), "light")
            self.assertEqual(T.windows_theme(lambda: None), "light")       # value missing
            self.assertEqual(T.windows_theme(mock.Mock(side_effect=OSError)), "light")
            self.assertEqual(T.resolve("system", lambda: 0), "dark")
            self.assertEqual(T.resolve("dark", lambda: 1), "dark")
            self.assertEqual(T.resolve("light", lambda: 0), "light")
            self.assertEqual(T.next_mode("light"), "dark")
            self.assertEqual(T.next_mode("dark"), "system")
            self.assertEqual(T.next_mode("system"), "light")


class TitleBarTest(unittest.TestCase):
    class Win:
        def winfo_id(self):
            return 4242

    def api(self, results=(0,)):
        api = mock.Mock()
        api.GetAncestor.return_value = 777
        api.DwmSetWindowAttribute.side_effect = list(results)
        return api

    @staticmethod
    def calls(api):
        import ctypes
        out = []
        for c in api.DwmSetWindowAttribute.call_args_list:
            hwnd, attr, ref, size = c.args
            value = ref._obj.value if hasattr(ref, "_obj") else ctypes.cast(ref, ctypes.POINTER(ctypes.c_int))[0]
            out.append((hwnd, attr, value, size))
        return out

    def test_dark_and_light(self):
        api = self.api()
        self.assertTrue(T.set_title_bar(self.Win(), True, api=api, os_name="nt"))
        api.GetAncestor.assert_called_once_with(4242, 2)
        self.assertEqual(self.calls(api), [(777, 20, 1, 4)])
        self.assertEqual(api.SetWindowPos.call_count, 1)
        flags = api.SetWindowPos.call_args.args[-1]
        self.assertEqual(flags, 0x20 | 0x2 | 0x1 | 0x4)    # FRAMECHANGED | NOMOVE | NOSIZE | NOZORDER
        api = self.api()
        self.assertTrue(T.set_title_bar(self.Win(), False, api=api, os_name="nt"))
        self.assertEqual(self.calls(api), [(777, 20, 0, 4)])

    def test_old_windows_falls_back_to_19_and_errors_are_ignored(self):
        api = self.api(results=(-2147024809, 0))            # E_INVALIDARG for 20, then 19 works
        self.assertTrue(T.set_title_bar(self.Win(), True, api=api, os_name="nt"))
        self.assertEqual([c[1] for c in self.calls(api)], [20, 19])
        api = self.api()
        api.DwmSetWindowAttribute.side_effect = OSError("no dwmapi")
        self.assertFalse(T.set_title_bar(self.Win(), True, api=api, os_name="nt"))
        api = self.api()
        api.GetAncestor.side_effect = AttributeError
        self.assertFalse(T.set_title_bar(self.Win(), True, api=api, os_name="nt"))

    def test_nothing_off_windows(self):
        api = self.api()
        self.assertFalse(T.set_title_bar(self.Win(), True, api=api, os_name="posix"))
        self.assertEqual(api.mock_calls, [])

    def test_dpi_awareness_is_system_aware_with_fallback(self):
        # system DPI awareness (1), as IDLE uses: Tk does not handle WM_DPICHANGED, so per-monitor awareness (2)
        # would leave fonts and row heights at the first monitor's size on a monitor with another scaling
        api = mock.Mock()
        self.assertEqual(T.dpi_aware(api=api, os_name="nt"), 1)
        api.shcore.SetProcessDpiAwareness.assert_called_once_with(1)
        api = mock.Mock()
        api.shcore.SetProcessDpiAwareness.side_effect = OSError
        self.assertEqual(T.dpi_aware(api=api, os_name="nt"), 1)
        api.user32.SetProcessDPIAware.assert_called_once_with()
        api = mock.Mock()
        api.shcore.SetProcessDpiAwareness.side_effect = AttributeError
        api.user32.SetProcessDPIAware.side_effect = AttributeError
        self.assertIsNone(T.dpi_aware(api=api, os_name="nt"))
        self.assertIsNone(T.dpi_aware(api=mock.Mock(), os_name="posix"))


class ThemeViewerTest(unittest.TestCase):
    """The viewer on the synthetic demo: every widget follows the palette, toggles, persistence, rows, labels."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = pldemo.build(os.path.join(self.tmp.name, "device"))
        self.apps = []
        self.bars = []
        self.app = self.make()

    def make(self, withdraw=True, **kw):
        kw.setdefault("title_bar", lambda w, dark: self.bars.append(dark))
        kw.setdefault("theme_reader", mock.Mock(side_effect=AssertionError("real registry read")))
        app = V.Viewer([("demo", self.src)], **kw)
        if withdraw:
            app.withdraw()
        app.update()
        self.apps.append(app)
        return app

    def tearDown(self):
        for a in self.apps:
            try:
                a.on_close(warn=False)
            except Exception:
                pass
        self.tmp.cleanup()

    def open_everything(self, app):
        """The filter panel of a date column (with its quick-range buttons) and every Combobox drop-down."""
        app.show("E_calls")
        date_col = next(c for c in app.g.views["E_calls"].cols if app.col_type(c) == V.F.DATE)
        panel = app.open_filter(date_col)
        self.assertIsNotNone(panel)
        self.assertTrue(panel.preset_btns)
        for w in walk(app):
            if w.winfo_class() == "TCombobox":
                app.tk.call("ttk::combobox::PopdownWindow", w)      # creates the drop-down list
        app.update()
        return panel

    def classic(self, app):
        return [w for w in walk(app) if w.winfo_class() not in TTK_CLASSES]

    def assert_palette(self, app, name):
        pal = {c.lower() for c in T.PALETTES[name].values()}
        seen = set()
        for w in self.classic(app):
            keys = set(w.keys())
            for opt in COLOUR_OPTS:
                if opt in keys:
                    val = str(w.cget(opt))
                    if val == "":
                        continue
                    self.assertIn(norm(app, val), pal, f"{name}: {w} ({w.winfo_class()}) {opt}={val}")
                    seen.add(w.winfo_class())
            if w.winfo_class() in ("Entry", "Spinbox", "Text", "Listbox"):
                self.assertEqual(norm(app, w.cget("selectbackground")), T.PALETTES[name]["select_bg"], str(w))
                self.assertEqual(norm(app, w.cget("selectforeground")), T.PALETTES[name]["select_fg"], str(w))
                if w.winfo_class() != "Listbox":
                    self.assertEqual(norm(app, w.cget("insertbackground")), T.PALETTES[name]["ink"], str(w))
        for cls in ("Tk", "Frame", "Label", "Button", "Checkbutton", "Entry", "Spinbox", "Text", "Panedwindow",
                    "Listbox"):
            self.assertIn(cls, seen, f"no {cls} was checked - the walk missed part of the window")

    # 4
    def test_dark_mode_reaches_every_classic_widget_and_back(self):
        app = self.app
        self.open_everything(app)
        self.assertIn(app.rangebar, list(walk(app)))          # the second toolbar row is part of the walk
        app.apply_theme("dark")
        app.update()
        self.assert_palette(app, "dark")
        light_bgs = {"#ffffff", "#f4f7fa"}
        for w in self.classic(app):
            if "background" in w.keys():
                self.assertNotIn(norm(app, w.cget("background")), light_bgs, str(w))
        app.apply_theme("light")
        app.update()
        self.assert_palette(app, "light")
        self.assertEqual(norm(app, app.cget("background")), T.PALETTES["light"]["bg"])

    def test_a_panel_opened_in_dark_mode_is_dark(self):
        app = self.app
        app.apply_theme("dark")
        self.open_everything(app)
        self.assert_palette(app, "dark")

    # 5
    def test_ttk_styles_and_tags_follow_the_palette(self):
        app = self.app
        for name in ("dark", "light"):
            app.apply_theme(name)
            pal = T.PALETTES[name]
            st = V.ttk.Style(app)
            lk = lambda *a: norm(app, st.lookup(*a))
            self.assertEqual(lk("Treeview", "fieldbackground"), pal["field"])
            self.assertEqual(lk("Treeview", "background"), pal["field"])
            self.assertEqual(lk("Treeview", "foreground"), pal["ink"])
            self.assertEqual(lk("Treeview", "background", ["selected"]), pal["select_bg"])
            self.assertEqual(lk("Treeview", "foreground", ["selected"]), pal["select_fg"])
            self.assertEqual(lk("Treeview.Heading", "background"), pal["heading_bg"])
            self.assertEqual(lk("Treeview.Heading", "foreground"), pal["ink"])
            self.assertEqual(lk("TCombobox", "fieldbackground", ["readonly"]), pal["field"])
            self.assertEqual(lk("TCombobox", "foreground", ["readonly"]), pal["ink"])
            self.assertEqual(lk("TScrollbar", "troughcolor"), pal["trough"])
            tag = lambda tree, t: norm(app, tree.tag_configure(t, "foreground"))
            self.assertEqual(tag(app.nav, "group"), pal["ink"])
            self.assertEqual(tag(app.nav, "item"), pal["ink"])
            self.assertEqual(tag(app.nav, "sub"), pal["muted"])
            self.assertEqual(tag(app.nav, "inferred"), pal["kind.cross"])
            self.assertEqual(tag(app.nav, "empty"), pal["faint"])
            for k in V.KIND:
                self.assertEqual(tag(app.links, k), pal["kind." + k], k)
            self.assertEqual(norm(app, app.detail.tag_cget("k", "foreground")), pal["muted"])

    # 6
    def test_runtime_colours_follow_a_toggle(self):
        app = self.app
        app.show("R23")              # a check view: red badge and basis line
        app._status("something went wrong", warn=True)
        app.filter_var.set("x")
        for name in ("dark", "light", "dark"):
            app.apply_theme(name)
            pal = T.PALETTES[name]
            self.assertEqual(norm(app, app.kind_lbl.cget("background")), pal["kind.check"])
            self.assertEqual(norm(app, app.kind_lbl.cget("foreground")), pal["on_kind"])
            self.assertEqual(norm(app, app.basis_lbl.cget("foreground")), pal["kind.check"])
            self.assertEqual(norm(app, app.filter_lbl.cget("foreground")), pal["ink"])
            self.assertEqual(norm(app, app.filter_lbl.cget("background")), pal["bg"])
            self.assertEqual(norm(app, app.status.cget("foreground")), pal["warn"])
            self.assertEqual(norm(app, app.status.cget("background")), pal["panel"])
        app._status("fine")
        self.assertEqual(norm(app, app.status.cget("foreground")), T.PALETTES["dark"]["status"])
        app.show("E_calls")
        self.assertEqual(norm(app, app.kind_lbl.cget("background")), T.PALETTES["dark"]["kind.table"])

    def test_hint_tooltip_uses_the_palette(self):
        app = self.app
        app.apply_theme("dark")
        h = V.Hint(app.export_btn, "hello")
        h._show()
        try:
            lbl = h.tip.winfo_children()[0]
            self.assertEqual(norm(app, lbl.cget("background")), T.PALETTES["dark"]["tip_bg"])
            self.assertEqual(norm(app, lbl.cget("foreground")), T.PALETTES["dark"]["tip_fg"])
        finally:
            h._leave()

    def test_toolbar_control_shortcut_and_title_bar(self):
        app = self.make(withdraw=False, theme_reader=lambda: 1)      # Windows apps set to light
        self.assertEqual(app.theme_mode, "light")
        self.assertEqual(app.theme_combo.get(), "Theme: Light")
        self.assertIn(app.theme_combo, list(walk(app.export_btn.master)))       # on the toolbar
        app.theme_combo.set("Theme: Dark")
        app.theme_combo.event_generate("<<ComboboxSelected>>")
        app.update()
        self.assertEqual((app.theme_mode, app.theme_name), ("dark", "dark"))
        self.assertEqual(self.bars[-1], True)
        app.focus_force()
        app.update()
        app.event_generate("<Control-Shift-L>")      # dark -> follow Windows (the reader says light)
        app.update()
        self.assertEqual(app.theme_mode, "system")
        self.assertEqual(app.theme_combo.get(), "Theme: Follow Windows")
        self.assertEqual(self.bars[-1], False)
        app.event_generate("<Control-L>")           # Shift+L can arrive as an upper-case L
        app.update()
        self.assertEqual(app.theme_mode, "light")

    def test_follow_windows_polls_every_five_seconds(self):
        answers = [1]
        reader = mock.Mock(side_effect=lambda: answers[-1])
        app = self.make(theme="system", theme_reader=reader)
        self.assertEqual(app.theme_name, "light")
        self.assertIsNotNone(app._theme_job)
        self.assertEqual(V.THEME_POLL_MS, 5000)
        answers.append(0)            # the user switches Windows to dark
        app._poll_system()
        self.assertEqual(app.theme_name, "dark")
        self.assertEqual(norm(app, app.cget("background")), T.PALETTES["dark"]["bg"])
        app.apply_theme("light")     # leaving system mode stops the polling
        self.assertIsNone(app._theme_job)

    def test_choice_is_saved_only_when_asked(self):
        with tempfile.TemporaryDirectory() as appdata, mock.patch.dict(os.environ, {"APPDATA": appdata}):
            app = self.make()                       # tests and --demo: nothing is read or written
            app.set_theme_mode("dark")
            app.set_theme_mode("light")
            self.assertFalse(os.path.exists(os.path.join(appdata, "phone-link-forensics")))
            path = T.settings_path()
            app = self.make(settings_path=path, theme_reader=lambda: 1)
            app.set_theme_mode("dark")
            self.assertEqual(T.saved_mode(), "dark")
            app.set_theme_mode("system")
            self.assertEqual(T.saved_mode(), "system")

    def test_cli_theme_and_settings(self):
        with tempfile.TemporaryDirectory() as appdata, mock.patch.dict(os.environ, {"APPDATA": appdata}):
            T.save_mode("dark")
            self.assertEqual(V.startup_theme(None, demo=False, screenshot=None), ("dark", T.settings_path()))
            self.assertEqual(V.startup_theme("light", demo=False, screenshot=None), ("light", T.settings_path()))
            self.assertEqual(V.startup_theme(None, demo=True, screenshot=None), ("system", None))
            self.assertEqual(V.startup_theme("dark", demo=True, screenshot=None), ("dark", None))
            self.assertEqual(V.startup_theme(None, demo=False, screenshot="x.png"), ("system", None))
            os.remove(T.settings_path())
            self.assertEqual(V.startup_theme(None, demo=False, screenshot=None), ("system", T.settings_path()))
            with mock.patch.object(T, "load_settings", side_effect=AssertionError("read")):
                V.startup_theme("dark", demo=True, screenshot=None)
                V.startup_theme(None, demo=False, screenshot="x.png")

    # 10
    def test_row_height_fits_the_font_at_high_dpi(self):
        app = self.app
        st = V.ttk.Style(app)
        # Tk keeps the scaling per display, shared by every later window of the process: put it back afterwards
        old = float(app.tk.call("tk", "scaling"))
        try:
            self._row_heights_at(app, st)
        finally:
            app.tk.call("tk", "scaling", old)
            app.refit_fonts()

    def _row_heights_at(self, app, st):
        for display in (1.0, 1.5, 2.0):
            app.tk.call("tk", "scaling", display * 96 / 72)
            app.refit_fonts()
            for style, fonts in (("Treeview", ("PL.body",)), ("Nav.Treeview", ("PL.body", "PL.group", "PL.italic"))):
                rh = int(st.lookup(style, "rowheight"))
                for f in fonts:
                    line = V.tkfont.nametofont(f, root=app).metrics("linespace")
                    self.assertGreaterEqual(rh, line + 2, f"{style} at {display:.0%}: {rh} < {f} {line}")
            if display == 1.5:
                self.assertGreaterEqual(int(st.lookup("Treeview", "rowheight")), 25)
            if display == 2.0:
                self.assertGreaterEqual(int(st.lookup("Treeview", "rowheight")), 32)

    def test_named_fonts_replace_literal_tuples(self):
        app = self.app
        self.open_everything(app)
        names = set(V.tkfont.names(app))
        for f in ("PL.body", "PL.bold", "PL.italic", "PL.small", "PL.smallbold", "PL.group", "PL.title",
                  "PL.viewtitle"):
            self.assertIn(f, names)
        for w in self.classic(app):
            if "font" in w.keys():
                self.assertIn(str(w.cget("font")), names | {"TkDefaultFont", "TkTextFont", "TkFixedFont"}, str(w))
        with open(V.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotRegex(src, r"\(\"(Segoe UI|Cambria)\"", "font tuples belong in pltheme")
        self.assertNotRegex(src, r"#[0-9a-fA-F]{6}\b", "colours belong in pltheme")

    # 11
    def test_long_contents_label_is_ellipsized_with_a_tooltip(self):
        app = self.make(withdraw=False)       # a hidden list has no width yet: it keeps the names whole
        self.assertEqual(app.nav.item("N_notifications", "text"), "Notifications (live and recovered)")
        long = "Notifications (live and recovered) with an unusually long synthetic name for the test"
        with mock.patch.dict(V.NAV, {"N_notifications": ("notifications", None, long)}):
            app.fill_nav()
            app.nav.column("#0", width=180)
            app.fit_nav_labels()
            shown = app.nav.item("N_notifications", "text")
            self.assertTrue(shown.endswith("…"), shown)
            self.assertTrue(long.startswith(shown[:-1].rstrip()))
            font = V.tkfont.nametofont("PL.body", root=app)
            self.assertLessEqual(font.measure(shown), 180 - 2 * app.nav_indent())
            self.assertEqual(app.nav_full["N_notifications"], long)
            self.assertEqual(app.nav_tip_text("N_notifications"), long)
            self.assertIsNone(app.nav_tip_text("grp:calls"))          # short names need no tooltip
            tip = app.nav_tip.show("N_notifications")
            try:
                self.assertEqual(tip.winfo_children()[0].cget("text"), long)
            finally:
                app.nav_tip.hide()
            count = app.nav.set("N_notifications", "rows")             # the count stays whole
            self.assertTrue(count)
            self.assertGreaterEqual(int(app.nav.column("rows", "width")), font.measure(count))
            app.nav.column("#0", width=900)                             # wider again: the full name comes back
            app.fit_nav_labels()
            self.assertEqual(app.nav.item("N_notifications", "text"), long)
            app.nav.column("#0", width=180)
            app.fit_nav_labels()
            app.show("E_calls")
            app.nav.selection_set("N_notifications")
            app.on_nav()
            self.assertEqual(app.title_lbl.cget("text"), long)          # selected: the full name above the table

    def test_fit_text(self):
        m = lambda s: 10 * len(s)
        self.assertEqual(T.fit_text("short", m, 100), "short")
        self.assertEqual(T.fit_text("abcdefghij", m, 60), "abcde…")
        self.assertEqual(T.fit_text("abc def ghi", m, 60), "abc d…")
        self.assertEqual(T.fit_text("abc  def", m, 50), "abc…")      # no space before the ellipsis
        self.assertEqual(T.fit_text("abcdef", m, 5), "…")


class ViewerReview3Test(unittest.TestCase):
    """The third review of the window (synthetic demo): sizes, export, filters, keyboard, housekeeping."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = pldemo.build(os.path.join(self.tmp.name, "device"))
        self.apps = []
        self.app = self.make()

    def make(self, devices=None, withdraw=True, **kw):
        kw.setdefault("title_bar", lambda w, dark: True)
        kw.setdefault("theme_reader", lambda: 1)
        app = V.Viewer(devices or [("demo", self.src)], **kw)
        if withdraw:
            app.withdraw()
        app.update()
        self.apps.append(app)
        return app

    def tearDown(self):
        for a in self.apps:
            try:
                a.on_close(warn=False)
            except Exception:
                pass
        self.tmp.cleanup()

    def sized(self, w, h, **kw):
        app = self.make(withdraw=False, **kw)
        app.geometry(f"{w}x{h}+0+0")
        app.update()
        return app

    # ---- 1 the linked-rows pane
    def test_linked_rows_pane_shows_its_message_and_fits_its_columns(self):
        import tkinter.font as tkfont
        for w, h in ((1366, 740), (1600, 950)):
            app = self.sized(w, h)
            app.show("E_calls", select_index=0)      # call 1 has no linked row inside its database
            app.on_row()
            app.update()
            kids = app.links.get_children()
            self.assertEqual(len(kids), 1)
            vals = dict(zip(app.links["columns"], app.links.item(kids[0], "values")))
            self.assertIn("No other row is linked", vals["target"])     # the first column shown
            self.assertEqual(app.links["displaycolumns"][0], "target")
            shown = app.links["displaycolumns"]
            widths = [int(app.links.column(c, "width")) for c in shown]
            self.assertLessEqual(sum(widths), app.links.winfo_width(), (w, widths))
            bold = tkfont.nametofont("PL.bold", root=app)
            for c, width in zip(shown, widths):
                self.assertGreaterEqual(width, bold.measure(app.links.heading(c, "text")), (w, c))
            app.on_close(warn=False)

    # ---- 2 the status bar on smaller windows
    def test_status_bar_stays_visible_on_small_windows(self):
        for w, h in ((1366, 740), (1280, 680), (1000, 560)):
            app = self.sized(w, h)
            app._status("hash check text")
            app.update()
            self.assertTrue(app.status.winfo_ismapped(), (w, h))
            self.assertGreater(app.status.winfo_height(), 1)
            self.assertLessEqual(app.status.winfo_y() + app.status.winfo_height(), app.winfo_height(), (w, h))
            app.on_close(warn=False)

    # ---- 3 the filter panel's OK and Cancel inside the window
    def test_filter_panel_buttons_stay_inside_the_window(self):
        for w, h in ((1366, 740), (1280, 680), (1600, 950)):
            app = self.sized(w, h)
            app.show("E_calls")
            for col in ("StartUTC", "Type"):        # a date panel (the tallest) and a text panel
                p = app.open_filter(col)
                app.update()
                top = p.winfo_rooty() - app.winfo_rooty()
                self.assertGreaterEqual(top, 0, (w, h, col))
                for b in (p.ok_btn, p.cancel_btn):
                    bottom = b.winfo_rooty() - app.winfo_rooty() + b.winfo_height()
                    self.assertTrue(b.winfo_ismapped(), (w, h, col))
                    self.assertLessEqual(bottom, app.winfo_height(), (w, h, col))
                self.assertGreaterEqual(int(p.list.cget("height")), 3)      # the value list is still usable
            app.geometry(f"{w}x{h - 120}")                                  # a resize with the panel open
            app.update()
            bottom = p.ok_btn.winfo_rooty() - app.winfo_rooty() + p.ok_btn.winfo_height()
            self.assertLessEqual(bottom, app.winfo_height(), (w, h - 120))
            app.on_close(warn=False)

    # ---- 4 export with a text search still waiting for its timer
    def test_export_applies_a_pending_text_search_first(self):
        app = self.app
        app.show("E_calls")
        app._key_typed()                      # a keystroke: the refill waits for the debounce timer ...
        app.filter_var.set("Alice")
        self.assertIsNotNone(app._filter_job)
        self.assertEqual(len(app.cur_rows), 5)
        out = os.path.join(self.tmp.name, "race.csv")
        with mock.patch.object(V.filedialog, "asksaveasfilename", lambda **k: out):
            app.export()                      # ... Ctrl+E before it fires
        with open(out, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))[1:]
        self.assertEqual(len(rows), 2)
        with open(out + ".filter.txt", encoding="utf-8-sig") as f:
            side = f.read()
        self.assertIn("Rows exported: 2 of 5", side)
        self.assertIn("'Alice'", side)
        self.assertIsNone(app._filter_job)
        self.assertEqual(len(app.table.get_children()), 2)

    # ---- 5 reopening a filter and pressing OK
    def test_checklist_keeps_the_ticks_of_values_other_filters_hide(self):
        # the list shows only Incoming (another filter hides the rest); the old filter was Incoming only
        cl = F.Checklist([("Incoming", 1)], 0, ticked={"Incoming"}, all_keys={"Incoming", "Outgoing", "Missed"})
        self.assertEqual(cl.result(), (frozenset({"Incoming"}), True))
        cl = F.Checklist([("Incoming", 1)], 0, ticked={"Incoming", "Missed"},
                         all_keys={"Incoming", "Outgoing", "Missed"})
        cl.set("Incoming", False)
        self.assertEqual(cl.result(), (frozenset({"Missed"}), True))      # Missed was not in the list: kept
        cl = F.Checklist([("Incoming", 1)], 0, ticked=None, all_keys={"Incoming", "Outgoing"})
        cl.set("Incoming", False)
        self.assertEqual(cl.result(), (frozenset({"Outgoing"}), True))    # no old filter: hidden values stay ticked
        cl.set("Incoming", True)
        self.assertEqual(cl.result(), (None, True))

    def test_reopening_a_filter_and_pressing_ok_keeps_it(self):
        app = self.app
        app.show("E_calls")
        app.apply_column_filter("E_calls", "Type", F.ColumnFilter(F.TEXT, values={"Incoming"}))
        app.apply_column_filter("E_calls", "Number", F.ColumnFilter(F.TEXT, values={"+1-555-0102"}))
        self.assertEqual(len(app.cur_rows), 1)
        old = app.view_state("E_calls")["filters"]["Type"]
        p = app.open_filter("Type")
        self.assertEqual(p.checklist.keys, ["Incoming"])      # the value list shows what the Number filter leaves
        p.ok()
        f = app.view_state("E_calls")["filters"].get("Type")
        self.assertIs(f, old, "nothing changed: OK keeps the filter as it was")
        app.apply_column_filter("E_calls", "Number", None)
        self.assertEqual(sorted(app.g.views["E_calls"].rows[i]["Type"] for i in app.cur_rows), ["Incoming"] * 3)
        # a change to a listed value keeps the hidden values' ticks
        app.apply_column_filter("E_calls", "Type", F.ColumnFilter(F.TEXT, values={"Incoming", "Missed"}))
        app.apply_column_filter("E_calls", "Number", F.ColumnFilter(F.TEXT, values={"+1-555-0102"}))
        p = app.open_filter("Type")
        p.toggle_item("v:Incoming")
        p.ok()
        self.assertEqual(app.view_state("E_calls")["filters"]["Type"].values, frozenset({"Missed"}))

    # ---- 6 "blanks kept" in a value filter's text
    def test_value_filter_text_says_blanks_kept_when_the_column_has_blanks(self):
        f = F.ColumnFilter(F.TEXT, values={"Incoming"}, blanks=True, has_blanks=True)
        self.assertEqual(f.describe("T"), "T is one of 'Incoming' (blanks kept)")
        f = F.ColumnFilter(F.TEXT, values={"Incoming"}, blanks=False, has_blanks=True)
        self.assertEqual(f.describe("T"), "T is one of 'Incoming' (blanks removed)")
        self.assertEqual(F.ColumnFilter(F.TEXT, values={"Incoming"}).describe("T"), "T is one of 'Incoming'")
        app = self.app
        app.show("E_calls")                   # ContactName is blank for call 4
        p = app.open_filter("ContactName")
        p.toggle_item("v:Bob Example")
        p.ok()
        self.assertIn("(blanks kept)", app.filter_lbl.cget("text"))
        out = os.path.join(self.tmp.name, "b.csv")
        with mock.patch.object(V.filedialog, "asksaveasfilename", lambda **k: out):
            app.export()
        with open(out + ".filter.txt", encoding="utf-8-sig") as fh:
            self.assertIn("(blanks kept)", fh.read())

    # ---- 7 times without a zone are undated
    def test_zoneless_times_are_undated_for_sort_conditions_and_range(self):
        nz = "2030-01-01 13:30:00 (no zone)"
        self.assertIsNone(F.parse_time(nz))
        self.assertTrue(F.is_zoneless(nz))
        self.assertFalse(F.is_zoneless("2030-01-01 13:30:00"))
        self.assertEqual(F.detect_type([nz] * 3, "TimeUTC"), F.DATE)        # still a date column
        rows = [{"c": v} for v in (nz, "2030-01-02 00:00:00", None, "2029-01-01 00:00:00")]
        order = [rows[i]["c"] for i in F.sort_indices(range(4), rows, "c", F.DATE)]
        self.assertEqual(order, ["2029-01-01 00:00:00", "2030-01-02 00:00:00", nz, None])    # after the times with a zone
        self.assertIsNone(F.make_condition(F.DATE, "on", "2030-01-01").test(nz))
        self.assertEqual(F.value_key(nz, F.DATE), nz)                       # listed as it is, not under a day
        self.assertEqual(F.newest([nz, "2029-01-01"]).year, 2029)
        app = self.app
        app.g.add_view(V.R.View("X_nz", "zone-less synthetic", "table", "synthetic", ["ID", "TimeUTC"],
                                [{"ID": 1, "TimeUTC": "2030-01-01 13:00:00"}, {"ID": 2, "TimeUTC": nz},
                                 {"ID": 3, "TimeUTC": "2030-01-05 00:00:00"}]))
        app.fill_nav()
        app.show("X_nz")
        app.range_from.set("2030-01-01")
        app.range_to.set("2030-01-01")
        app.apply_range()
        ids = lambda: [app.g.views["X_nz"].rows[i]["ID"] for i in app.cur_rows]
        self.assertEqual(ids(), [1, 2])          # undated rows kept, as "Keep undated rows" says
        self.assertIn("no zone", app.filter_lbl.cget("text"))
        self.assertIn("undated", app.filter_lbl.cget("text"))
        app.range_keep.set(False)
        app.apply_range()
        self.assertEqual(ids(), [1])
        app.clear_range()
        app.set_sort("TimeUTC", False)
        self.assertEqual(ids(), [1, 3, 2])
        self.assertIn(nz, app.table.item("1", "values"))                   # the raw value stays visible
        self.assertIn("no zone", V.HINTS["range"])
        p = app.open_filter("TimeUTC")
        self.assertIn("no zone", p.zoneless_lbl.cget("text"))

    # ---- 8 the case-wide dates prefer a UTC column
    def test_case_wide_dates_prefer_a_utc_column(self):
        app = self.make(connections=V.C.demo_source())
        for vid in ("C_usb", "C_install", "C_wpd", "E_calls"):
            self.assertTrue(app.main_time_col(vid).endswith("UTC"), (vid, app.main_time_col(vid)))
        app.g.add_view(V.R.View("X_local", "local only", "table", "synthetic", ["ID", "SeenLocal"],
                                [{"ID": 1, "SeenLocal": "2030-01-01 10:00:00"}]))
        app.fill_nav()
        app.show("X_local")
        self.assertEqual(app.main_time_col("X_local"), "SeenLocal")      # no UTC column: the local one
        app.range_from.set("2030-01-01")
        app.apply_range()
        self.assertIn("SeenLocal, as logged (local time)", app.filter_lbl.cget("text"))
        self.assertIn("as logged (local time)", app.export_record(os.path.join(self.tmp.name, "x.csv")))
        app.show("C_usb")
        self.assertNotIn("as logged", app.filter_lbl.cget("text"))

    # ---- 9 the keyboard can pick any column to filter
    def test_ctrl_left_right_pick_the_column_for_the_keyboard_filter(self):
        app = self.sized(1366, 740)
        app.show("E_calls", select_index=0)
        cols = app.g.views["E_calls"].cols

        def key(widget, seq):
            widget.focus_force()
            app.update()
            widget.event_generate(seq)
            app.update()
        self.assertEqual(app.current_col(), cols[0])
        self.assertIn(f"column for Alt+Down: {cols[0]}", app.filter_lbl.cget("text"))
        key(app.table, "<Control-Right>")
        key(app.table, "<Control-Right>")
        self.assertEqual(app.current_col(), cols[2])
        self.assertIn(f"column for Alt+Down: {cols[2]}", app.filter_lbl.cget("text"))
        key(app.table, "<Control-Left>")
        self.assertEqual(app.current_col(), cols[1])
        for _ in range(len(cols) + 2):
            key(app.table, "<Control-Right>")
        self.assertEqual(app.current_col(), cols[-1])           # stops at the last column
        left, right = app.table.xview()
        self.assertEqual(right, 1.0)                             # scrolled into view
        key(app.table, "<Alt-Down>")
        self.assertEqual(app.panel.col, cols[-1])
        key(app.panel.list, "<Control-Left>")                   # in the panel, outside a text box: the next panel
        self.assertEqual(app.panel.col, cols[-2])
        key(app.panel.search_entry, "<Control-Left>")           # in a text box it moves by word, as usual
        self.assertEqual(app.panel.col, cols[-2])
        self.assertIn("Ctrl+Left", app.hints[app.filter_lbl])
        self.assertIn("Alt+Down", app.hints[app.filter_lbl])

    # ---- 10 a click in a text box while a panel is open
    def test_clicking_a_text_box_closes_the_panel_and_keeps_the_focus_there(self):
        app = self.sized(1366, 740)
        app.show("E_calls")
        app.focus_force()
        app.update()
        for target in (app.from_entry, app.filter_entry):
            app.open_filter("Type")
            app.update()
            target.event_generate("<Button-1>", x=5, y=5)
            target.event_generate("<ButtonRelease-1>", x=5, y=5)
            app.update()
            self.assertIsNone(app.panel)
            self.assertIs(app.focus_get(), target)
        p = app.open_filter("Type")             # Cancel / Esc still hand the focus back to the table
        p.cancel()
        app.update()
        self.assertIs(app.focus_get(), app.table)

    # ---- 11 Enter on a focused button, Tab inside the panel
    def test_enter_presses_the_focused_button_and_tab_stays_in_the_panel(self):
        app = self.sized(1366, 740)
        app.show("E_calls")

        def key(widget, seq):
            widget.focus_force()
            app.update()
            widget.event_generate(seq)
            app.update()
        p = app.open_filter("Type")
        p.toggle_item("v:Missed")
        key(p.cancel_btn, "<Return>")
        self.assertIsNone(app.panel)
        self.assertNotIn("Type", app.view_state("E_calls")["filters"], "Enter on Cancel cancels")
        p = app.open_filter("Type")
        p.toggle_item("v:Missed")
        key(p.sort_btns[1], "<Return>")
        self.assertIsNone(app.panel)
        self.assertEqual(app.view_state("E_calls")["sort"], ("Type", True))
        self.assertNotIn("Type", app.view_state("E_calls")["filters"], "Enter on a sort button sorts only")
        p = app.open_filter("Type")
        p.toggle_item("v:Missed")
        key(p.search_entry, "<Return>")                       # elsewhere Enter is OK
        self.assertEqual(len(app.cur_rows), 4)
        p = app.open_filter("Type")
        order = p.focus_order()
        self.assertEqual(order[0], p.sort_btns[0])
        self.assertEqual(order[-2:], [p.ok_btn, p.cancel_btn])
        self.assertNotIn(p.b_entry, order)                    # disabled: skipped
        key(p.cancel_btn, "<Tab>")
        self.assertIs(app.focus_get(), order[0])               # wraps inside the panel
        key(order[0], "<Shift-Tab>")
        self.assertIs(app.focus_get(), p.cancel_btn)
        key(p.search_entry, "<Tab>")
        self.assertIs(app.focus_get(), order[order.index(p.search_entry) + 1])
        self.assertIsNotNone(app.panel)
        key(p.ok_btn, "<Escape>")
        self.assertIsNone(app.panel)

    # ---- 12 large integers keep every digit
    def test_large_integers_are_compared_exactly(self):
        cells = [9007199254740993, 9007199254740992, "9007199254740993", "9,007,199,254,740,992"]
        cf = F.ColumnFilter(F.NUMBER, cond=F.make_condition(F.NUMBER, "equals", "9007199254740993"))
        self.assertEqual([c for c in cells if cf.match(c)], [9007199254740993, "9007199254740993"])
        self.assertNotEqual(F.parse_number(133000000000000001), F.parse_number(133000000000000002))   # FILETIMEs
        self.assertIsInstance(F.parse_number("12"), int)
        self.assertIsInstance(F.parse_number("1.5"), float)
        rows = [{"c": c} for c in (133000000000000002, "133000000000000001", 5.5)]
        self.assertEqual([rows[i]["c"] for i in F.sort_indices(range(3), rows, "c", F.NUMBER)],
                         [5.5, "133000000000000001", 133000000000000002])
        between = F.make_condition(F.NUMBER, "between", "9007199254740993", "9007199254740993")
        self.assertFalse(between.test(9007199254740992))
        with self.assertRaises(F.FilterError):
            F.make_condition(F.NUMBER, "between", "9007199254740993", "9007199254740992")

    # ---- 13 text that was cut off
    def test_description_lines_rewrap_on_resize_and_time_columns_fit_seconds(self):
        import tkinter.font as tkfont
        app = self.sized(1600, 950)
        app.show("E_calls")
        app.update()
        for w in (app.basis_lbl, app.filter_lbl):
            self.assertLessEqual(int(w.cget("wraplength")), w.winfo_width())
            self.assertGreater(int(w.cget("wraplength")), w.winfo_width() - app.px(40))
        app.geometry("1100x700")
        app.update()
        for w in (app.basis_lbl, app.filter_lbl):
            self.assertLessEqual(int(w.cget("wraplength")), w.winfo_width(), "rewrapped at the new width")
            self.assertGreater(int(w.cget("wraplength")), 50)
        body = tkfont.nametofont("PL.body", root=app)
        for vid in ("E_calls", "N_notifications", "E_photos"):
            app.show(vid)
            for c in app.date_cols(vid):
                need = max(body.measure(F.cell_text(r.get(c))[:19]) for r in app.g.views[vid].rows) + app.px(8)
                self.assertGreaterEqual(int(app.table.column(c, "width")), need, (vid, c))

    # ---- 14 one far-future date does not anchor the quick ranges
    def test_quick_ranges_ignore_implausible_future_dates(self):
        from datetime import datetime
        now = datetime(2026, 11, 13, 9, 0, 0)
        vals = ["2025-03-01 10:00:00", "9999-12-31 23:59:59", "2025-02-27 08:00:00", "2027-06-01 00:00:00"]
        self.assertEqual(F.preset_info(vals, "Last 7 days of data", now=now),
                         ("2025-02-22 10:00:00", "2025-03-01 10:00:00", 2))
        self.assertEqual(F.preset_range(vals, "Last 7 days of data", now=now)[1], "2025-03-01 10:00:00")
        self.assertEqual(F.preset_info(["2026-11-14 01:00:00"], "Last 24 h of data", now=now)[2], 0)   # within a day
        # every date later than tomorrow (a synthetic case, a phone clock set ahead): only far outliers are ignored
        self.assertEqual(F.preset_info(["2030-01-01 00:00:00", "9999-12-31 23:59:59"], "Last 7 days of data",
                                       now=now), ("2029-12-25 00:00:00", "2030-01-01 00:00:00", 1))
        self.assertEqual(F.preset_info(["2030-01-01 00:00:00", "2031-01-01 00:00:00"], "Last 7 days of data",
                                       now=now)[1:], ("2031-01-01 00:00:00", 0))
        app = self.app
        app.g.views["E_calls"].rows.append({"CallID": 98, "StartUTC": "9999-12-31 23:59:59", "Type": "Missed"})
        app._types = {}
        app.show("E_calls")
        app.range_preset("Last 7 days of data")
        self.assertTrue(app.range_to.get().startswith("2030-"), app.range_to.get())
        self.assertIn("ignoring 1 implausible future date", app.range_err.cget("text"))
        p = app.open_filter("StartUTC")
        p.preset("Last 24 h of data")
        self.assertTrue(p.b_var.get().startswith("2030-"))
        self.assertIn("ignoring 1 implausible future date", p.error_lbl.cget("text"))
        self.assertIn("implausible", V.HINTS["range"])

    # ---- 16 controls: 3:1 against what is next to them (WCAG 2.1, 1.4.11 non-text contrast)
    def test_scrollbars_and_text_box_borders_meet_3_to_1(self):
        self.assertEqual(T.UI_AA, 3.0)
        for pair in (("thumb", "trough"), ("thumb_active", "trough"), ("edge", "field"), ("edge", "bg"),
                     ("edge", "panel")):
            self.assertIn(pair, T.NON_TEXT_PAIRS)
        for name in T.PALETTES:
            bad = T.failing_ui_pairs(name)
            self.assertEqual(bad, [], f"{name}: " + ", ".join(f"{f} on {b} {r:.2f}" for f, b, r in bad))
        self.assertEqual(T.failing_pairs("light"), [])         # text stays at 4.5:1
        app = self.app
        for name in ("light", "dark"):
            app.apply_theme(name)
            app.update()
            for e in (app.filter_entry, app.from_entry, app.nearby_spin):
                self.assertEqual(int(e.cget("highlightthickness")), 1, str(e))     # the border is drawn ...
                self.assertEqual(norm(app, e.cget("highlightbackground")), T.PALETTES[name]["edge"])   # ... in edge
            st = V.ttk.Style(app)
            self.assertEqual(norm(app, st.lookup("TScrollbar", "background")), T.PALETTES[name]["thumb"])
            self.assertEqual(norm(app, st.lookup("TCombobox", "bordercolor")), T.PALETTES[name]["edge"])

    # ---- copies that could not be removed (plrelate.remove_copy / Evidence.left_behind)
    def test_a_copy_left_behind_by_a_failed_open_is_reported(self):
        app = self.app
        stuck = os.path.join(self.tmp.name, "plrelate-stuck000")
        os.makedirs(stuck)

        class Failing:
            def __init__(self, path):
                e = OSError("synthetic open failure")
                e.left_behind = stuck
                raise e
        with mock.patch.object(V.R, "Evidence", Failing), mock.patch.object(V.messagebox, "showerror") as err:
            self.assertFalse(app.load_device(0))
        self.assertIn(stuck, app.leftovers)
        msg = err.call_args[0][1]
        self.assertIn("could not be removed", msg)
        self.assertIn(stuck, msg)
        self.assertIn("could not be removed", app.status.cget("text"))
        with mock.patch.object(V.messagebox, "showwarning") as warn, mock.patch.object(V.Viewer, "destroy"):
            app.on_close()
        self.assertIn(stuck, warn.call_args[0][1])

    def test_an_old_copy_that_cannot_be_removed_is_shown_at_once_and_on_close(self):
        d2 = pldemo.build(os.path.join(self.tmp.name, "device2"))
        app = self.make([("one", self.src), ("two", d2)])
        old = app.ev.work
        with mock.patch.object(V.R, "remove_copy", side_effect=lambda p: p):    # Windows keeps a file open
            self.assertTrue(app.load_device(1))
        self.assertEqual(app.leftovers, [old])
        self.assertIn("could not be removed", app.status.cget("text"))
        self.assertIn(old, app.status.cget("text"))
        other = os.path.join(self.tmp.name, "plrelate-other000")      # one only plrelate knows about
        os.makedirs(other)
        V.R.LEFT_BEHIND.append(other)
        try:
            with mock.patch.object(V.messagebox, "showwarning") as warn, mock.patch.object(V.Viewer, "destroy"):
                app.on_close()
            text = warn.call_args[0][1]
            self.assertIn(old, text)
            self.assertIn(other, text)
        finally:
            V.R.LEFT_BEHIND.remove(other)
            V.R.remove_copy(old)


if __name__ == "__main__":
    unittest.main()
