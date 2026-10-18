import json
import os
import tempfile
import unittest
from unittest import mock

from queenbee.mother import board
from queenbee.mother.dispatch import Dispatch


class Messages(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.f = self.dir.name

    def tearDown(self):
        self.dir.cleanup()

    def write(self, *lines):
        os.makedirs(os.path.join(self.f, ".queenbee"), exist_ok=True)
        with open(board.board_path(self.f), "a", encoding="utf-8") as fh:
            for l in lines:
                fh.write(l if isinstance(l, str) else json.dumps(l))
                fh.write("\n")

    def test_clean_accepts_workers_queen_user_and_drops_the_rest(self):
        self.write({"from": "p3", "to": "all", "text": "x"}, {"from": "stranger", "to": "all", "text": "x"},
                   {"from": "W2", "to": ["queen", "W4", "rm -rf"], "text": " hi "}, {"from": "W2", "text": ""},
                   "not json", {"from": "queen", "to": "W1", "text": "y" * 2000})
        msgs, off = board.read_new(self.f, 0)
        self.assertEqual([(m["from"], m["to"]) for m in msgs],
                         [("W3", ["all"]), ("W2", ["queen", "W4"]), ("queen", ["W1"])])
        self.assertEqual(len(msgs[2]["text"]), board.MAX_TEXT)
        self.assertEqual(board.read_new(self.f, off), ([], off))

    def test_a_line_with_its_own_field_names_is_still_read(self):
        # Codex wrote {"job": "W1", "message": ...}, with a byte-order mark; the workers waiting for it never got it.
        os.makedirs(os.path.join(self.f, ".queenbee"), exist_ok=True)
        with open(board.board_path(self.f), "a", encoding="utf-8-sig") as fh:
            fh.write(json.dumps({"job": "W1", "message": "Note shape: dict with id, text, tags"}) + "\n")
        msgs, _ = board.read_new(self.f, 0)
        self.assertEqual(msgs, [{"from": "W1", "to": ["all"], "text": "Note shape: dict with id, text, tags"}])

    def test_a_half_written_last_line_waits(self):
        self.write({"from": "W1", "to": "all", "text": "a"})
        with open(board.board_path(self.f), "a", encoding="utf-8") as fh:
            fh.write('{"from": "W1", "to": "all", "te')
        msgs, off = board.read_new(self.f, 0)
        self.assertEqual(len(msgs), 1)
        with open(board.board_path(self.f), "a", encoding="utf-8") as fh:
            fh.write('xt": "b"}\n')
        more, _ = board.read_new(self.f, off)
        self.assertEqual([m["text"] for m in more], ["b"])

    def test_recipients(self):
        m = lambda frm, to: {"from": frm, "to": to, "text": "t"}
        self.assertEqual(board.recipients(m("W2", ["all"]), [1, 2, 3]), [1, 3])        # not back to the sender
        self.assertEqual(board.recipients(m("W2", ["W3", "W9", "W2"]), [1, 2, 3]), [3])
        self.assertEqual(board.recipients(m("W2", ["queen"]), [1, 2, 3]), [])
        self.assertEqual(board.recipients(m("queen", ["all"]), [1, 2]), [1, 2])
        self.assertTrue(board.to_queen(m("W2", ["queen"])))


class Routing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.host = mock.Mock(port=7877)
        self.writes = []
        self.host._call.side_effect = lambda m, *a, **k: (
            [] if m == "terminals" else (self.writes.append(a), {"ok": True})[1] if m == "terminal_write"
            else {"ok": True, "id": "t"})
        self.d = Dispatch([], {"models": {"claude": {}, "codex": {}}}, self.host)
        with mock.patch.object(Dispatch, "start"):
            for goal in ("normalise the data", "analyse snr", "tune the model"):
                self.d.add_job(goal, self.tmp, "claude", "haiku")
        for j in self.d.jobs:
            j.update(kind="task", status="agent working", terminal=f"t{j['priority']}")
        self.d.state_of = lambda t: ("working", "")

    def post(self, frm, to, text):
        board.post(self.tmp, frm, to, text)

    def test_a_finding_reaches_the_addressed_workers_and_the_queen_stream(self):
        self.post("W2", "all", "SNR is below 6 dB under frame 400; crop there")
        self.post("W2", ["W3"], "batch size 32 is the most the GPU holds")
        self.post("W1", "queen", "normalisation done: 0..1 float32")
        self.d._route_board(self.tmp)
        inbox = lambda p: board.unread(self.tmp, p, 0)[0]
        self.assertEqual(len(inbox(1)), 1)
        self.assertEqual(len(inbox(3)), 2)
        self.assertEqual(inbox(2), [])                       # nothing is sent back to its sender
        self.assertIn("from W2", inbox(1)[0])
        self.assertIn("crop there", inbox(3)[0])
        self.assertEqual([m["from"] for m in self.d.board_log], ["W2", "W2", "W1"])
        self.assertEqual(self.d.board_log[2]["delivered"], [])
        self.assertTrue(any("normalisation done" in e[1] for e in self.d.jobs[0]["events"]))
        self.d._route_board(self.tmp)                        # read once only
        self.assertEqual(len(inbox(3)), 2)

    def test_an_idle_worker_is_nudged_a_working_one_is_not(self):
        self.d.state_of = lambda t: ("idle", "") if t == "t1" else ("working", "")
        self.post("W2", "all", "hello")
        self.d._route_board(self.tmp)
        nudged = [w[0] for w in self.writes if "new message" in str(w)]
        self.assertEqual(nudged, ["t1"])

    def test_the_queen_and_the_user_speak_through_the_same_board(self):
        self.assertTrue(self.d.say("W2", "focus on the 16-bit range first")["ok"])
        self.assertIn("from queen", board.unread(self.tmp, 2, 0)[0][0])
        self.assertTrue(self.d.say("all", "stop using bicubic", sender="user")["ok"])
        self.assertIn("from user", board.unread(self.tmp, 3, 0)[0][-1])
        self.assertFalse(self.d.say("all", "   ")["ok"])

    def test_finished_workers_get_no_mail(self):
        self.d.jobs[2]["status"] = "done"
        self.post("W1", "all", "x")
        self.d._route_board(self.tmp)
        self.assertEqual(board.unread(self.tmp, 3, 0)[0], [])
        self.assertEqual(len(board.unread(self.tmp, 2, 0)[0]), 1)

    def test_the_brief_tells_a_worker_about_the_board_only_when_it_has_company(self):
        job = self.d.jobs[0]
        brief = self.d._brief(job, None, False)
        self.assertIn("board.jsonl", brief)
        self.assertIn("inbox/W1.md", brief)
        for o in self.d.jobs[1:]:
            o["status"] = "done"
        self.assertNotIn("board.jsonl", self.d._brief(job, None, False))


class ByteOrderMarks(unittest.TestCase):
    def test_a_board_whose_first_line_has_a_bom_is_still_read(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".queenbee"))
            with open(board.board_path(d), "wb") as fh:
                fh.write(b"\xef\xbb\xbf" + json.dumps({"from": "W1", "to": "all", "text": "hi"}).encode() + b"\n")
            msgs, _ = board.read_new(d, 0)
            self.assertEqual([m["text"] for m in msgs], ["hi"])

    def test_a_report_written_with_a_bom_counts(self):
        with tempfile.TemporaryDirectory() as d:
            host = mock.Mock(port=7877)
            host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "t"}
            disp = Dispatch([], {"models": {"claude": {}}}, host)
            with mock.patch.object(Dispatch, "start"):
                disp.add_job("x", d, "claude", "haiku")
            job = disp.jobs[0]
            os.makedirs(os.path.join(d, ".queenbee"))
            with open(disp.report_path(job), "wb") as fh:
                fh.write(b"\xef\xbb\xbf" + b'{"status": "done", "notes": "ok"}')
            self.assertEqual(disp._read_report(job)["status"], "done")


