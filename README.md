# Local autonomous engineering loop

Codex owns inspection, research, priority, task specification and acceptance.
Claude implements one task at a time in an **existing, explicitly pinned Claude
Code conversation**. Python, Git and the two installed CLIs are the only runtime
dependencies. Python 3.11+ and Linux are required; tested on Fedora 44/Python 3.14.

## Current workspace status

The supplied workspace was empty at setup, with read-only Git and coordination
placeholders. The human subsequently supplied
[`sugerdarco/pd24`](https://github.com/sugerdarco/pd24) and authorized publishing the
implementation. Git is now initialized locally with that repository as `origin`.
The implemented system is the initial goal; no separate product specification
was present.

Coordination therefore lives in **`.agent-loop/`**, selected automatically. Setup
and local tests are implemented. A real one-cycle invocation was attempted and
stopped at preflight with `BLOCKED`; no model was invoked and no conversation was
created. A live Codex/Claude cycle has **not** been validated in this workspace.
The remaining blocker is the missing existing project Claude session: no matching
local transcript or configured session ID is available. The tool will not search
unrelated conversations or invent a replacement. The human-authorized initial
push does not enable automatic pushes in the autonomous runtime.

## Commands

Run from the actual Git repository root. On a normal writable checkout the
default coordination directory is `.agents/`. An existing `.agent-loop/GOAL.md`
or `.agent-loop/config.json` takes precedence, so clones preserve this project's
committed goal. Config, state, locks and run logs stay local and are ignored by
Git. Run `init` first on a fresh clone. Global options go before the subcommand:

```bash
python3 tools/agent_loop.py doctor
python3 tools/agent_loop.py status
python3 tools/agent_loop.py inspect
python3 tools/agent_loop.py --root /path/to/repo --agent-dir .agent-loop status
```

Initial configuration in a checkout with the **existing project conversation**:

```bash
python3 tools/agent_loop.py init --claude-session EXISTING-SESSION-UUID
```

`AGENT_LOOP_CLAUDE_SESSION` is an alternative to the flag during init. If neither
is supplied, init adopts a session only if exactly one matching local project
transcript exists, and persists its UUID. Multiple candidates are never resolved
by choosing the latest. Names/search terms are deliberately unsupported in V1:
the installed CLI documents UUID resume but can open a picker for a search term.
An explicit ID must exist locally and contain a matching project cwd and session
ID. `CLAUDE_CONFIG_DIR` is supported. Session storage is a local CLI implementation
detail; a changed layout fails closed. Init never sends a model prompt.

The first safe bounded validation command is:

```bash
python3 tools/agent_loop.py run --rounds 1
```

It currently exits 2 with the recorded session blocker. Once preflight is
satisfied, that exact command runs Codex plan, one Claude turn, and Codex review.
If completion is proposed, a separate Codex final review runs too. One round is
bounded by the per-turn timeout (default 900 seconds); it can contain up to four
model turns, plus a recovery review if a previous attempt was interrupted.

```bash
python3 tools/agent_loop.py run --rounds 1 --dry-run
python3 tools/agent_loop.py run --rounds 5 --timeout 600 \
  --max-consecutive-failures 3 --max-corrections-per-task 3
python3 -m unittest discover -s tests -v
```

Dry-run performs preflight and shows the command/sequence without model calls.
It records `BLOCKED` when preflight fails. Doctor performs no model calls or auth
probes. Its success means local prerequisites passed, not that authentication,
network access or live resume has been proven. Tests use temporary repositories
and fake agents and consume no model credits.

## Session ownership and permissions

Exit the manually operated foreground Claude process before the orchestrator
takes ownership. For a CLI-managed background session, stop it with the CLI's
supported stop command; stopping preserves its conversation. **Merely detaching a
still-running writer is insufficient.** Do not open the session interactively
while the loop runs. The tool conservatively refuses *any* other same-user Claude
process, uses an exclusive repository lock and a cross-worktree per-session lock
under a private `/tmp/agent-loop-UID` directory, and rechecks before each worker
turn. These advisory locks cannot stop a human from starting an unrelated manual
writer between checks; exclusive ownership remains an operating requirement.

Locally inspected on 2026-10-17:

- Codex CLI `0.163.0`: `codex -a never exec --sandbox workspace-write --json
  --output-schema ... --output-last-message ... -`. Prompts arrive through stdin.
- Claude Code `2.1.302`: `claude --resume UUID --print --output-format json`.
  Result JSON must identify that exact `session_id`. Missing/different identity,
  malformed JSON or a detected resume error stops the system. There is no
  `--continue`, `--fork-session`, `--session-id` creation or fallback invocation.
- Claude uses `--permission-mode acceptEdits --permission-prompts none
  --restricted`, empty MCP configuration, disabled hooks/slash commands/Chrome,
  and a limited built-in tool set. Its preallowed Bash command is
  `python3 -m unittest ...`, matching this project's test suite. Other commands
  that would prompt are denied and must be reported to Codex. This policy is
  deliberately project-specific, not an arbitrary shell allowlist.

For another Python project, local `config.json` may set `worker_python` to a
repository-relative interpreter such as `pd24/.venv/bin/python`. Preflight checks
that it is executable. This adds permission to run that interpreter with arguments
so Claude can execute the project's tests and bounded experiments. It executes
repository Python code and is not an OS sandbox. Absolute paths, parent traversal
and shell syntax are rejected. Leave it null for the default unittest-only policy.

No permission bypass flags are used. Codex has workspace-write sandboxing and
never asks for approval. Claude restricted mode confines file tools; **executing
repository tests still executes repository code**, so CLI permissions are not an
OS security boundary. Use a dedicated account or Podman with only the checkout
and necessary auth mounted for stronger isolation. Do not run against untrusted
tests with ambient production credentials. This tool never reads auth/SSH key
files or dumps environment variables, and it does not install tools or use sudo.

The local help is the source of truth for flag support. The supervisor workflow
also follows the structured-output and bounded-repair approach described in
[OpenAI's iterative repair-loop guide](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex).

## Durable protocol and decisions

`GOAL.md` is the immutable root specification, hashed at init. An existing goal is
never replaced. `init --goal-file path` imports a human specification only when no
goal exists. Changing an established goal makes subsequent execution stop; this
version does not autonomously accept a changed specification.

`MEMORY.md`, `ASSUMPTIONS.md`, `BACKLOG.md`, and `RESEARCH.md` hold concise durable
knowledge. The role prompts live in `tools/agent_prompts/`. Codex researches when
useful, chooses reversible assumptions without questions, and can defer one
blocked task while pursuing independent work. Open backlog items use
`- [ ] P1 ...`; resolved items use `- [x] P1 ...` (similarly P0/P2/P3).

TASK, RESULT and CODEX_REVIEW use a fenced `json` header plus human-readable
sections. For example:

````markdown
```json
{"task_id":"T-001","issue_key":"startup-regression","objective":"Restore startup","acceptance_criteria":["Startup regression test passes"]}
```

# Requirements
Concrete scope, technical direction, tests and acceptance evidence follow here.
````

Claude's RESULT includes the exact task ID, `COMPLETE|PARTIAL|BLOCKED`, a map from
every exact criterion to `PASS|FAIL|NOT_TESTED`, and a list of actual test commands
and observed results. A fresh report is mandatory; the old one is archived before
invocation. Claude COMPLETE is only a worker claim. Codex must independently
inspect source/diff/history and execute tests, with an explicit
`INITIAL|APPROVED|REJECTED|NEEDS_FOLLOWUP` verdict. New tasks use new task IDs.
Corrections may retain the current task ID with an explicit REJECTED or
NEEDS_FOLLOWUP verdict, and must retain the same issue key. Each attempt still
requires a fresh RESULT and counts toward the correction limit.

If a non-completion review's Markdown header omits only `completion_checks`, the
orchestrator copies that field from the already schema-validated structured
decision and logs the repair. All existing fields must agree exactly. Conflicting
values, missing identity fields and incomplete COMPLETE reports still fail closed;
no checklist value or acceptance verdict is inferred.

Completion requires a **separate final Codex turn**, an approved review with all
goal/functionality/tests/security/P0-P1/architecture/docs/runtime/assumptions/Git
checks true, evidence, and no open P0/P1 backlog. The protocol validates the
structure; the supervisor is responsible for the truth of its engineering
judgment. No program can infer correctness solely from generated text. Fake-agent
tests verify orchestration behavior, not live model quality.

## Recovery, auditing and stopping

Each bounded round is sequential. An interrupted worker is marked pending review
*before* launch; restart reviews partial work before issuing another task. Normal
worker errors still get Codex review. Rejections and protocol failures count
toward the failure limit and are passed to the next Codex planning turn. Session,
goal or Git ownership violations stop immediately after a safe review where
possible. No automatic backoff is used; retries are bounded by rounds and failures.

Repeated issue keys, identical source snapshots, A/B/A/B source oscillation and
repeated review summaries trigger an explicit root-cause/new-approach requirement.
The recovery strategy is stored in MEMORY. Corrections remain bounded even if
task IDs change. These are heuristics: they cannot prove semantic progress, and
the total rounds limit is the final bound.

State is atomically replaced with fsync and tracks status, cycle, session, task,
successful turns, review/test summaries, failure counters, pending review and
blockers. There is no waiting-for-human state. Ctrl+C and SIGTERM terminate the
child process group and persist `STOPPED`; a hard kill can leave `RUNNING`, which
is recoverable once the process lock is released. An actual surviving Claude
process prevents restart. Timeouts terminate subprocess groups. Exit codes:
0 = successful bounded run or verified COMPLETE; 1 = bounded run ended with a
recoverable failure; 2 = preflight/fatal error or project BLOCKED; 130 = interrupted.
`STOPPED` at a round limit never means project COMPLETE.

Run artifacts contain separate prompts/stdout/stderr, subprocess metadata,
decisions, archived reports, and before/after Git inventories and patches. Files
are private by default. Known credential values and common token patterns are
redacted in output logs, but logs are not guaranteed secret-free; keep them local.
Secret-like paths are excluded from source snapshots/backups. Never commit logs.

Dirty Git is permitted: baseline inventories, index/worktree patches, and private
byte-for-byte backups of ordinary modified/untracked files are captured. The
agents are instructed to preserve these changes; their HEAD/index mutations stop
the loop. This V1 uses **file/patch checkpoints rather than automatic commits**,
so unrelated staged work can never enter an agent-created commit. Recovery is
manual from evidence; the tool never resets, cleans, stashes, checks out, pushes
or deploys. Do not edit the worktree concurrently with an active cycle.

`status` and `inspect` are observation commands, not decision gates. There is no
interactive approval path and no dependency on human answers during operation.
