#!/usr/bin/env python3
"""Sequential Codex supervisor / persistent Claude worker. Python 3.11+, Linux."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from typing import Any, Iterator


class LoopError(Exception):
    """Recoverable agent/protocol failure."""


class SafetyError(LoopError):
    """Fail closed: session, ownership, goal or repository invariant violated."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, text: str) -> None:
    """Same-filesystem replacement; fsync both data and directory before returning."""
    if path.is_symlink():
        raise SafetyError(f"Refusing symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise LoopError(f"Cannot read valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise LoopError(f"Expected JSON object: {path}")
    return value


def markdown_data(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text()
        match = re.search(r"^```json\s*\n(.*?)\n```", text, re.M | re.S)
        data = json.loads(match.group(1)) if match else None
    except (OSError, ValueError) as exc:
        raise LoopError(f"Missing/malformed report: {path.name}") from exc
    if not isinstance(data, dict):
        raise LoopError(f"Missing JSON header: {path.name}")
    return data


@dataclass
class Config:
    schema_version: int = 1
    claude_session: str | None = None
    timeout: int = 900
    max_consecutive_failures: int = 3
    max_corrections_per_task: int = 3

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            config = cls(**read_json(path))
        except TypeError as exc:
            raise LoopError("Invalid configuration fields") from exc
        if config.schema_version != 1:
            raise LoopError("Unsupported configuration version")
        for key in ("timeout", "max_consecutive_failures", "max_corrections_per_task"):
            if type(getattr(config, key)) is not int or getattr(config, key) < 1:
                raise LoopError(f"{key} must be a positive integer")
        if config.claude_session is not None:
            canonical_session(config.claude_session)
        return config


@dataclass
class State:
    schema_version: int = 1
    status: str = "STOPPED"
    cycle: int = 0
    claude_session: str | None = None
    current_task_id: str | None = None
    last_successful_codex_turn: str | None = None
    last_successful_claude_turn: str | None = None
    last_reviewed_commit: str | None = None
    last_codex_verdict: str = "INITIAL"
    last_claude_status: str | None = None
    latest_tests: list[str] = field(default_factory=list)
    consecutive_failures: int = 0
    repeated_task_count: int = 0
    issue_attempts: dict[str, int] = field(default_factory=dict)
    fingerprints: list[str] = field(default_factory=list)
    review_failures: list[str] = field(default_factory=list)
    pending_review: bool = False
    started_at: str | None = None
    updated_at: str | None = None
    blocker: str | None = None
    last_error: str | None = None
    loop_warning: str | None = None
    goal_sha256: str | None = None

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            state = cls(**read_json(path))
        except TypeError as exc:
            raise LoopError("Invalid state fields") from exc
        if state.schema_version != 1 or state.status not in {
            "RUNNING", "PAUSED", "STOPPED", "COMPLETE", "ERROR", "BLOCKED"
        }:
            raise LoopError("Unsupported state version/status")
        for key in ("cycle", "consecutive_failures", "repeated_task_count"):
            if type(getattr(state, key)) is not int or getattr(state, key) < 0:
                raise LoopError(f"Invalid state counter: {key}")
        if not isinstance(state.issue_attempts, dict) or any(
            not isinstance(k, str) or type(v) is not int or v < 0
            for k, v in state.issue_attempts.items()
        ):
            raise LoopError("Invalid issue counters")
        for key in ("fingerprints", "review_failures", "latest_tests"):
            if not isinstance(getattr(state, key), list) or not all(
                isinstance(v, str) for v in getattr(state, key)
            ):
                raise LoopError(f"Invalid state list: {key}")
        if type(state.pending_review) is not bool:
            raise LoopError("Invalid pending review flag")
        return state

    def save(self, path: Path) -> None:
        self.updated_at = now()
        write_json(path, asdict(self))


def canonical_session(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SafetyError("Claude session must be an existing explicit UUID; no latest/fork fallback") from exc


def session_directory(root: Path) -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    return base / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(root.resolve()))


def session_exists(root: Path, session: str) -> Path:
    session = canonical_session(session)
    path = session_directory(root) / f"{session}.jsonl"
    if not path.is_file() or path.is_symlink():
        raise SafetyError(f"Existing Claude transcript unavailable for this project: {session}")
    # Only inspect project-associated metadata. Never print conversation content.
    try:
        with path.open() as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(entry, dict) and entry.get("sessionId") == session
                        and entry.get("type") in {"user", "assistant"}
                        and isinstance(entry.get("cwd"), str)
                        and Path(entry.get("cwd", "")).resolve() == root.resolve()):
                    return path
    except OSError as exc:
        raise SafetyError("Cannot read existing project session metadata") from exc
    raise SafetyError("Session has no matching project conversation; refusing to create one")


def discover_sessions(root: Path) -> list[str]:
    found = []
    for path in sorted(session_directory(root).glob("*.jsonl")):
        try:
            session_exists(root, path.stem)
            found.append(path.stem)
        except LoopError:
            pass
    return found


@contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SafetyError(f"Another orchestrator owns {path.name}") from exc
        yield
    finally:
        os.close(fd)


def assert_no_claude_process() -> None:
    """Conservatively refuse any other same-user Claude writer (including other cwd)."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            comm = (entry / "comm").read_text().strip().lower()
            args = (entry / "cmdline").read_bytes().split(b"\0")
            names = [Path(os.fsdecode(a)).name.lower() for a in args[:2] if a]
            if "claude" in comm or any(n in {"claude", "claude.js"} for n in names):
                raise SafetyError(f"Existing Claude process PID {entry.name}; session ownership is not exclusive")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise SafetyError("Cannot establish exclusive Claude process ownership") from exc


def redact(text: str) -> str:
    # Never dump the environment; redact any known credential value incidentally emitted.
    for key, value in os.environ.items():
        if re.search(r"TOKEN|SECRET|PASSWORD|API_KEY", key, re.I) and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}", "[REDACTED]", text)
    return text


def execute(argv: list[str], root: Path, timeout: int, prompt: str = "",
            log: Path | None = None) -> str:
    """No shell or PTY; terminate the entire subprocess group on timeout/interrupt."""
    started = now()
    output = error = ""
    code = None
    failure = None
    try:
        process = subprocess.Popen(argv, cwd=root, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, errors="replace", start_new_session=True)
        try:
            output, error = process.communicate(prompt, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                output, error = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                output, error = process.communicate()
            raise
        code = process.returncode
        if code:
            raise LoopError(f"{Path(argv[0]).name} exited {code}; see separate stderr log")
        return output
    except FileNotFoundError as exc:
        failure = "missing executable"
        raise LoopError(f"Executable unavailable: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        failure = "timeout"
        raise LoopError(f"{Path(argv[0]).name} timed out after {timeout}s") from exc
    except (LoopError, KeyboardInterrupt) as exc:
        failure = str(exc) or "interrupted"
        raise
    finally:
        if log:
            atomic_write(log.with_suffix(".stdout.txt"), redact(output))
            atomic_write(log.with_suffix(".stderr.txt"), redact(error))
            write_json(log.with_suffix(".metadata.json"), {
                "started_at": started, "ended_at": now(), "returncode": code,
                "failure": failure, "executable": Path(argv[0]).name,
            })


def git(root: Path, *args: str) -> str:
    return execute(["git", "-C", str(root), *args], root, 20)


def check_git(root: Path) -> None:
    try:
        actual = Path(git(root, "rev-parse", "--show-toplevel").strip()).resolve()
    except LoopError as exc:
        raise SafetyError("Workspace is not a valid Git repository; no automatic initialization") from exc
    if actual != root:
        raise SafetyError(f"Use repository root {actual}")
    if git(root, "ls-files", "-u").strip():
        raise SafetyError("Unresolved Git conflicts")
    for marker in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        path = Path(git(root, "rev-parse", "--git-path", marker).strip())
        if (root / path).exists():
            raise SafetyError(f"In-progress Git operation: {marker}")


def safe_source(path: str, coord: Path, root: Path) -> bool:
    parts = Path(path).parts
    return bool(parts) and not any(
        p in {".git", ".aws", ".ssh", ".codex", "__pycache__", "node_modules", ".venv"}
        or p.startswith(".env") or re.search(r"secret|credential|\.pem$|\.key$", p, re.I)
        for p in parts
    ) and not (root / path).is_relative_to(coord)


def snapshot(root: Path, coord: Path) -> dict[str, Any]:
    paths = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    hashes = {}
    for name in sorted(set(p for p in paths if p and safe_source(p, coord, root))):
        path = root / name
        if path.is_symlink():
            hashes[name] = "symlink:" + os.readlink(path)
        elif path.is_file() and path.resolve().is_relative_to(root):
            with path.open("rb") as stream:
                hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        else:
            hashes[name] = "missing"
    try:
        head = git(root, "rev-parse", "HEAD").strip()
    except LoopError:
        head = None  # unborn repository is permitted
    return {"head": head, "status": git(root, "status", "--short"), "files": hashes,
            "fingerprint": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}


def audit(root: Path, coord: Path, run: Path, label: str) -> dict[str, Any]:
    data = snapshot(root, coord)
    write_json(run / f"{label}-git.json", data)
    # Explicit safe paths exclude ignored secrets; no git add/reset/commit is ever used.
    names = list(data["files"])
    diff = git(root, "diff", "--no-ext-diff", "--no-textconv", "--", *names) if names else ""
    staged = git(root, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--", *names) if names else ""
    atomic_write(run / f"{label}-worktree.patch", redact(diff))
    atomic_write(run / f"{label}-index.patch", redact(staged))
    if label == "baseline":
        # Recover untracked/user-modified files too, without changing the index.
        changed = set(git(root, "diff", "--name-only", "-z").split("\0"))
        changed.update(git(root, "diff", "--cached", "--name-only", "-z").split("\0"))
        changed.update(git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
        for name in changed:
            path = root / name
            if name in data["files"] and path.is_file() and not path.is_symlink():
                target = run / "baseline-files" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                # Baseline bytes stay local, private and unmodified for recovery.
                shutil.copyfile(path, target)
                target.chmod(0o600)
    return data


CHECKS = ("goal", "functionality", "tests", "security", "p0_p1", "architecture", "docs", "runtime", "assumptions", "git")
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["TASK", "CONTINUE", "FINAL_REVIEW", "COMPLETE", "BLOCKED"]},
        "verdict": {"type": "string", "enum": ["INITIAL", "APPROVED", "REJECTED", "NEEDS_FOLLOWUP"]},
        "task_id": {"type": "string"}, "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "recovery_strategy": {"type": "string"},
        "completion_checks": {"type": "object", "additionalProperties": False,
                              "properties": {k: {"type": "boolean"} for k in CHECKS},
                              "required": list(CHECKS)},
    },
    "required": ["action", "verdict", "task_id", "summary", "evidence", "recovery_strategy", "completion_checks"],
}


def boss_command(root: Path, schema: Path, output: Path) -> list[str]:
    return ["codex", "-a", "never", "exec", "--sandbox", "workspace-write",
            "--cd", str(root), "--json", "--output-schema", str(schema),
            "--output-last-message", str(output), "-"]


def worker_command(session: str) -> list[str]:
    return ["claude", "--resume", canonical_session(session), "--print", "--output-format", "json",
            "--permission-mode", "acceptEdits", "--permission-prompts", "none",
            "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "", "--disable-slash-commands", "--no-chrome",
            "--tools", "Read,Edit,Write,Glob,Grep,Bash",
            "--allowedTools", "Read,Edit,Write,Bash(python3 -m unittest *)",
            "--settings", json.dumps({"disableAllHooks": True})]


def prompt(kind: str, **values: Any) -> str:
    path = Path(__file__).parent / "agent_prompts" / f"{kind}.md"
    return path.read_text().format(**values)


def validate_task(path: Path, task_id: str) -> dict[str, Any]:
    data = markdown_data(path)
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", task_id):
        raise LoopError("Invalid task ID")
    if data.get("task_id") != task_id:
        raise LoopError("TASK task ID mismatch")
    for key in ("issue_key", "objective"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise LoopError(f"TASK requires {key}")
    criteria = data.get("acceptance_criteria")
    if (not isinstance(criteria, list) or not criteria
            or not all(isinstance(v, str) and v.strip() for v in criteria)
            or len(criteria) != len(set(criteria))):
        raise LoopError("TASK requires unique nonempty acceptance criteria")
    return data


def validate_result(path: Path, task: dict[str, Any]) -> dict[str, Any]:
    data = markdown_data(path)
    if data.get("task_id") != task["task_id"]:
        raise LoopError("RESULT task ID mismatch")
    if not isinstance(data.get("status"), str) or data["status"] not in {"COMPLETE", "PARTIAL", "BLOCKED"}:
        raise LoopError("Invalid RESULT status")
    criteria = data.get("acceptance_criteria")
    if (not isinstance(criteria, dict) or set(criteria) != set(task["acceptance_criteria"])
            or any(not isinstance(v, str) or v not in {"PASS", "FAIL", "NOT_TESTED"} for v in criteria.values())):
        raise LoopError("RESULT must represent every acceptance criterion")
    tests = data.get("tests")
    if not isinstance(tests, list) or not all(isinstance(v, str) and v.strip() for v in tests):
        raise LoopError("RESULT tests must be a list of evidence strings")
    if data["status"] == "COMPLETE" and (not tests or any(v != "PASS" for v in criteria.values())):
        raise LoopError("RESULT COMPLETE requires all criteria PASS and test evidence")
    return data


def validate_review(data: dict[str, Any], phase: str) -> None:
    if set(data) != set(REVIEW_SCHEMA["required"]):
        raise LoopError("Missing/unknown Codex decision fields")
    actions = {"plan": {"TASK", "FINAL_REVIEW", "BLOCKED"},
               "review": {"CONTINUE", "FINAL_REVIEW", "BLOCKED"},
               "final": {"COMPLETE", "CONTINUE", "BLOCKED"}}
    if (not isinstance(data["action"], str) or not isinstance(data["verdict"], str)
            or data["action"] not in actions[phase]
            or data["verdict"] not in {"INITIAL", "APPROVED", "REJECTED", "NEEDS_FOLLOWUP"}):
        raise LoopError("Invalid Codex action/verdict for phase")
    if not all(isinstance(data[k], str) for k in ("task_id", "summary", "recovery_strategy")):
        raise LoopError("Invalid Codex string fields")
    if not isinstance(data["evidence"], list) or not data["evidence"] or not all(
        isinstance(v, str) and v.strip() for v in data["evidence"]
    ):
        raise LoopError("Codex decision lacks independent evidence")
    checks = data["completion_checks"]
    if not isinstance(checks, dict) or set(checks) != set(CHECKS) or any(type(v) is not bool for v in checks.values()):
        raise LoopError("Invalid completion checklist")
    if data["action"] == "COMPLETE" and (not all(checks.values()) or data["verdict"] != "APPROVED"):
        raise LoopError("COMPLETE requires a dedicated approved final review and all checks")


def open_priorities(coord: Path) -> list[str]:
    return [line for line in (coord / "BACKLOG.md").read_text().splitlines()
            if re.match(r"\s*- \[ \] P[01]\b", line)]


class Orchestrator:
    def __init__(self, root: Path, coord: Path):
        self.root, self.coord = root.resolve(), coord.resolve()
        if not self.coord.is_relative_to(self.root) or self.coord == self.root:
            raise SafetyError("Coordination directory must be inside the repository")
        self.config = Config.load(coord / "config.json")
        self.state = State.load(coord / "state.json")
        self.limits: dict[str, int] = {}

    def limit(self, key: str) -> int:
        return self.limits.get(key, getattr(self.config, key))

    def save(self) -> None:
        self.state.save(self.coord / "state.json")

    def guard(self) -> None:
        for name in ("GOAL.md", "config.json", "state.json", "TASK.md", "RESULT.md", "CODEX_REVIEW.md", "runs"):
            if (self.coord / name).is_symlink():
                raise SafetyError(f"Unsafe coordination symlink: {name}")
        goal = hashlib.sha256((self.coord / "GOAL.md").read_bytes()).hexdigest()
        if goal != self.state.goal_sha256:
            raise SafetyError("Authoritative GOAL.md changed; refusing autonomous goal drift")
        if Config.load(self.coord / "config.json") != self.config:
            raise SafetyError("Configuration changed during operation")
        if self.state.claude_session != self.config.claude_session:
            raise SafetyError("Session identity mismatch between state and configuration")

    def git_identity(self) -> tuple[str, str]:
        try:
            head = git(self.root, "rev-parse", "--verify", "HEAD").strip()
        except LoopError:
            head = "unborn"
        index = git(self.root, "ls-files", "--stage", "-z")
        return head, hashlib.sha256(index.encode()).hexdigest()

    def protected_files(self, worker: bool = False) -> dict[str, bytes]:
        names = ["GOAL.md", "config.json", "state.json"]
        if worker:
            names += ["TASK.md", "CODEX_REVIEW.md", "BACKLOG.md"]
        return {name: (self.coord / name).read_bytes() for name in names}

    def verify_protected(self, before: dict[str, bytes]) -> None:
        for name, content in before.items():
            path = self.coord / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise SafetyError(f"Agent modified protected coordination file: {name}")

    def preflight(self) -> list[str]:
        errors = []
        for check in (lambda: check_git(self.root), self.guard):
            try:
                check()
            except (LoopError, OSError) as exc:
                errors.append(str(exc))
        for executable, args, flags in (
            ("codex", ["exec", "--help"], ["--sandbox", "--output-schema", "--output-last-message", "--json"]),
            ("claude", ["--help"], ["--resume", "--print", "--output-format", "--restricted", "--permission-prompts", "acceptEdits"]),
        ):
            try:
                if not shutil.which(executable):
                    raise LoopError(f"Missing executable: {executable}")
                help_text = execute([executable, *args], self.root, 20)
                missing = [f for f in flags if f not in help_text]
                if missing:
                    raise LoopError(f"{executable} missing supported flags: {missing}")
            except LoopError as exc:
                errors.append(str(exc))
        try:
            if not self.config.claude_session:
                raise SafetyError("No existing Claude session configured; never creating a new conversation")
            session_exists(self.root, self.config.claude_session)
            assert_no_claude_process()
        except LoopError as exc:
            errors.append(str(exc))
        for name in ("GOAL.md", "MEMORY.md", "ASSUMPTIONS.md", "BACKLOG.md", "RESEARCH.md"):
            if not (self.coord / name).is_file():
                errors.append(f"Missing {name}")
        try:
            self.coord.joinpath("runs").mkdir(exist_ok=True)
            with tempfile.TemporaryFile(dir=self.coord), tempfile.TemporaryFile(dir=self.coord / "runs"):
                pass
        except OSError as exc:
            errors.append(f"State/log directory not writable: {exc.strerror}")
        return errors

    def boss(self, phase: str, run: Path) -> dict[str, Any]:
        self.guard()
        check_git(self.root)
        context = json.dumps({"cycle": self.state.cycle, "task_id": self.state.current_task_id,
                              "pending_review": self.state.pending_review,
                              "last_error": self.state.last_error,
                              "loop_warning": self.state.loop_warning})
        text = prompt("boss", coord=self.coord.relative_to(self.root), phase=phase, context=context)
        atomic_write(run / f"codex-{phase}-prompt.txt", text)
        schema, output = run / "review-schema.json", run / f"codex-{phase}-decision.json"
        write_json(schema, REVIEW_SCHEMA)
        # Remove stale protocol files only after archiving them in this run.
        review = self.coord / "CODEX_REVIEW.md"
        if review.exists():
            atomic_write(run / f"before-{phase}-review.md", review.read_text())
        if phase == "plan" and (self.coord / "TASK.md").exists():
            atomic_write(run / "previous-task.md", (self.coord / "TASK.md").read_text())
        git_identity = self.git_identity()
        protected = self.protected_files()
        try:
            execute(boss_command(self.root, schema, output), self.root, self.limit("timeout"),
                    text, run / f"codex-{phase}")
        finally:
            self.verify_protected(protected)
            self.guard()
            if self.git_identity() != git_identity:
                raise SafetyError("Codex changed Git HEAD/index; preserve evidence and stop")
        self.guard()
        data = read_json(output)
        validate_review(data, phase)
        if markdown_data(review) != data:
            raise LoopError("CODEX_REVIEW.md must match the structured decision")
        if self.state.pending_review and data["verdict"] == "INITIAL":
            raise LoopError("INITIAL cannot accept an existing worker attempt")
        if phase == "review" and data["task_id"] != self.state.current_task_id:
            raise LoopError("Codex reviewed a different task")
        atomic_write(run / f"codex-{phase}-review.md", review.read_text())
        self.state.last_successful_codex_turn = now()
        self.state.last_codex_verdict = data["verdict"]
        self.state.last_reviewed_commit = snapshot(self.root, self.coord)["head"]
        self.state.pending_review = False
        if phase == "review" and data["verdict"] != "APPROVED":
            digest = hashlib.sha256(data["summary"].encode()).hexdigest()
            self.state.review_failures = (self.state.review_failures + [digest])[-4:]
            if len(self.state.review_failures) >= 2 and len(set(self.state.review_failures[-2:])) == 1:
                self.state.loop_warning = "Repeated rejection: diagnose root cause and change approach"
        self.save()
        return data

    def decision(self, data: dict[str, Any], run: Path) -> bool:
        if data["action"] == "FINAL_REVIEW":
            data = self.boss("final", run)
        if data["action"] == "COMPLETE":
            if open_priorities(self.coord):
                raise LoopError("Cannot COMPLETE with unresolved P0/P1 backlog")
            self.state.status = "COMPLETE"
        elif data["action"] == "BLOCKED":
            self.state.status = "BLOCKED"
            self.state.blocker = data["summary"]
        else:
            return False
        self.save()
        return True

    def detect_loop(self, task: dict[str, Any], data: dict[str, Any]) -> None:
        key = task["issue_key"]
        count = self.state.issue_attempts.get(key, 0) + 1
        self.state.issue_attempts[key] = count
        self.state.repeated_task_count = count - 1
        if count > self.limit("max_corrections_per_task") + 1:
            raise SafetyError(f"Correction budget exhausted for {key}")
        history = self.state.fingerprints
        stalled = len(history) >= 2 and history[-1] == history[-2]
        oscillates = len(history) >= 4 and history[-4:-2] == history[-2:]
        if count >= 3 or stalled or oscillates or self.state.loop_warning:
            self.state.loop_warning = "Repeated task/stalled work/oscillation: require a materially different approach"
            if not data["recovery_strategy"].strip():
                self.save()
                raise LoopError(self.state.loop_warning)
            with (self.coord / "MEMORY.md").open("a") as stream:
                stream.write(f"\n## Loop recovery, cycle {self.state.cycle}\n{data['recovery_strategy']}\n")
            self.state.loop_warning = None

    def worker(self, task: dict[str, Any], run: Path) -> None:
        self.guard()
        session_exists(self.root, self.config.claude_session)
        assert_no_claude_process()
        text = prompt("worker", coord=self.coord.relative_to(self.root), task_id=task["task_id"])
        atomic_write(run / "claude-prompt.txt", text)
        result = self.coord / "RESULT.md"
        if result.exists():
            atomic_write(run / "previous-result.md", result.read_text())
            result.unlink()  # a stale report must never satisfy this turn
        task_text = (self.coord / "TASK.md").read_bytes()
        self.state.pending_review = True
        self.save()  # crash after here requires review before any further implementation
        git_identity = self.git_identity()
        protected = self.protected_files(worker=True)
        try:
            output = execute(worker_command(self.config.claude_session), self.root,
                             self.limit("timeout"), text, run / "claude")
        except LoopError as exc:
            errfile = run / "claude.stderr.txt"
            detail = errfile.read_text() if errfile.exists() else ""
            outfile = run / "claude.stdout.txt"
            detail += outfile.read_text() if outfile.exists() else ""
            if re.search(r"(session|conversation).*(not found|does not exist|invalid)|cannot resume|failed to resume|no conversation", detail, re.I):
                raise SafetyError("Claude resume failed; no fallback conversation permitted") from exc
            raise
        finally:
            self.verify_protected(protected)
            self.guard()
            if self.git_identity() != git_identity:
                raise SafetyError("Claude changed Git HEAD/index; preserve evidence and stop")
        self.guard()
        if (self.coord / "TASK.md").read_bytes() != task_text:
            raise SafetyError("Worker modified the assigned task")
        try:
            response = json.loads(output)
        except ValueError as exc:
            raise SafetyError("Claude response malformed; cannot verify resumed session identity") from exc
        if not isinstance(response, dict) or response.get("session_id") != self.config.claude_session:
            raise SafetyError("Claude returned a different/missing session ID; stopping without fallback")
        if response.get("is_error") or response.get("type") != "result" or response.get("subtype") != "success":
            raise LoopError("Claude returned an unsuccessful result; see worker output")
        report = validate_result(result, task)
        atomic_write(run / "claude-result.md", result.read_text())
        self.state.last_successful_claude_turn = now()
        self.state.last_claude_status = report["status"]
        self.state.latest_tests = report["tests"]
        self.save()

    def run(self, rounds: int, dry_run: bool = False) -> int:
        if rounds < 1:
            raise LoopError("rounds must be positive")
        with locked(self.coord / "loop.lock"):
            # Reload after taking ownership; a concurrent init/run may have updated state.
            self.state = State.load(self.coord / "state.json")
            errors = self.preflight()
            if errors:
                self.state.status, self.state.blocker = "BLOCKED", "; ".join(errors)
                self.save()
                raise SafetyError(self.state.blocker)
            if dry_run:
                print(json.dumps({"rounds": rounds, "claude_session": self.config.claude_session,
                                  "worker_argv": worker_command(self.config.claude_session),
                                  "sequence": "Codex plan -> Claude -> Codex review -> optional final review"}, indent=2))
                return 0
            if self.state.status == "COMPLETE":
                print("COMPLETE: dedicated final review already recorded")
                return 0
            # A global same-session advisory lock also protects other worktrees/coord dirs.
            lockdir = Path(tempfile.gettempdir()) / f"agent-loop-{os.getuid()}"
            lockdir.mkdir(mode=0o700, exist_ok=True)
            if lockdir.is_symlink() or lockdir.stat().st_uid != os.getuid() or lockdir.stat().st_mode & 0o077:
                raise SafetyError("Unsafe session lock directory")
            with locked(lockdir / f"{self.config.claude_session}.lock"):
                return self._cycles(rounds)

    def _cycles(self, rounds: int) -> int:
        self.state.status, self.state.blocker = "RUNNING", None
        self.state.started_at = self.state.started_at or now()
        self.save()
        try:
            for _ in range(rounds):
                self.state.cycle += 1
                run = self.coord / "runs" / f"{self.state.cycle:05d}-{uuid.uuid4().hex[:8]}"
                run.mkdir(mode=0o700)
                self.save()
                print(f"Cycle {self.state.cycle}; Claude session {self.config.claude_session}", flush=True)
                baseline = audit(self.root, self.coord, run, "baseline")
                if not self.state.fingerprints:
                    self.state.fingerprints.append(baseline["fingerprint"])
                try:
                    if self.state.pending_review:
                        recovery = self.boss("review", run)
                        if self.decision(recovery, run):
                            return 0 if self.state.status == "COMPLETE" else 2
                        # Use a separate artifact directory for the new turn's review.
                        run = run / "recovered"
                        run.mkdir()
                    data = self.boss("plan", run)
                    if self.decision(data, run):
                        return 0 if self.state.status == "COMPLETE" else 2
                    if data["action"] != "TASK":
                        raise LoopError("Final review requires follow-up; replan next bounded attempt")
                    task = validate_task(self.coord / "TASK.md", data["task_id"])
                    if task["task_id"] == self.state.current_task_id:
                        raise LoopError("Every new attempt needs a fresh task ID; keep issue_key stable")
                    self.detect_loop(task, data)
                    self.state.current_task_id = task["task_id"]
                    atomic_write(run / "task.md", (self.coord / "TASK.md").read_text())
                    audit(self.root, self.coord, run, "after-codex")
                    self.save()
                    worker_error = worker_fatal = None
                    try:
                        self.worker(task, run)
                    except SafetyError as exc:
                        worker_error = worker_fatal = str(exc)
                        self.state.last_error = worker_error
                        self.save()
                    except LoopError as exc:
                        worker_error = str(exc)
                        self.state.last_error = worker_error
                        self.save()
                    after = audit(self.root, self.coord, run, "after-claude")
                    self.state.fingerprints = (self.state.fingerprints + [after["fingerprint"]])[-6:]
                    review = self.boss("review", run)
                    if worker_fatal:
                        raise SafetyError(worker_fatal)
                    if worker_error and review["verdict"] == "APPROVED":
                        raise LoopError("Protocol/worker failure cannot be approved without a corrective turn")
                    if self.decision(review, run):
                        return 0 if self.state.status == "COMPLETE" else 2
                    if worker_error or review["verdict"] != "APPROVED":
                        raise LoopError(worker_error or review["summary"])
                    self.state.consecutive_failures = 0
                    self.state.last_error = None
                except SafetyError:
                    raise
                except LoopError as exc:
                    self.state.consecutive_failures += 1
                    self.state.last_error = str(exc)
                    atomic_write(run / "failure.txt", str(exc) + "\n")
                    self.save()
                    if self.state.consecutive_failures >= self.limit("max_consecutive_failures"):
                        raise SafetyError(f"Consecutive failure limit reached: {exc}") from exc
                finally:
                    write_json(run / "state-after.json", asdict(self.state))
                    self.save()
            self.state.status = "STOPPED"  # budget exhausted, never inferred completion
            return 1 if self.state.last_error else 0
        except KeyboardInterrupt:
            self.state.status, self.state.last_error = "STOPPED", "Interrupted; partial work preserved"
            return 130
        except (LoopError, OSError) as exc:
            self.state.status, self.state.last_error = "ERROR", str(exc)
            return 2
        finally:
            self.save()


DEFAULT_GOAL = """# Human goal

Implement and operate a local sequential autonomous software-engineering loop.
Codex inspects, researches, prioritizes, assigns tasks and independently verifies.
Claude implements/tests using the SAME pre-existing project conversation.
Deliver a maintainable Python CLI, durable state, audit logs, recovery limits,
session ownership checks, tests and documentation. Validate with one bounded live
cycle after successful preflight. No unrelated product goal is available here.

## Hard constraints
Never ask the human runtime questions or wait for approval. Never create a new
Claude conversation or silently continue latest. Preserve existing work and goal.
No destructive Git commands, automatic commits of user work, pushes, deployment,
sudo, production access, secret inspection or unrelated host modifications.
No GUI/terminal automation. Use supported noninteractive CLI interfaces.
Bound all execution and keep Codex and Claude sequential. Missing credentials,
repository or existing session must be recorded; do not fabricate success.
"""


def initialize(root: Path, coord: Path, session: str | None, goal_file: Path | None = None) -> None:
    coord.mkdir(parents=True, exist_ok=True)
    with locked(coord / "loop.lock"):
        config_path, state_path = coord / "config.json", coord / "state.json"
        config = Config.load(config_path) if config_path.exists() else Config()
        state = State.load(state_path) if state_path.exists() else State()
        if state.status == "RUNNING":
            raise SafetyError("Cannot reinitialize RUNNING state")
        selected = session or config.claude_session or os.environ.get("AGENT_LOOP_CLAUDE_SESSION")
        if not selected:
            candidates = discover_sessions(root)
            selected = candidates[0] if len(candidates) == 1 else None
        error = None
        if selected:
            selected = canonical_session(selected)
            if config.claude_session and selected != config.claude_session:
                raise SafetyError("Refusing to switch the established worker conversation")
            try:
                session_exists(root, selected)
            except LoopError as exc:
                error = str(exc)
        else:
            error = "No unique existing project Claude conversation found; no session was created"
        config.claude_session = state.claude_session = selected
        defaults = {"GOAL.md": goal_file.read_text() if goal_file else DEFAULT_GOAL,
                    "MEMORY.md": "# Durable project knowledge\n",
                    "ASSUMPTIONS.md": "# Material assumptions\n",
                    "BACKLOG.md": "# Codex-owned backlog\n\n- [ ] P1 Validate one bounded live cycle against GOAL.md.\n",
                    "RESEARCH.md": "# Engineering research\n",
                    "TASK.md": "# No task assigned yet\n",
                    "RESULT.md": "# No worker attempt yet\n",
                    "CODEX_REVIEW.md": "# Verdict\n\nINITIAL\n"}
        for name, content in defaults.items():
            if not (coord / name).exists():
                atomic_write(coord / name, content)
        digest = hashlib.sha256((coord / "GOAL.md").read_bytes()).hexdigest()
        if state.goal_sha256 and state.goal_sha256 != digest:
            raise SafetyError("Existing authoritative goal changed; init will not reset its fingerprint")
        state.goal_sha256 = digest
        state.status, state.blocker = ("BLOCKED", error) if error else ("STOPPED", None)
        coord.joinpath("runs").mkdir(exist_ok=True)
        write_json(config_path, asdict(config))
        state.save(state_path)
        print(f"Coordination: {coord}\nClaude session: {selected or 'UNCONFIGURED'}")
        if error:
            raise SafetyError(error)


def choose_coord(root: Path, value: str | None) -> Path:
    if value:
        path = root / value
    elif any((root / ".agent-loop" / name).exists() for name in ("config.json", "GOAL.md")):
        path = root / ".agent-loop"
    elif (root / ".agents").exists() and not os.access(root / ".agents", os.W_OK):
        path = root / ".agent-loop"
    else:
        path = root / ".agents"
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or resolved == root or path.is_symlink():
        raise SafetyError("Unsafe coordination directory")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--agent-dir", help="Relative coordination directory; auto-detects .agents/.agent-loop")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--claude-session", help="Existing UUID (never continue-latest)")
    init.add_argument("--goal-file", type=Path)
    for command in ("doctor", "status", "inspect"):
        commands.add_parser(command)
    run = commands.add_parser("run")
    run.add_argument("--rounds", type=int, default=1)
    run.add_argument("--timeout", type=int)
    run.add_argument("--max-consecutive-failures", type=int)
    run.add_argument("--max-corrections-per-task", type=int)
    run.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        root = args.root.resolve()
        coord = choose_coord(root, args.agent_dir)
        if args.command == "init":
            initialize(root, coord, args.claude_session, args.goal_file)
            return 0
        app = Orchestrator(root, coord)
        if args.command == "doctor":
            errors = app.preflight()
            for executable in ("codex", "claude"):
                try:
                    print(execute([executable, "--version"], root, 20).strip())
                except LoopError as exc:
                    print(str(exc))
            print(f"Coordination: {coord}\nClaude session: {app.config.claude_session or 'UNCONFIGURED'}")
            print("\n".join("FAIL: " + e for e in errors) if errors else "PASS: local preflight (no model call; auth/resume not yet exercised)")
            return 2 if errors else 0
        if args.command in {"status", "inspect"}:
            print(json.dumps(asdict(app.state), indent=2))
            print("Open P0/P1:", len(open_priorities(coord)))
            try:
                print("Git status:\n" + git(root, "status", "--short"))
            except LoopError:
                print("Git status: unavailable")
            print("Assumptions:\n" + (coord / "ASSUMPTIONS.md").read_text()[-3000:])
            if args.command == "inspect":
                for name in ("GOAL.md", "TASK.md", "RESULT.md", "CODEX_REVIEW.md", "RESEARCH.md"):
                    print(f"\n{name}\n{(coord / name).read_text()}")
                print("Run logs:", coord / "runs")
                for path in sorted((coord / "runs").iterdir())[-5:]:
                    print(path)
                try:
                    print(git(root, "diff", "--stat"))
                except LoopError:
                    pass
            return 0
        # Runtime limits are invocation-local; keep stored configuration invariant.
        overrides = {key: getattr(args, key) for key in
                     ("timeout", "max_consecutive_failures", "max_corrections_per_task")
                     if getattr(args, key) is not None}
        for key, value in overrides.items():
            if value < 1:
                raise LoopError(f"{key} must be positive")
        # Separate effective limits from the persisted configuration checked by guard.
        app.limits = overrides
        previous = signal.signal(signal.SIGTERM, stop_signal)
        try:
            return app.run(args.rounds, args.dry_run)
        finally:
            signal.signal(signal.SIGTERM, previous)
    except (LoopError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def stop_signal(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    sys.exit(main())