class LeanBrief(unittest.TestCase):
    def setUp(self):
        self.a, self.b = tempfile.mkdtemp(), tempfile.mkdtemp()
        host = mock.Mock(port=7877)
        host._call.side_effect = lambda m, *x, **k: [] if m == "terminals" else {"ok": True, "id": "t"}
        self.d = Dispatch([], {"models": {"claude": {}}}, host)
        with mock.patch.object(Dispatch, "start"):
            self.d.add_job("one", self.a, "claude", "haiku")
            self.d.add_job("two", self.b, "claude", "haiku")
        for j in self.d.jobs:
            j.update(kind="task", work_order="1.")

    def test_a_worker_in_another_folder_is_not_listed_and_gets_no_board_talk(self):
        brief = self.d._brief(self.d.jobs[0], None, False)
        self.assertNotIn("Other workers", brief)
        self.assertNotIn("board.jsonl", brief)

    def test_workers_of_one_folder_still_hear_of_each_other(self):
        self.d.jobs[1]["project_path"] = self.a
        brief = self.d._brief(self.d.jobs[0], None, False)
        self.assertIn("Other workers", brief)
        self.assertIn("board.jsonl", brief)

    def test_progress_lines_can_be_asked_for_sparingly(self):
        self.assertIn("after each meaningful step", self.d._brief(self.d.jobs[0], None, False))
        self.d.cfg["progress_lines"] = "few"
        brief = self.d._brief(self.d.jobs[0], None, False)
        self.assertIn("at most three short lines", brief)
        self.assertNotIn("after each meaningful step", brief)


