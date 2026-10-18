"""Approvals without screen reading: a worker's Claude Code hook asks the queen before a tool call; Codex team workers run with
approvals off inside their sandbox; "one agent alone" keeps the vendor's own prompts."""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from queenbee.mother import approver, auth, config, hook
from queenbee.mother.dispatch import Dispatch
from queenbee.mother.server import Mother


class ToolCalls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inside = os.path.join(self.tmp, "src", "a.py")

    def test_edits_inside_the_project_are_allowed_and_outside_asks(self):
        self.assertEqual(approver.classify_tool("Edit", {"file_path": self.inside}, self.tmp)[0], "allow")
        self.assertEqual(approver.classify_tool("Write", {"file_path": os.path.join(self.tmp, "..", "x.py")}, self.tmp)[0], "deny")
        self.assertEqual(approver.classify_tool("Write", {"file_path": r"C:\elsewhere\x.py"}, self.tmp)[0], "deny")

    def test_commands_follow_the_screen_rules(self):
        self.assertEqual(approver.classify_tool("Bash", {"command": "python -m unittest discover -s tests"}, self.tmp)[0], "allow")
        self.assertEqual(approver.classify_tool("Bash", {"command": "pip install requests"}, self.tmp)[0], "deny")
        self.assertEqual(approver.classify_tool("Bash", {"command": "curl https://example.com -o x"}, self.tmp)[0], "deny")
        self.assertEqual(approver.classify_tool("Bash", {"command": ""}, self.tmp)[0], "ask")

    def test_what_a_worker_says_in_an_echo_is_not_what_it_runs(self):
        # Seen in the first real run: a progress line mentioning "pip install" got an innocent command refused.
        cmd = 'python -c "print(1+1)"; echo "python ok; next: pip install requests" >> .queenbee/W1.progress'
        self.assertEqual(approver.classify_tool("Bash", {"command": cmd}, self.tmp)[0], "allow")
        self.assertEqual(approver.classify_tool("Bash", {"command": 'echo "x" >> log; pip install requests'}, self.tmp)[0], "deny")

    def test_the_agents_own_scratch_folder_is_writable_the_temp_folder_is_not(self):
        import tempfile
        scratch = os.path.join(tempfile.gettempdir(), "claude", "C--some-project", "abc", "scratchpad", "patch.py")
        self.assertEqual(approver.classify_tool("Write", {"file_path": scratch}, self.tmp)[0], "allow")
        self.assertEqual(approver.classify_tool("Write", {"file_path": os.path.join(tempfile.gettempdir(), "patch.py")}, self.tmp)[0], "deny")
        self.assertEqual(approver.classify_tool("Bash", {"command": f'cat > "{scratch}" <<EOF\nprint(1)\nEOF'}, self.tmp)[0], "allow")

    def test_a_heredoc_body_is_text_not_a_command(self):
        body = "python - <<'EOF'\np = 'minidb/storage.py'\ns = open(p, encoding='utf-8').read()\ndel s\nopen(p, 'w').write(s)\nEOF"
        self.assertEqual(approver.classify_tool("Bash", {"command": body}, self.tmp)[0], "allow")
        climbs = "python - <<'EOF'\nopen('../x.py', 'w').write('hi')\nEOF"
        self.assertEqual(approver.classify_tool("Bash", {"command": climbs}, self.tmp)[0], "deny")
        self.assertEqual(approver.classify_tool("Bash", {"command": "rm -rf build"}, self.tmp)[0], "deny")

    def test_edits_outside_the_project_are_never_approved_even_in_temp_or_pythons_folder(self):
        # The review's finding: the command rules may name the temp folder and Python's own folder, the edit rules may not.
        import sys
        for folder in (tempfile.gettempdir(), os.path.dirname(sys.executable)):
            self.assertEqual(approver.classify_tool("Write", {"file_path": os.path.join(folder, "x.py")}, self.tmp)[0], "deny", folder)

    def test_reads_are_allowed_the_network_is_not_and_unknown_tools_ask(self):
        self.assertEqual(approver.classify_tool("Read", {"file_path": self.inside}, self.tmp)[0], "allow")
        self.assertEqual(approver.classify_tool("WebFetch", {"url": "https://x"}, self.tmp)[0], "deny")
        self.assertEqual(approver.classify_tool("Task", {}, self.tmp)[0], "ask")


    def test_waiting_on_the_board_is_refused_with_what_to_do_instead(self):
        # The forms the Haiku workers used in real runs, each of which left a worker deaf to the queen for minutes.
        waits = [
            "cd x && for i in $(seq 1 27); do if [ -s .queenbee/board.jsonl ] && grep -q W1 .queenbee/board.jsonl; then break; fi; sleep 20; done",
            "cd x && echo \"waiting\" >> .queenbee/W2.progress && sleep 60; cat .queenbee/board.jsonl",
            "until grep -q '\"from\": *\"W1\"' .queenbee/board.jsonl 2>/dev/null; do sleep 2; done",
            "touch .queenbee/board.jsonl && tail -n 0 -F .queenbee/board.jsonl",
            "Get-Content .queenbee/inbox/W2.md -Wait",
        ]
        for cmd in waits:
            self.assertEqual(approver.classify_tool("Bash", {"command": cmd}, self.tmp), ("deny", approver.NO_WAITING), cmd)
        self.assertEqual(approver.classify_tool("Monitor", {"command": waits[2]}, self.tmp)[0], "deny")
        # Reading the board once, a progress line that mentions waiting, a watcher on something else: as before.
        for cmd in ("tail -n 3 .queenbee/board.jsonl; cat .queenbee/inbox/W2.md",
                    "python -m unittest; echo \"next: wait until W1 is done\" >> .queenbee/W2.progress",
                    # seen in a run: the words of a report written with printf, not a loop
                    "printf '%s\\n' '{\"status\": \"blocked\", \"notes\": \"Blocked until W1 lands the store\"}' > .queenbee/W2.json"):
            self.assertEqual(approver.classify_tool("Bash", {"command": cmd}, self.tmp)[0], "allow", cmd)
        self.assertEqual(approver.classify_tool("Monitor", {"command": "python -m http.server"}, self.tmp)[0], "ask")


