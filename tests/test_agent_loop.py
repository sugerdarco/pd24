"""No model calls: real temporary Git repos plus deterministic CLI doubles."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from tools import agent_loop as a


def report(path, data):
    a.atomic_write(path, "```json\n" + json.dumps(data) + "\n```\n\n# Evidence\nVerified in test fixture.\n")


def review(action="CONTINUE", verdict="APPROVED", task_id="T-1", complete=False):
    return {"action": action, "verdict": verdict, "task_id": task_id,
            "summary": "Independent verification", "evidence": ["python3 -m unittest: PASS"],
            "recovery_strategy": "", "completion_checks": {k: complete for k in a.CHECKS}}


def task(task_id="T-1", issue="bug"):
    return {"task_id": task_id, "issue_key": issue, "objective": "Fix behavior",
            "acceptance_criteria": ["Regression passes"]}


def result(task_id="T-1", status="COMPLETE"):
    return {"task_id": task_id, "status": status,
            "acceptance_criteria": {"Regression passes": "PASS"},
            "tests": ["python3 -m unittest: PASS"]}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.coord = self.root / ".agents"
        self.session = str(uuid.uuid4())
        self.env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path(self.tmp.name) / "claude")})
        self.env.start()
        self.addCleanup(self.env.stop)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        a.git(self.root, "config", "user.name", "Test")
        a.git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / ".gitignore").write_text(".agents/\n.env\n")
        (self.root / "app.py").write_text("VALUE = 0\n")
        a.git(self.root, "add", ".")
        a.git(self.root, "commit", "-qm", "baseline")
        path = a.session_directory(self.root) / f"{self.session}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "user", "sessionId": self.session, "cwd": str(self.root)}) + "\n")
        with redirect_stdout(io.StringIO()):
            a.initialize(self.root, self.coord, self.session)
        self.app = a.Orchestrator(self.root, self.coord)

    def fake_agents(self, behavior=None):
        original = a.execute
        calls = []
        def execute(argv, root, timeout, prompt="", log=None):
            if argv[0] not in {"codex", "claude"}:
                return original(argv, root, timeout, prompt, log)
            if "--help" in argv:
                return "--sandbox --output-schema --output-last-message --json --resume --print --output-format --restricted --permission-prompts acceptEdits"
            if "--version" in argv:
                return "test CLI"
            if argv[0] == "codex":
                phase = log.name.removeprefix("codex-")
                calls.append("codex-" + phase)
                current_id = f"T-{self.app.state.cycle}"
                data = review("TASK" if phase == "plan" else "CONTINUE",
                              "INITIAL" if phase == "plan" and not self.app.state.current_task_id else "APPROVED",
                              current_id)
                if behavior:
                    data = behavior("codex-" + phase, data, argv, log)
                if phase == "plan" and data["action"] == "TASK":
                    report(self.coord / "TASK.md", task(data["task_id"]))
                report(self.coord / "CODEX_REVIEW.md", data)
                a.write_json(Path(argv[argv.index("--output-last-message") + 1]), data)
                return "{}"
            calls.append("claude")
            self.assertEqual(argv[argv.index("--resume") + 1], self.session)
            self.assertNotIn("--continue", argv)
            self.assertNotIn("--fork-session", argv)
            if behavior:
                returned = behavior("claude", None, argv, log)
                if returned is not None:
                    return returned
            report(self.coord / "RESULT.md", result(self.app.state.current_task_id))
            (self.root / "app.py").write_text(f"VALUE = {self.app.state.cycle}\n")
            return json.dumps({"session_id": self.session, "type": "result", "subtype": "success", "is_error": False})
        patches = [patch.object(a, "execute", side_effect=execute),
                   patch.object(a, "assert_no_claude_process"),
                   patch.object(a.shutil, "which", return_value="/fake/bin")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return calls

    def run_loop(self, rounds=1):
        with redirect_stdout(io.StringIO()):
            return self.app.run(rounds)


class ProtocolTests(Fixture):
    def test_clone_keeps_committed_goal_without_local_configuration(self):
        coord = self.root / ".agent-loop"
        coord.mkdir()
        (coord / "GOAL.md").write_text("# Existing human goal\n")
        self.assertEqual(a.choose_coord(self.root, None), coord)
        self.assertEqual(a.choose_coord(self.root, ".agents"), self.coord)

    def test_config_and_state_round_trip(self):
        self.assertEqual(self.app.config.claude_session, self.session)
        self.app.state.cycle = 8
        self.app.save()
        self.assertEqual(a.State.load(self.coord / "state.json").cycle, 8)

    def test_invalid_configuration_and_state(self):
        for value in (0, -1, "3", True):
            with self.subTest(value=value):
                config = a.asdict(self.app.config)
                config["timeout"] = value
                a.write_json(self.coord / "config.json", config)
                with self.assertRaises(a.LoopError):
                    a.Config.load(self.coord / "config.json")
        data = a.asdict(self.app.state)
        data["status"] = "WAITING_FOR_HUMAN"
        a.write_json(self.coord / "state.json", data)
        with self.assertRaises(a.LoopError):
            a.State.load(self.coord / "state.json")

    def test_atomic_write_failure_preserves_old_state(self):
        path = self.coord / "atomic.json"
        a.write_json(path, {"old": True})
        with patch.object(a.os, "replace", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                a.write_json(path, {"new": True})
        self.assertEqual(a.read_json(path), {"old": True})
        self.assertEqual(list(self.coord.glob(".atomic.json.*")), [])

    def test_atomic_write_rejects_symlink(self):
        path = self.coord / "bad"
        path.symlink_to(self.root / "app.py")
        with self.assertRaises(a.SafetyError):
            a.atomic_write(path, "bad")
        self.assertEqual((self.root / "app.py").read_text(), "VALUE = 0\n")

    def test_task_and_result_validation(self):
        with self.assertRaises(a.LoopError):
            a.validate_task(self.coord / "TASK.md", "T-1")
        report(self.coord / "TASK.md", task())
        self.assertEqual(a.validate_task(self.coord / "TASK.md", "T-1"), task())
        for data in ({}, result("WRONG"), result(status="UNKNOWN"),
                     {**result(), "acceptance_criteria": {}},
                     {**result(), "acceptance_criteria": {"Regression passes": "NOT_TESTED"}},
                     {**result(), "tests": []}):
            with self.subTest(data=data):
                report(self.coord / "RESULT.md", data)
                with self.assertRaises(a.LoopError):
                    a.validate_result(self.coord / "RESULT.md", task())
        report(self.coord / "RESULT.md", result())
        self.assertEqual(a.validate_result(self.coord / "RESULT.md", task())["status"], "COMPLETE")
        (self.coord / "RESULT.md").unlink()
        with self.assertRaises(a.LoopError):
            a.validate_result(self.coord / "RESULT.md", task())
        (self.coord / "RESULT.md").write_text("```json\nBAD\n```\n")
        with self.assertRaises(a.LoopError):
            a.validate_result(self.coord / "RESULT.md", task())

    def test_completion_only_in_final_phase(self):
        data = review("COMPLETE", complete=True)
        a.validate_review(data, "final")
        with self.assertRaises(a.LoopError):
            a.validate_review(data, "review")
        data["completion_checks"]["security"] = False
        with self.assertRaises(a.LoopError):
            a.validate_review(data, "final")

    def test_repair_omitted_nonfinal_checklist_preserves_narrative(self):
        data = review("TASK", "INITIAL")
        header = {k: v for k, v in data.items() if k != "completion_checks"}
        path = self.coord / "CODEX_REVIEW.md"
        report(path, header)
        a.validate_review(data, "plan")
        self.assertTrue(a.reconcile_review(path, data))
        self.assertEqual(a.markdown_data(path), data)
        self.assertIn("Verified in test fixture.", path.read_text())
        self.assertFalse(a.reconcile_review(path, data))

    def test_review_repair_rejects_conflicts_and_missing_identity(self):
        data = review()
        path = self.coord / "CODEX_REVIEW.md"
        for header in ({**data, "verdict": "REJECTED"},
                       {k: v for k, v in data.items() if k != "task_id"},
                       {**data, "unexpected": True}):
            with self.subTest(header=header):
                report(path, header)
                original = path.read_bytes()
                with self.assertRaises(a.LoopError):
                    a.reconcile_review(path, data)
                self.assertEqual(path.read_bytes(), original)

    def test_completion_report_checklist_cannot_be_repaired(self):
        data = review("COMPLETE", complete=True)
        report(self.coord / "CODEX_REVIEW.md", {k:v for k,v in data.items() if k != "completion_checks"})
        a.validate_review(data, "final")
        with self.assertRaises(a.LoopError):
            a.reconcile_review(self.coord / "CODEX_REVIEW.md", data)

    def test_malformed_types_return_protocol_errors(self):
        for key in ("action", "verdict"):
            data = review()
            data[key] = []
            with self.assertRaises(a.LoopError):
                a.validate_review(data, "review")
        data = result()
        data["status"] = {}
        report(self.coord / "RESULT.md", data)
        with self.assertRaises(a.LoopError):
            a.validate_result(self.coord / "RESULT.md", task())
        data = result()
        data["acceptance_criteria"]["Regression passes"] = []
        report(self.coord / "RESULT.md", data)
        with self.assertRaises(a.LoopError):
            a.validate_result(self.coord / "RESULT.md", task())

    def test_existing_session_only(self):
        self.assertEqual(a.discover_sessions(self.root), [self.session])
        for session in ("latest", "friendly-name", str(uuid.uuid4())):
            with self.subTest(session=session), self.assertRaises(a.SafetyError):
                a.session_exists(self.root, session)
        with self.assertRaises(a.SafetyError):
            a.session_exists(self.root.parent, self.session)

    def test_init_no_session_creates_blocked_scaffold_without_cli(self):
        a.session_directory(self.root).joinpath(f"{self.session}.jsonl").unlink()
        other = self.root / ".other-agents"
        with patch.object(a, "execute") as execute, redirect_stdout(io.StringIO()):
            with self.assertRaises(a.SafetyError):
                a.initialize(self.root, other, None)
        execute.assert_not_called()
        self.assertIsNone(a.Config.load(other / "config.json").claude_session)
        self.assertEqual(a.State.load(other / "state.json").status, "BLOCKED")

    def test_init_never_switches_session_or_rewrites_goal(self):
        before = (self.coord / "GOAL.md").read_text()
        with self.assertRaises(a.SafetyError):
            a.initialize(self.root, self.coord, str(uuid.uuid4()))
        self.assertEqual((self.coord / "GOAL.md").read_text(), before)

    def test_commands_and_prompts(self):
        command = a.worker_command(self.session)
        self.assertIn("--resume", command)
        self.assertIn("--restricted", command)
        self.assertIn("none", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        command = a.boss_command(self.root, Path("schema"), Path("output"))
        self.assertEqual(command[:4], ["codex", "-a", "never", "exec"])
        self.assertIn("workspace-write", command)
        self.assertIn("independently", a.prompt("boss", coord=".agents", phase="plan", context="{}"))
        worker = a.prompt("worker", coord=".agents", task_id="T-1")
        self.assertIn('"task_id":"T-1"', worker)
        self.assertIn("Do not ask", worker)

    def test_project_interpreter_policy_is_explicit_and_validated(self):
        command = a.worker_command(self.session, "pd24/.venv/bin/python")
        self.assertIn("Bash(pd24/.venv/bin/python *)", command[command.index("--allowedTools") + 1])
        for value in ("/usr/bin/python", "../python", "python3", "venv/python;sudo", "venv/python *", []):
            with self.subTest(value=value), self.assertRaises(a.SafetyError):
                a.worker_command(self.session, value)
        config = a.asdict(self.app.config)
        config["worker_python"] = "pd24/.venv/bin/python"
        a.write_json(self.coord / "config.json", config)
        self.assertEqual(a.Config.load(self.coord / "config.json").worker_python, config["worker_python"])

    def test_goal_drift_and_symlink_guard(self):
        (self.coord / "GOAL.md").write_text("new goal")
        with self.assertRaises(a.SafetyError):
            self.app.guard()

    def test_lock_refuses_second_owner(self):
        with a.locked(self.coord / "test.lock"):
            with self.assertRaises(a.SafetyError):
                with a.locked(self.coord / "test.lock"):
                    self.fail("acquired twice")


class ProcessTests(Fixture):
    def test_process_scan_refuses_existing_same_user_claude(self):
        proc = Path(self.tmp.name) / "proc"
        entry = proc / "99999999"
        entry.mkdir(parents=True)
        (entry / "comm").write_text("claude\n")
        (entry / "cmdline").write_bytes(b"claude\0--resume\0existing\0")
        with patch.object(a, "Path", side_effect=lambda value: proc if value == "/proc" else Path(value)):
            with self.assertRaisesRegex(a.SafetyError, "Existing Claude process"):
                a.assert_no_claude_process()
        (entry / "comm").write_text("python3\n")
        (entry / "cmdline").write_bytes(b"python3\0unrelated.py\0")
        with patch.object(a, "Path", side_effect=lambda value: proc if value == "/proc" else Path(value)):
            a.assert_no_claude_process()

    def test_missing_binary_nonzero_timeout(self):
        for args, limit in ((["/no/such/executable"], 1),
                            ([sys.executable, "-c", "import sys; print('out'); print('err',file=sys.stderr); sys.exit(3)"], 5),
                            ([sys.executable, "-c", "import time; time.sleep(20)"], 1)):
            with self.subTest(args=args), self.assertRaises(a.LoopError):
                a.execute(args, self.root, limit, log=self.coord / "test")
        self.assertEqual(a.read_json(self.coord / "test.metadata.json")["failure"], "timeout")

    def test_output_separation_and_redaction(self):
        log = self.coord / "process"
        with patch.dict(os.environ, {"FAKE_API_KEY": "unique-secret-test-value"}):
            text = a.execute([sys.executable, "-c",
                              "import sys; print('unique-secret-test-value'); print('error text',file=sys.stderr)"],
                             self.root, 5, log=log)
        self.assertIn("unique-secret-test-value", text)
        self.assertEqual(log.with_suffix(".stdout.txt").read_text(), "[REDACTED]\n")
        self.assertEqual(log.with_suffix(".stderr.txt").read_text(), "error text\n")


class LoopTests(Fixture):
    def test_missing_planning_checklist_reaches_worker_and_is_audited(self):
        self.fake_agents()
        original_execute = a.execute
        def omit_header(*args, **kwargs):
            output = original_execute(*args, **kwargs)
            argv = args[0]
            log = args[4] if len(args) > 4 else kwargs.get("log")
            if argv[0] == "codex" and log and log.name == "codex-plan":
                path = self.coord / "CODEX_REVIEW.md"
                data = a.markdown_data(path)
                del data["completion_checks"]
                report(path, data)
            return output
        with patch.object(a, "execute", side_effect=omit_header):
            self.assertEqual(self.run_loop(), 0)
        self.assertEqual(self.app.state.last_claude_status, "COMPLETE")
        repairs = list((self.coord / "runs").glob("*/codex-plan-protocol-repair.json"))
        self.assertEqual(len(repairs), 1)

    def test_one_cycle_has_post_worker_review(self):
        calls = self.fake_agents()
        self.assertEqual(self.run_loop(), 0)
        self.assertEqual(calls, ["codex-plan", "claude", "codex-review"])
        state = a.State.load(self.coord / "state.json")
        self.assertEqual(state.status, "STOPPED")
        self.assertFalse(state.pending_review)
        self.assertEqual(state.last_claude_status, "COMPLETE")
        self.assertEqual(state.last_codex_verdict, "APPROVED")

    def test_two_rounds_reuse_identical_session(self):
        calls = self.fake_agents()
        self.assertEqual(self.run_loop(2), 0)
        self.assertEqual(calls.count("claude"), 2)
        self.assertEqual(self.app.state.cycle, 2)
        self.assertEqual(self.app.state.claude_session, self.session)

    def test_max_round_validation(self):
        with self.assertRaises(a.LoopError):
            self.run_loop(0)

    def test_dry_run_never_calls_models(self):
        calls = self.fake_agents()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.app.run(1, dry_run=True), 0)
        self.assertEqual(calls, [])
        self.assertEqual(self.app.state.cycle, 0)

    def test_doctor_does_not_prompt(self):
        calls = self.fake_agents()
        self.assertEqual(self.app.preflight(), [])
        self.assertEqual(calls, [])

    def test_missing_session_stops_before_any_model(self):
        calls = self.fake_agents()
        a.session_directory(self.root).joinpath(f"{self.session}.jsonl").unlink()
        with self.assertRaises(a.SafetyError):
            self.run_loop()
        self.assertEqual(calls, [])
        self.assertEqual(self.app.state.status, "BLOCKED")

    def test_active_claude_refuses_preflight(self):
        calls = self.fake_agents()
        with patch.object(a, "assert_no_claude_process", side_effect=a.SafetyError("active Claude")):
            with self.assertRaises(a.SafetyError):
                self.run_loop()
        self.assertEqual(calls, [])

    def test_session_mismatch_stops_and_still_reviews(self):
        def behavior(kind, data, argv, log):
            return json.dumps({"session_id": str(uuid.uuid4())}) if kind == "claude" else data
        calls = self.fake_agents(behavior)
        self.assertEqual(self.run_loop(3), 2)
        self.assertEqual(calls, ["codex-plan", "claude", "codex-review"])
        self.assertEqual(self.app.state.status, "ERROR")
        self.assertIn("session ID", self.app.state.last_error)

    def test_resume_error_has_no_fallback(self):
        def behavior(kind, data, argv, log):
            if kind == "claude":
                a.atomic_write(log.with_suffix(".stderr.txt"), "No conversation found with session ID")
                raise a.LoopError("claude exited 1")
            return data
        calls = self.fake_agents(behavior)
        self.assertEqual(self.run_loop(3), 2)
        self.assertEqual(calls.count("claude"), 1)
        self.assertIn("resume failed", self.app.state.last_error)

    def test_malformed_output_fails_closed(self):
        def behavior(kind, data, argv, log):
            return "not json" if kind == "claude" else data
        self.fake_agents(behavior)
        self.assertEqual(self.run_loop(), 2)
        self.assertIn("malformed", self.app.state.last_error)

    def test_stale_result_removed_and_failure_reviewed(self):
        report(self.coord / "RESULT.md", result())
        def behavior(kind, data, argv, log):
            if kind == "claude":
                return json.dumps({"session_id": self.session, "type": "result", "subtype": "success"})
            if kind == "codex-review":
                data["verdict"] = "NEEDS_FOLLOWUP"
            return data
        calls = self.fake_agents(behavior)
        self.assertEqual(self.run_loop(), 1)
        self.assertIn("codex-review", calls)
        self.assertEqual(self.app.state.consecutive_failures, 1)
        self.assertFalse((self.coord / "RESULT.md").exists())

    def test_failure_retry_and_limit(self):
        def behavior(kind, data, argv, log):
            if kind == "codex-plan":
                raise a.LoopError("boss timed out")
            return data
        calls = self.fake_agents(behavior)
        self.app.limits["max_consecutive_failures"] = 2
        self.assertEqual(self.run_loop(5), 2)
        self.assertEqual(calls, ["codex-plan", "codex-plan"])
        self.assertEqual(self.app.state.consecutive_failures, 2)

    def test_autonomous_correction_resets_failure_count(self):
        def behavior(kind, data, argv, log):
            if kind == "codex-review" and self.app.state.cycle == 1:
                data["verdict"] = "REJECTED"
            return data
        calls = self.fake_agents(behavior)
        self.assertEqual(self.run_loop(2), 0)
        self.assertEqual(calls.count("claude"), 2)
        self.assertEqual(self.app.state.consecutive_failures, 0)

    def test_corrective_task_can_reuse_id_with_fresh_worker_result(self):
        def behavior(kind, data, argv, log):
            if kind == "codex-plan" and self.app.state.cycle == 2:
                data["task_id"] = "T-1"
                data["verdict"] = "NEEDS_FOLLOWUP"
            elif kind == "codex-review":
                data["task_id"] = "T-1"
                if self.app.state.cycle == 1:
                    data["verdict"] = "REJECTED"
            return data
        calls = self.fake_agents(behavior)
        self.assertEqual(self.run_loop(2), 0)
        self.assertEqual(calls.count("claude"), 2)
        self.assertEqual(self.app.state.issue_attempts["bug"], 2)
        self.assertEqual(self.app.state.consecutive_failures, 0)
        self.assertTrue(list((self.coord / "runs").glob("00002-*/previous-result.md")))

    def test_approved_task_cannot_silently_replay_same_id(self):
        def behavior(kind, data, argv, log):
            if kind == "codex-plan" and self.app.state.cycle == 2:
                data["task_id"] = "T-1"
            return data
        calls = self.fake_agents(behavior)
        self.assertEqual(self.run_loop(2), 1)
        self.assertEqual(calls.count("claude"), 1)
        self.assertIn("explicit corrective verdict", self.app.state.last_error)

    def test_loop_detection_requires_new_approach_and_bounds_corrections(self):
        self.app.state.fingerprints = ["same", "same"]
        with self.assertRaises(a.LoopError):
            self.app.detect_loop(task(), review())
        data = review()
        data["recovery_strategy"] = "Replace polling with a bounded event wait after tracing the race"
        self.app.detect_loop(task(), data)
        self.assertIn("bounded event wait", (self.coord / "MEMORY.md").read_text())
        self.app.state.issue_attempts["bug"] = 4
        with self.assertRaises(a.SafetyError):
            self.app.detect_loop(task(), data)

    def test_oscillation_is_detected(self):
        self.app.state.fingerprints = ["a", "b", "a", "b"]
        with self.assertRaises(a.LoopError):
            self.app.detect_loop(task(), review())

    def test_dirty_user_work_and_index_are_preserved(self):
        (self.root / "notes.txt").write_text("user notes\n")
        (self.root / "staged.txt").write_text("staged user work\n")
        a.git(self.root, "add", "staged.txt")
        before = a.git(self.root, "diff", "--cached")
        self.fake_agents()
        self.assertEqual(self.run_loop(), 0)
        self.assertEqual((self.root / "notes.txt").read_text(), "user notes\n")
        self.assertEqual(a.git(self.root, "diff", "--cached"), before)
        backups = list((self.coord / "runs").glob("*/baseline-files/notes.txt"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "user notes\n")
        self.assertEqual(a.git(self.root, "rev-list", "--count", "HEAD").strip(), "1")

    def test_agent_git_index_change_stops(self):
        def behavior(kind, data, argv, log):
            if kind == "claude":
                (self.root / "bad.txt").write_text("bad")
                a.git(self.root, "add", "bad.txt")
            return data
        self.fake_agents(behavior)
        self.assertEqual(self.run_loop(), 2)
        self.assertIn("Git HEAD/index", self.app.state.last_error)

    def test_worker_cannot_change_acceptance_criteria(self):
        def behavior(kind, data, argv, log):
            if kind == "claude":
                report(self.coord / "TASK.md", task("T-1", issue="changed"))
            return data
        self.fake_agents(behavior)
        self.assertEqual(self.run_loop(), 2)
        self.assertIn("protected coordination file: TASK.md", self.app.state.last_error)

    def test_git_operation_blocks_before_model_calls(self):
        calls = self.fake_agents()
        (self.root / ".git" / "MERGE_HEAD").write_text("fake")
        with self.assertRaises(a.SafetyError):
            self.run_loop()
        self.assertEqual(calls, [])

    def test_dedicated_final_review(self):
        (self.coord / "BACKLOG.md").write_text("# Backlog\n- [x] P1 Done\n")
        def behavior(kind, data, argv, log):
            if kind == "codex-review":
                data["action"] = "FINAL_REVIEW"
            elif kind == "codex-final":
                data = review("COMPLETE", task_id=self.app.state.current_task_id, complete=True)
            return data
        calls = self.fake_agents(behavior)
        self.assertEqual(self.run_loop(), 0)
        self.assertEqual(calls[-1], "codex-final")
        self.assertEqual(self.app.state.status, "COMPLETE")

    def test_p0_p1_prevents_completion(self):
        def behavior(kind, data, argv, log):
            if kind == "codex-review":
                data["action"] = "FINAL_REVIEW"
            elif kind == "codex-final":
                return review("COMPLETE", complete=True)
            return data
        self.fake_agents(behavior)
        self.assertEqual(self.run_loop(), 1)
        self.assertNotEqual(self.app.state.status, "COMPLETE")

    def test_interrupt_preserves_pending_review(self):
        def behavior(kind, data, argv, log):
            if kind == "claude":
                raise KeyboardInterrupt()
            return data
        self.fake_agents(behavior)
        self.assertEqual(self.run_loop(), 130)
        state = a.State.load(self.coord / "state.json")
        self.assertEqual(state.status, "STOPPED")
        self.assertTrue(state.pending_review)

    def test_crash_restart_reviews_before_new_worker(self):
        self.app.state.current_task_id = "T-0"
        self.app.state.pending_review = True
        self.app.save()
        calls = self.fake_agents()
        # Recovery review should refer to the old task; the next plan creates T-1.
        original_boss = self.app.boss
        def boss(phase, run):
            if phase == "review" and self.app.state.current_task_id == "T-0":
                calls.append("recovery-review")
                self.app.state.pending_review = False
                return review(task_id="T-0")
            return original_boss(phase, run)
        with patch.object(self.app, "boss", side_effect=boss):
            self.assertEqual(self.run_loop(), 0)
        self.assertEqual(calls[:2], ["recovery-review", "codex-plan"])


class SubprocessIntegrationTests(Fixture):
    def test_real_cli_boundary_with_fake_model_executables(self):
        """Real CLI subprocesses; isolate host ownership from unrelated live Claude."""
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        stub = '''import json, pathlib, sys
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
if "--version" in args:
    print("stub 1.0")
    sys.exit(0)
if "--help" in args:
    print("--sandbox --output-schema --output-last-message --json --resume --print --output-format --restricted --permission-prompts acceptEdits")
    sys.exit(0)
coord = pathlib.Path(".agents")
text = sys.stdin.read()
def report(name, data):
    (coord / name).write_text("```json\\n" + json.dumps(data) + "\\n```\\n")
if name == "codex":
    phase = "plan" if "Phase: plan." in text else "review"
    data = {"action":"TASK" if phase == "plan" else "CONTINUE", "verdict":"INITIAL" if phase == "plan" else "APPROVED", "task_id":"T-live-stub", "summary":"Verified actual file", "evidence":["Inspected app.py"], "recovery_strategy":"", "completion_checks":{k:False for k in ("goal","functionality","tests","security","p0_p1","architecture","docs","runtime","assumptions","git")}}
    if phase == "plan":
        report("TASK.md", {"task_id":"T-live-stub", "issue_key":"test", "objective":"Test subprocess boundary", "acceptance_criteria":["File updated"]})
    else:
        assert pathlib.Path("app.py").read_text() == "VALUE = 42\\n"
    report("CODEX_REVIEW.md", data)
    pathlib.Path(args[args.index("--output-last-message")+1]).write_text(json.dumps(data))
    print(json.dumps({"type":"test_event"}))
else:
    session = args[args.index("--resume")+1]
    assert "T-live-stub" in text
    pathlib.Path("app.py").write_text("VALUE = 42\\n")
    report("RESULT.md", {"task_id":"T-live-stub", "status":"COMPLETE", "acceptance_criteria":{"File updated":"PASS"}, "tests":["Inspected app.py"]})
    print(json.dumps({"type":"result", "subtype":"success", "session_id":session, "is_error":False}))
'''
        for name in ("codex", "claude"):
            path = bindir / name
            path.write_text(f"#!{sys.executable}\n" + stub)
            path.chmod(0o700)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
        # Fake model executables cannot share a real Claude session. Keep the
        # production ownership guard unchanged; exercise it separately above.
        bootstrap = (
            "import sys\nfrom unittest.mock import patch\n"
            f"sys.path.insert(0, {str(Path(a.__file__).resolve().parent.parent)!r})\n"
            "from tools import agent_loop\n"
            "with patch.object(agent_loop, 'assert_no_claude_process'):\n"
            "    raise SystemExit(agent_loop.main())\n"
        )
        command = [sys.executable, "-c", bootstrap, "--root", str(self.root),
                   "run", "--rounds", "1", "--timeout", "30"]
        finished = subprocess.run(command, env=env, text=True, capture_output=True, timeout=40)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertEqual(a.State.load(self.coord / "state.json").last_codex_verdict, "APPROVED")
        runs = list((self.coord / "runs").iterdir())
        self.assertEqual(len(runs), 1)
        self.assertTrue((runs[0] / "claude.stdout.txt").is_file())
        self.assertTrue((runs[0] / "codex-review.metadata.json").is_file())
        self.assertEqual((self.root / "app.py").read_text(), "VALUE = 42\n")


if __name__ == "__main__":
    unittest.main()