class Findings(unittest.TestCase):
    """A finding is kept in .queenbee/FINDINGS.md for every later worker, once, and the next worker's brief points at it."""

    def test_a_marked_message_is_a_finding_and_is_kept_once(self):
        import os
        import tempfile
        from queenbee.mother import board
        tmp = tempfile.mkdtemp()
        self.assertEqual(board.clean({"from": "W2", "to": "all", "text": "finding: the Store Python cannot run in the sandbox"})["kind"], "finding")
        self.assertEqual(board.clean({"from": "W2", "to": "all", "kind": "finding", "text": "x"})["kind"], "finding")
        self.assertNotIn("kind", board.clean({"from": "W2", "to": "all", "text": "parser done"}))
        self.assertTrue(board.keep_finding(tmp, "W2", "the Store Python cannot run in the sandbox"))
        self.assertFalse(board.keep_finding(tmp, "W2", "the Store Python cannot run in the sandbox"))
        text = open(os.path.join(tmp, ".queenbee", "FINDINGS.md"), encoding="utf-8").read()
        self.assertEqual(text.count("Store Python"), 1)
        self.assertIn("# Findings", text)


class FixFinding(unittest.TestCase):
    """The integration-fix worker's closing note is kept as a finding; an ordinary worker's note is not."""

    def test_the_fix_workers_note_is_kept(self):
        import os
        import tempfile
        from queenbee.mother.dispatch import Dispatch
        tmp = tempfile.mkdtemp()
        fix = {"priority": 4, "title": "integration fix 1", "project_path": tmp}
        self.assertTrue(Dispatch._keep_fix_finding(fix, "Check 109: a derived table without an alias must parse."))
        self.assertFalse(Dispatch._keep_fix_finding(fix, "Check 109: a derived table without an alias must parse."))
        self.assertFalse(Dispatch._keep_fix_finding({"priority": 1, "title": "parser", "project_path": tmp}, "parser done"))
        self.assertFalse(Dispatch._keep_fix_finding(fix, "   "))
        text = open(os.path.join(tmp, ".queenbee", "FINDINGS.md"), encoding="utf-8").read()
        self.assertIn("- (W4, general) Check 109", text)
        from queenbee.mother import board
        self.assertEqual(board.load_findings(tmp)[0]["source"], "fix")
        self.assertNotIn("parser done", text)