class TheQueenAnswers(unittest.TestCase):
    def make(self, manual=False):
        host = mock.MagicMock()
        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(host, queen="q9", name="T", primary=False)
        d = Dispatch([], config.load(), host, 7878)
        self.tmp = tempfile.mkdtemp()
        d.jobs.append({"priority": 2, "kind": "task", "title": "parser", "status": "agent working", "project_path": self.tmp,
                       "agent": "claude", "tier": "sonnet", "events": [], "terminal": "term-2"})
        q.dispatch = d
        q.manual_approvals = manual
        return q

    def test_an_allowed_call_is_recorded_with_the_other_decisions(self):
        q = self.make()
        out = q.hook_decision(2, self.tmp, "Bash", {"command": "python -m pytest -q"})
        self.assertEqual(out["decision"], "allow")
        self.assertEqual(q.approvals[-1]["decision"], "approved")
        self.assertEqual(q.approvals[-1]["source"], "hook")
        self.assertEqual(q.dispatch.approvals[-1]["priority"], 2)

    def test_a_refused_call_is_declined_by_the_queen_with_her_reason(self):
        q = self.make()
        out = q.hook_decision(2, self.tmp, "Bash", {"command": "rm -rf build"})
        self.assertEqual(out["decision"], "deny")
        self.assertIn("no", out["reason"])
        self.assertEqual(q.approvals[-1]["decision"], "declined by the queen")

    def test_a_wait_on_the_board_is_turned_back_without_a_mark_for_the_user(self):
        q = self.make()
        out = q.hook_decision(2, self.tmp, "Monitor", {"command": "tail -n 0 -F .queenbee/board.jsonl"})
        self.assertEqual((out["decision"], out["reason"]), ("deny", approver.NO_WAITING))
        job = q.dispatch.jobs[0]
        self.assertFalse(job.get("refused"))
        self.assertIn("not to wait", job["events"][-1][1])

    def test_a_free_workers_questions_stay_the_users(self):
        q = self.make(manual=True)
        out = q.hook_decision(2, self.tmp, "Bash", {"command": "python -m pytest -q"})
        self.assertEqual(out["decision"], "ask")
        self.assertEqual(q.approvals, [])

    def test_an_unknown_worker_asks(self):
        q = self.make()
        self.assertEqual(q.hook_decision(9, self.tmp, "Bash", {"command": "ls"})["decision"], "ask")

    def test_a_refusal_stays_on_the_workers_node_until_the_user_answers(self):
        q = self.make()
        q.hook_decision(2, self.tmp, "Bash", {"command": "rm -rf build"})
        q.hook_decision(2, self.tmp, "Bash", {"command": "rm -rf build"})
        job = q.dispatch.jobs[0]
        self.assertEqual([r["call"] for r in job["refused"]], ["Bash: rm -rf build"])      # the same call once
        self.assertEqual(q.settle_refusal(2, "Bash: rm -rf build", allow=False), {"ok": True})
        self.assertEqual(job["refused"], [])
        self.assertEqual(q.hook_decision(2, self.tmp, "Bash", {"command": "rm -rf build"})["decision"], "deny")
        self.assertFalse(q.settle_refusal(2, "Bash: rm -rf dist", allow=True)["ok"])

    def test_allowing_a_refusal_lets_that_exact_call_through_and_tells_the_worker(self):
        q = self.make()
        q.hook_decision(2, self.tmp, "Bash", {"command": "rm -rf build"})
        with mock.patch("queenbee.mother.server.time.sleep"):
            out = q.settle_refusal(2, "Bash: rm -rf build", allow=True)
        self.assertEqual(out, {"ok": True, "told": True})
        written = [c.args[2] for c in q.host._call.call_args_list if c.args[0] == "terminal_write"]
        self.assertIn("rm -rf build", written[0])
        self.assertEqual(q.dispatch.approvals[0]["decision"], "declined by the queen, then allowed by you")
        again = q.hook_decision(2, self.tmp, "Bash", {"command": "rm -rf build"})
        self.assertEqual(again["decision"], "allow")
        self.assertIn("you allowed", again["reason"])
        self.assertEqual(q.hook_decision(2, self.tmp, "Bash", {"command": "rm -rf dist"})["decision"], "deny")
        self.assertEqual(q.dispatch.jobs[0]["refused"][0]["call"], "Bash: rm -rf dist")

    def test_a_worker_that_is_not_running_is_not_told(self):
        q = self.make()
        q.hook_decision(2, self.tmp, "Bash", {"command": "rm -rf build"})
        q.dispatch.jobs[0]["status"] = "stopped"
        self.assertEqual(q.settle_refusal(2, "Bash: rm -rf build", allow=True), {"ok": True, "told": False})
        self.assertFalse(any(c.args[0] == "terminal_write" for c in q.host._call.call_args_list))
        self.assertEqual(q.hook_decision(2, self.tmp, "Bash", {"command": "rm -rf build"})["decision"], "allow")


class TheHookProgram(unittest.TestCase):
    def test_a_forced_ask_is_printed_so_it_overrides_what_the_start_flags_pre_approved(self):
        # Probed on Claude Code 2.1.301 (claude -p, --allowedTools Write, acceptEdits): with this output the Write did not
        # run; without the hook it did.
        call = json.dumps({"cwd": "C:/p", "tool_name": "Write", "tool_input": {"file_path": "C:/p/a.txt"}})
        with mock.patch.object(hook, "decide", return_value={"ok": True, "decision": "ask", "force": True, "reason": "r"}), \
                mock.patch("sys.stdin", io.StringIO(call)), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(hook.main(["--port", "7880", "--job", "2"]), 0)
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_it_prints_claude_codes_shape_for_allow_and_deny_and_nothing_for_ask(self):
        call = json.dumps({"cwd": "C:/p", "tool_name": "Bash", "tool_input": {"command": "ls"}})
        for decision, printed in (("allow", True), ("deny", True), ("ask", False)):
            with mock.patch.object(hook, "decide", return_value={"ok": True, "decision": decision, "reason": "r"}), \
                    mock.patch("sys.stdin", io.StringIO(call)), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(hook.main(["--port", "7880", "--job", "2"]), 0)
            text = out.getvalue()
            if printed:
                self.assertEqual(json.loads(text)["hookSpecificOutput"]["permissionDecision"], decision)
            else:
                self.assertEqual(text, "")

    def test_no_server_means_no_answer_and_no_crash(self):
        with mock.patch.object(hook, "decide", side_effect=OSError("down")), mock.patch("sys.stdin", io.StringIO("{}")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(hook.main([]), 0)
        self.assertEqual(out.getvalue(), "")


class WorkersStartWithTheHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        host = mock.Mock(port=7877)
        host._call.side_effect = lambda m, *a, **k: [] if m == "terminals" else {"ok": True, "id": "term-9"}
        self.d = Dispatch([], {"models": {"claude": {"low": "haiku", "medium": "sonnet", "high": "sonnet"}, "codex": {"low": "", "medium": "", "high": ""}},
                               "efforts": {"codex": {"low": "low", "medium": "medium", "high": "high"}}}, host, 7878)

    def job(self, agent, plain=False):
        return {"priority": 3, "title": "w", "goal": "g", "project_path": self.tmp, "agent": agent, "effort": "medium",
                "same_as": None, "work_order": "", "kind": "task", "plain": plain, "tier": None}

    def test_a_claude_team_worker_gets_a_settings_file_naming_the_hook(self):
        token = auth.make_token(7880)                                      # what a server on 7880 would have written
        with mock.patch("queenbee.mother.dispatch.binary_for", return_value="claude.exe"):
            argv = self.d._argv(self.job("claude"), "the brief")
        self.assertIn("--settings", argv)
        path = argv[argv.index("--settings") + 1]
        settings = json.load(open(path, encoding="utf-8"))
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertTrue("queenbee.mother.hook" in command or "hook.py" in command, command)
        self.assertIn("--port 7880", command)
        self.assertIn("--job 3", command)
        self.assertIn("--token " + token, command)
        self.assertIn("Bash", settings["hooks"]["PreToolUse"][0]["matcher"])

    def test_one_agent_alone_gets_no_hook_and_keeps_codexs_prompts(self):
        with mock.patch("queenbee.mother.dispatch.binary_for", return_value="claude.exe"):
            self.assertNotIn("--settings", self.d._argv(self.job("claude", plain=True), "b"))
        with mock.patch("queenbee.mother.dispatch.binary_for", return_value="codex.exe"):
            self.assertNotIn("-a", self.d._argv(self.job("codex", plain=True), "b"))
            team = self.d._argv(self.job("codex"), "b")
        self.assertEqual(team[team.index("-a") + 1], "never")


class OverHttp(unittest.TestCase):
    """The hook reaches the queen through the page server: the request is matched to a worker by folder and job number."""

    def setUp(self):
        import threading
        import urllib.request
        from queenbee.mother.server import serve

        self.tmp = tempfile.mkdtemp()
        host = mock.MagicMock()
        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(host, queen="q1", name="T", primary=True)
        d = Dispatch([], config.load(), host, 7878)
        d.jobs.append({"priority": 1, "kind": "task", "title": "w", "status": "agent working", "project_path": self.tmp,
                       "agent": "claude", "tier": "haiku", "events": [], "terminal": "term-1"})
        q.dispatch = d
        self.q = q
        self.httpd = serve(0, 1)
        self.httpd.RequestHandlerClass.host = host
        self.httpd.RequestHandlerClass.hive = mock.Mock(queens=[q])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.urlopen = urllib.request.urlopen
        self.Request = urllib.request.Request

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, body, token=None):
        headers = {"Content-Type": "application/json", "X-QueenBee-Token": self.httpd.token if token is None else token}
        req = self.Request(self.base + "/api/hook", json.dumps(body).encode(), headers)
        with self.urlopen(req, timeout=10) as r:
            return json.load(r)

    def test_the_hook_program_gets_the_queens_answer(self):
        out = hook.decide(self.httpd.server_port, 1, {"cwd": self.tmp, "tool_name": "Bash", "tool_input": {"command": "python -m pytest"}},
                          self.httpd.token)
        self.assertEqual(out["decision"], "allow")
        self.assertEqual(self.q.approvals[-1]["source"], "hook")

    def test_a_call_from_a_folder_no_worker_owns_is_left_to_the_prompt(self):
        out = self.post({"job": 1, "cwd": tempfile.mkdtemp(), "tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(out["decision"], "ask")
        self.assertFalse(out["ok"])


class WriteTimeConflicts(unittest.TestCase):
    """Two workers in one folder: a file the plan gave to another worker is refused at the moment of the write and sent to the
    board; a file nobody owns that both edit gets both told, once."""

    def make(self):
        host = mock.MagicMock()
        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(host, queen="q9", name="T", primary=False)
        d = Dispatch([], config.load(), host, 7878)
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, ".queenbee"), exist_ok=True)
        for n, files in ((1, ["minidb/parser.py"]), (2, ["minidb/storage.py"]), (3, [])):
            d.jobs.append({"priority": n, "kind": "task", "title": f"w{n}", "status": "agent working", "project_path": self.tmp,
                           "agent": "claude", "tier": "sonnet", "events": [], "terminal": f"term-{n}", "files": files})
        q.dispatch = d
        return q

    def test_an_edit_of_another_workers_file_is_refused_and_sent_to_the_board(self):
        from queenbee.mother import board
        q = self.make()
        out = q.hook_decision(3, self.tmp, "Edit", {"file_path": os.path.join(self.tmp, "minidb", "parser.py")})
        self.assertEqual(out["decision"], "deny")
        self.assertIn("W1", out["reason"])
        msgs, _ = board.read_new(os.path.join(self.tmp, ".queenbee"), 0)
        self.assertTrue(any("W1's file" in (m.get("text") or "") for m in msgs), msgs)
        own = q.hook_decision(1, self.tmp, "Edit", {"file_path": os.path.join(self.tmp, "minidb", "parser.py")})
        self.assertEqual(own["decision"], "allow")

    def test_two_workers_on_one_unowned_file_are_told_once(self):
        from queenbee.mother import board
        q = self.make()
        path = os.path.join(self.tmp, "docs", "notes.md")
        self.assertEqual(q.hook_decision(1, self.tmp, "Write", {"file_path": path})["decision"], "allow")
        self.assertEqual(q.hook_decision(3, self.tmp, "Write", {"file_path": path})["decision"], "allow")
        self.assertEqual(q.hook_decision(3, self.tmp, "Write", {"file_path": path})["decision"], "allow")
        msgs, _ = board.read_new(os.path.join(self.tmp, ".queenbee"), 0)
        told = [m for m in msgs if "both editing" in (m.get("text") or "")]
        self.assertEqual(len(told), 1, msgs)
        self.assertIn("W3", told[0].get("text"))