class FindingsAsRecords(unittest.TestCase):
    """Findings are records: scope and source, validated when the package passes, superseded by name, read in from an
    older FINDINGS.md once, and the brief gets the relevant ones first."""

    def test_records_render_validate_and_supersede(self):
        import json
        import os
        import tempfile
        from queenbee.mother import board
        tmp = tempfile.mkdtemp()
        self.assertTrue(board.keep_finding(tmp, "W2", "storage.py: Storage.views is a dict name -> Query AST"))
        self.assertTrue(board.keep_finding(tmp, "W4", "parser.py: a derived table needs no alias when it has a WITH", source="fix"))
        recs = board.load_findings(tmp)
        self.assertEqual([r["id"] for r in recs], ["F1", "F2"])
        self.assertEqual(recs[0]["scope"], "storage")
        self.assertEqual(recs[1]["source"], "fix")
        text = open(os.path.join(tmp, ".queenbee", "FINDINGS.md"), encoding="utf-8").read()
        self.assertIn("- (W2, storage) storage.py: Storage.views", text)
        self.assertEqual(board.validate_findings(tmp, "python check.py passes"), 2)
        self.assertIn("[checked: python check.py passes]", open(os.path.join(tmp, ".queenbee", "FINDINGS.md"), encoding="utf-8").read())
        self.assertTrue(board.keep_finding(tmp, "W3", "storage.py: Storage.views maps lowercase names to Query ASTs (supersedes F1)",
                                           supersedes=board.supersede_hint("supersedes F1")))
        recs = board.load_findings(tmp)
        self.assertEqual(recs[0]["status"], "superseded")
        self.assertEqual(recs[0]["superseded_by"], "F3")
        text = open(os.path.join(tmp, ".queenbee", "FINDINGS.md"), encoding="utf-8").read()
        self.assertNotIn("is a dict name -> Query AST", text)
        self.assertIn("1 earlier finding superseded", text)
        long = "x" * 1500
        self.assertTrue(board.keep_finding(tmp, "W1", long))
        self.assertIn(long, open(os.path.join(tmp, ".queenbee", "findings.jsonl"), encoding="utf-8").read())   # no cut

    def test_an_older_findings_file_is_read_in_once(self):
        import os
        import tempfile
        from queenbee.mother import board
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, ".queenbee"))
        with open(os.path.join(tmp, ".queenbee", "FINDINGS.md"), "w", encoding="utf-8") as fh:
            fh.write("# Findings\n\n- (W2) dml.py: execute(storage, stmt, select, subquery) -> Result\n- (W1) parser decisions\n")
        recs = board.load_findings(tmp)
        self.assertEqual([(r["source_job"], r["source"], r["scope"]) for r in recs], [("W2", "carried", "dml"), ("W1", "carried", "general")])
        self.assertTrue(os.path.isfile(os.path.join(tmp, ".queenbee", "findings.jsonl")))
        self.assertFalse(board.keep_finding(tmp, "W2", "dml.py: execute(storage, stmt, select, subquery) -> Result"))

    def test_the_brief_gets_the_relevant_findings_first(self):
        import tempfile
        from queenbee.mother import board
        from queenbee.mother.dispatch import Dispatch
        tmp = tempfile.mkdtemp()
        board.keep_finding(tmp, "W2", "storage.py: views live in storage.views")
        board.keep_finding(tmp, "W1", "parser.py: WITH is stored on the outer Select")
        board.keep_finding(tmp, "W3", "the executor calls compute_window after HAVING")
        note = Dispatch._findings_note({"project_path": tmp, "title": "Parser and expressions", "files": ["minidb/parser.py"]})
        self.assertLess(note.index("parser.py: WITH"), note.index("storage.py: views"))
        self.assertIn("supersedes", note)
        self.assertEqual(Dispatch._findings_note({"project_path": tempfile.mkdtemp(), "title": "x", "files": []}), "")


class StaleAndConflictingFindings(unittest.TestCase):
    """A finding keeps a content hash of the files it names: any change, however soon, or the file's removal makes it stale
    and takes it out of the brief's first list. A failing check that names its file puts a conflict on it. A passing check
    stamps the current findings and clears their conflicts, and does not revive a stale one; a person can."""

    def make(self):
        import os
        import tempfile
        from queenbee.mother import board
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "minidb"))
        src = os.path.join(tmp, "minidb", "storage.py")
        open(src, "w", encoding="utf-8").write("x = 1\n")
        self.assertTrue(board.keep_finding(tmp, "W2", "minidb/storage.py: Storage.views is a dict of name -> Query"))
        self.assertTrue(board.keep_finding(tmp, "W1", "the parser keeps WITH on the outer select"))
        return tmp, src, board

    def test_a_change_right_away_makes_the_finding_stale(self):
        import os
        tmp, src, board = self.make()
        recs = board.load_findings(tmp)
        self.assertEqual(recs[0]["files"], ["minidb/storage.py"])
        self.assertTrue(recs[0]["hashes"]["minidb/storage.py"])
        self.assertEqual(board.refresh_findings(tmp), 0)
        open(src, "a", encoding="utf-8").write("y = 2\n")                       # within the same second: still a change
        self.assertEqual(board.refresh_findings(tmp), 1)
        recs = board.load_findings(tmp)
        self.assertEqual((recs[0]["status"], recs[1]["status"]), ("stale", "active"))
        text = open(os.path.join(tmp, ".queenbee", "FINDINGS.md"), encoding="utf-8").read()
        self.assertIn("## May be out of date", text)
        self.assertEqual([r["id"] for r in board.relevant_findings(tmp, ["minidb/storage.py"], [])], ["F2"])

    def test_a_deleted_file_makes_the_finding_stale(self):
        import os
        tmp, src, board = self.make()
        os.remove(src)
        self.assertEqual(board.refresh_findings(tmp), 1)
        self.assertIn("no longer exists", board.load_findings(tmp)[0]["stale_reason"])

    def test_a_passing_check_stamps_current_findings_and_does_not_revive_stale_ones(self):
        import os
        tmp, src, board = self.make()
        open(src, "a", encoding="utf-8").write("y = 2\n")
        self.assertEqual(board.refresh_findings(tmp), 1)                        # F1 stale
        self.assertEqual(board.flag_conflicts(tmp, "FAIL test_views ... minidb/storage.py line 40"), 1)
        self.assertEqual(board.validate_findings(tmp, "python check.py passes"), 1)   # F2 stamped; F1 untouched
        recs = board.load_findings(tmp)
        self.assertEqual(recs[0]["status"], "stale")
        self.assertIn("conflict", recs[0])
        self.assertEqual(recs[1]["validated_by"], "python check.py passes")
        self.assertTrue(board.settle_finding(tmp, "F1", "validate", by="Ada"))   # a person confirms it
        recs = board.load_findings(tmp)
        self.assertEqual(recs[0]["status"], "active")
        self.assertNotIn("conflict", recs[0])
        self.assertEqual(board.refresh_findings(tmp), 0)                        # validated as the file is now
        self.assertTrue(board.settle_finding(tmp, "F1", "retire", by="Ada"))
        self.assertEqual(board.load_findings(tmp)[0]["status"], "retired")
        self.assertFalse(board.settle_finding(tmp, "F9", "retire"))
        text = open(os.path.join(tmp, ".queenbee", "FINDINGS.md"), encoding="utf-8").read()
        self.assertNotIn("Storage.views", text)

    def test_files_named(self):
        from queenbee.mother import board
        self.assertEqual(board.files_named("dml.py: execute(storage, stmt) -> Result; see minidb/storage.py and docs/ARCHITECTURE.md"),
                         ["dml.py", "minidb/storage.py", "docs/ARCHITECTURE.md"])
        self.assertEqual(board.files_named("no files here"), [])


class FindingsInTheWindow(unittest.TestCase):
    """The Report tab's findings card: every record of the queen's folders with its state, and the user's two answers."""

    def make(self):
        from queenbee.mother import config
        from queenbee.mother.server import Mother

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        tmp = self.dir.name
        self.src = os.path.join(tmp, "parser.py")
        open(self.src, "w", encoding="utf-8").write("x = 1\n")
        board.keep_finding(tmp, "W1", "parser.py returns a list of tokens, never None")
        host = mock.MagicMock()
        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(host, queen="q9", name="T", primary=False)
        q.dispatch = Dispatch([], config.load(), host, 7878)
        q.dispatch.jobs.append({"priority": 1, "kind": "task", "title": "parser", "status": "done", "project_path": tmp,
                                "agent": "claude", "events": []})
        return q, tmp

    def test_the_card_shows_each_finding_with_its_state_as_the_files_are_now(self):
        q, tmp = self.make()
        [f] = q.finding_records()["folders"]
        self.assertEqual(f["folder"], tmp)
        self.assertEqual([(r["id"], r["status"]) for r in f["records"]], [("F1", "active")])
        open(self.src, "a", encoding="utf-8").write("y = 2\n")
        r = q.finding_records()["folders"][0]["records"][0]
        self.assertEqual(r["status"], "stale")
        self.assertIn("parser.py changed", r["stale_reason"])

    def test_the_user_confirms_or_retires_a_finding_of_this_queen_only(self):
        q, tmp = self.make()
        open(self.src, "a", encoding="utf-8").write("y = 2\n")
        q.finding_records()
        self.assertEqual(q.settle_finding(tmp, "F1", "validate"), {"ok": True})
        r = board.load_findings(tmp)[0]
        self.assertEqual((r["status"], r["validated_by"]), ("active", "you confirmed it"))
        self.assertFalse(q.settle_finding(tempfile.gettempdir(), "F1", "retire")["ok"])     # not her folder
        self.assertFalse(q.settle_finding(tmp, "F1", "delete")["ok"])
        self.assertFalse(q.settle_finding(tmp, "F7", "retire")["ok"])
        self.assertEqual(q.settle_finding(tmp, "F1", "retire"), {"ok": True})
        self.assertEqual(board.load_findings(tmp)[0]["status"], "retired")
        self.assertNotIn("never None", open(board.findings_path(tmp), encoding="utf-8").read())

    def test_a_queen_whose_folders_kept_nothing_shows_nothing(self):
        q, tmp = self.make()
        os.remove(board.records_path(tmp))
        os.remove(board.findings_path(tmp))
        self.assertEqual(q.finding_records(), {"ok": True, "folders": []})