class ShellWrites(unittest.TestCase):
    """A shell command that writes outside the project is refused, whatever form the write takes; writes inside pass."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def deny(self, command):
        verdict, why = approver.classify_tool("Bash", {"command": command}, self.tmp)
        self.assertEqual(verdict, "deny", (command, why))

    def allow(self, command):
        verdict, why = approver.classify_tool("Bash", {"command": command}, self.tmp)
        self.assertEqual(verdict, "allow", (command, why))

    def test_the_forms_the_review_named(self):
        self.deny("echo x > ../outside.txt")
        self.deny("Set-Content ..\\outside.txt 'x'")
        self.deny("echo x > /tmp/outside.txt")
        self.deny("cd .. && echo x > out.txt")                   # a relative write after cd .. lands outside
        self.deny("python -c \"open('../x.txt', 'w').write('hi')\"")
        self.deny("cat a | tee ~/notes.txt")
        self.deny("cp build/out.zip C:\\elsewhere\\out.zip")
        self.deny("Out-File -FilePath C:\\elsewhere\\log.txt")
        self.deny("mkdir ../newdir")

    def test_cd_is_not_a_write(self):
        import os
        import queenbee
        pkg = os.path.dirname(os.path.abspath(queenbee.__file__))
        self.allow(f'cd "{pkg}" 2>/dev/null && ls -la && grep -ril "hive" --include=*.py . | head -30')   # the demo worker's command
        self.allow("cd .queenbee && echo hi > note.txt")           # relative to the folder cd went to: inside
        self.deny("cd .. && echo x > out.txt")
        self.assertEqual(approver.working_dir("cd sub && echo 1 > a.txt", self.tmp), os.path.join(self.tmp, "sub"))
        self.assertEqual(approver.working_dir("echo 1 > a.txt", self.tmp), self.tmp)

    def test_writes_inside_the_project_still_pass(self):
        self.allow("echo x > notes.txt")
        self.allow("python -m pytest -q tests > test.log 2>&1")
        self.allow("Set-Content out\\report.md 'x'")
        self.allow(f"cd {self.tmp} && python check.py")
        self.allow("cat a | tee build.log")
        self.allow("python -c \"open('tests/x.txt', 'w').write('hi')\"")
        self.allow("ls > /dev/null; echo done")

    def test_a_relative_edit_path_is_the_workers_own_folder(self):
        self.assertEqual(approver.classify_tool("Edit", {"file_path": "minidb/parser.py"}, self.tmp)[0], "allow")
        self.assertEqual(approver.classify_tool("Write", {"file_path": "../x.py"}, self.tmp)[0], "deny")
        self.allow("python check.py > 1:")                       # a stream number, not a file
        self.allow("python check.py 2>&1 | tail -n 20")

    def test_targets_are_found_as_written(self):
        self.assertEqual(approver.write_targets("echo x > ../o.txt 2>&1"), ["../o.txt"])
        self.assertEqual(approver.write_targets("cp a.txt b.txt c.txt"), ["c.txt"])
        self.assertEqual(approver.write_targets('Set-Content -Path "a b.txt" x'), ["a b.txt"])
        self.assertEqual(approver.write_targets("ls | grep x"), [])

    def test_a_file_named_md_is_not_the_md_command(self):
        # seen: `wc -l ... TASK.md docs/*.md` refused as "writes outside the project: docs/*.md" (\bmd\b met the extension)
        self.assertEqual(approver.write_targets("wc -l minidb/*.py TASK.md docs/*.md"), [])
        self.assertEqual(approver.write_targets("cat notes.cp docs/x.mv"), [])
        self.assertEqual(approver.write_targets("md newdir"), ["newdir"])
        self.assertEqual(approver.write_targets("ls; mkdir -p build/out"), ["build/out"])
        self.deny("md ..\\newdir")

    def test_open_with_a_variable_is_read_through_its_assignment(self):
        script = "python - <<'EOF'\np='minidb/plan.py'\ns=open(p,encoding='utf-8').read()\nopen(p,'w',encoding='utf-8').write(s)\nEOF"
        self.assertEqual(approver.write_targets(script), ["minidb/plan.py"])
        self.allow(script)
        self.deny("python - <<'EOF'\np = '../x.py'\nopen(p, 'w').write('x')\nEOF")
        self.assertEqual(approver.write_targets("python -c \"open(path, 'w')\""), [])      # no assignment: not a path


class GitBashPaths(unittest.TestCase):
    """Claude Code's shell on Windows is Git Bash: a worker may write `cd /c/proj` for C:\\proj (seen 2026-10-18, three
    refusals of commands inside the project). /c/..., /cygdrive/c/... and /mnt/c/... name drive C on Windows."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        p = os.path.realpath(self.tmp)
        self.msys = "/" + p[0].lower() + p[2:].replace("\\", "/")

    def verdict(self, command):
        return approver.classify_tool("Bash", {"command": command}, self.tmp)

    def test_drive_paths_are_mapped_on_windows_only(self):
        with mock.patch.object(approver, "_WINDOWS", True):
            self.assertEqual(approver.native_path("/c/qbfilm/video-67"), "C:/qbfilm/video-67")
            self.assertEqual(approver.native_path("/cygdrive/d/x"), "D:/x")
            self.assertEqual(approver.native_path("/mnt/c/"), "C:/")
            self.assertEqual(approver.native_path("/c"), "C:/")
            self.assertEqual(approver.native_path("/tmp/x"), "/tmp/x")            # not a drive letter
            self.assertEqual(approver.native_path("/cd/x"), "/cd/x")
        with mock.patch.object(approver, "_WINDOWS", False):
            self.assertEqual(approver.native_path("/c/qbfilm"), "/c/qbfilm")      # on Linux or a Mac /c is a folder

    @unittest.skipUnless(os.name == "nt", "Git Bash paths name drives on Windows only")
    def test_the_three_commands_seen_are_allowed(self):
        cd = f'cd "{self.msys}"'                               # quoted: a temp folder may hold a space
        for command in (f'{cd}; cat .queenbee/inbox/W3.md 2>/dev/null; wc -l minidb/*.py TASK.md docs/*.md; grep -n "^#" TASK.md | head -80',
                        f'{cd}; echo "reading done; writing plan.py/executor.py" >> .queenbee/W3.progress; grep -n "^def " minidb/windows.py | head -20',
                        f"{cd} && python - <<'EOF'\np='minidb/plan.py'\ns=open(p,encoding='utf-8').read()\nopen(p,'w',encoding='utf-8').write(s)\nEOF"):
            self.assertEqual(self.verdict(command)[0], "allow", (command, self.verdict(command)[1]))
        self.assertTrue(approver.same_folder(approver.working_dir(f"{cd} && ls", self.tmp), self.tmp))
        self.assertEqual(approver.classify_tool("Write", {"file_path": self.msys + "/minidb/x.py"}, self.tmp)[0], "allow")

    @unittest.skipUnless(os.name == "nt", "Git Bash paths name drives on Windows only")
    def test_writes_outside_the_project_in_that_form_are_still_refused(self):
        for command in ("cd /c/elsewhere; echo x > out.txt", "echo x > /c/Windows/x.txt", f'cd "{self.msys}/.."; echo x > y.txt',
                        "python -c \"open('/c/elsewhere/x', 'w')\"", f'cd "{self.msys}"; cp a.txt /d/out/a.txt'):
            self.assertEqual(self.verdict(command)[0], "deny", command)
        self.assertEqual(approver.classify_tool("Write", {"file_path": "/c/elsewhere/x.py"}, self.tmp)[0], "deny")


class TheToken(unittest.TestCase):
    """Every /api/ request needs the token the server made at its start; /api/ping is open; the page gets the token only
    when opened the way the app opens it."""

    def setUp(self):
        import threading
        import urllib.error
        import urllib.request
        from queenbee.mother.server import serve
        self.httpd = serve(0, 1)
        self.httpd.RequestHandlerClass.host = mock.MagicMock()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.urlopen, self.Request, self.HTTPError = urllib.request.urlopen, urllib.request.Request, urllib.error.HTTPError

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path, headers=None):
        try:
            with self.urlopen(self.Request(self.base + path, headers=headers or {}), timeout=10) as r:
                return r.status, r.read()
        except self.HTTPError as e:
            return e.code, e.read()

    def test_the_token_is_written_and_required(self):
        self.assertEqual(auth.read_token(self.httpd.server_port), self.httpd.token)
        self.assertEqual(self.get("/api/ping")[0], 200)
        self.assertEqual(self.get("/api/agents")[0], 401)
        self.assertEqual(self.get("/api/agents", {"X-QueenBee-Token": "wrong"})[0], 401)
        self.assertEqual(self.get("/api/agents", {"X-QueenBee-Token": self.httpd.token})[0], 200)
        self.assertEqual(self.get("/api/agents", {"Authorization": "Bearer " + self.httpd.token})[0], 200)
        self.assertEqual(self.get("/api/agents?token=" + self.httpd.token)[0], 200)
        req = self.Request(self.base + "/api/hook", b"{}", {"Content-Type": "application/json"})
        try:
            self.urlopen(req, timeout=10)
            self.fail("a POST without the token went through")
        except self.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_the_page_carries_the_token_only_when_opened_by_the_app(self):
        status, body = self.get("/?k=" + self.httpd.token)
        self.assertEqual(status, 200)
        self.assertIn(b"window.QB_TOKEN=", body)
        self.assertIn(self.httpd.token.encode(), body)
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertNotIn(self.httpd.token.encode(), body)
        status, body = self.get("/?k=wrong")
        self.assertNotIn(self.httpd.token.encode(), body)



class TheUserAnswers(unittest.TestCase):
    """A queen whose user chose to answer the workers' prompts: she only watches; the workers start with the vendor's own
    prompts on, and her hook hands every call to the user."""

    def make(self):
        host = mock.MagicMock()
        with mock.patch("queenbee.mother.server.threading.Thread"):
            q = Mother(host, queen="q9", name="T", primary=False)
        self.tmp = tempfile.mkdtemp()
        d = Dispatch([], config.load(), host, 7878)
        d.jobs.append({"priority": 2, "kind": "task", "title": "parser", "status": "agent working", "project_path": self.tmp,
                       "agent": "claude", "tier": "sonnet", "effort": "medium", "events": [], "terminal": "term-2"})
        q.dispatch = d
        return q

    def argv(self, q, agent):
        job = dict(q.dispatch.jobs[0], agent=agent, tier="default" if agent == "codex" else "sonnet")
        with mock.patch("queenbee.mother.dispatch.binary_for", return_value=f"{agent}.exe"):
            return q.dispatch._argv(job, "the brief")

    def test_by_default_the_queen_answers_and_the_workers_never_wait(self):
        q = self.make()
        self.assertEqual(q.view()["answers"], "queen")
        self.assertEqual(q.hook_decision(2, self.tmp, "Bash", {"command": "rm -rf build"})["decision"], "deny")
        claude = self.argv(q, "claude")
        self.assertIn("acceptEdits", claude)
        self.assertIn("--allowedTools", claude)
        codex = self.argv(q, "codex")
        self.assertEqual(codex[codex.index("-a") + 1], "never")

    def test_when_the_user_answers_every_call_is_theirs_and_workers_start_with_prompts_on(self):
        q = self.make()
        self.assertEqual(q.set_answers("me"), {"ok": True, "answers": "me"})
        self.assertEqual(q.view()["answers"], "me")
        for call in ({"command": "rm -rf build"}, {"command": "python -m pytest -q"}):
            out = q.hook_decision(2, self.tmp, "Bash", call)
            self.assertEqual((out["decision"], out.get("force")), ("ask", True), call)
        self.assertEqual(q.approvals, [])
        self.assertNotIn("refused", q.dispatch.jobs[0])
        claude = self.argv(q, "claude")
        self.assertEqual(claude[claude.index("--permission-mode") + 1], "default")
        self.assertNotIn("--allowedTools", claude)
        self.assertNotIn("--disallowedTools", claude)
        self.assertIn("--settings", claude)                     # the hook stays, so the queen can take over again
        codex = self.argv(q, "codex")
        self.assertEqual(codex[codex.index("-a") + 1], "on-request")
        self.assertEqual(q.set_answers("queen"), {"ok": True, "answers": "queen"})
        self.assertEqual(q.hook_decision(2, self.tmp, "Bash", {"command": "rm -rf build"})["decision"], "deny")
        self.assertFalse(q.set_answers("nobody")["ok"])

    def test_a_dispatch_made_later_follows_the_choice(self):
        q = self.make()
        q.set_answers("me")
        q.dispatch = Dispatch([], config.load(), q.host, 7878)
        self.assertTrue(q.dispatch.asks_user)

    def test_the_choice_is_kept_with_the_queen(self):
        from pathlib import Path
        from queenbee.mother import server

        state = tempfile.mkdtemp()
        with mock.patch("queenbee.config._state_dir", return_value=Path(state)), \
                mock.patch("queenbee.mother.server.threading.Thread"):
            hive = server.Hive(mock.MagicMock())
            qid = hive.queens[0].queen
            self.assertEqual(hive.set_answers(qid, "me"), {"ok": True, "answers": "me"})
            again = server.Hive(mock.MagicMock())
            self.assertTrue(again.get(qid).user_answers)
            self.assertFalse(hive.set_answers("q404", "me")["ok"])
