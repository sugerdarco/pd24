# Engineering research

## Structured decisions and filesystem state — ADOPT_NOW

Problem: Free-form agent text cannot safely select state transitions.
Current approach: No prior implementation was present.
Candidate: Codex final-response JSON schema plus Markdown files with validated
JSON headers, atomic state replacement, explicit task IDs and acceptance criteria.
Benefit: Machine-checkable handoffs while retaining inspectable human reports.
Cost: A small protocol validator and schema.
Risk: Valid syntax does not establish engineering correctness; independent Codex
inspection/tests and a dedicated final review remain mandatory.
Fit/recommendation: Use standard-library Python and immutable goal fingerprints;
do not add a database or queue.
Evidence: Installed `codex exec --help`; [OpenAI iterative repair-loop guide](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex).

## Stable worker identity — ADOPT_NOW

Problem: Continue-latest can drift to another conversation.
Candidate: Validate exact project-local UUID metadata, pass `--resume UUID`,
verify returned session ID, use advisory locks and check for existing writers.
Benefit: Preserve the same logical context and fail closed on identity failures.
Cost: Local transcript layout dependency and conservative process exclusion.
Risk: CLI upgrades can change storage/output; doctor and runtime checks must stop
instead of silently creating a replacement. Manual processes do not honor locks.
Fit/recommendation: Require explicit ownership; never run meaningless resume tests.
Evidence: Local Claude 2.1.302 help and official
[programmatic operation](https://code.claude.com/docs/en/headless) and
[session management](https://code.claude.com/docs/en/sessions) documentation.

## Bare mode — REJECT for this initial integration

Problem: Hidden hooks/plugins can add unwanted behavior.
Candidate: Claude `--bare`.
Benefit: Predictable minimal startup.
Cost/risk: Installed help and official documentation say bare mode skips OAuth
and keychain authentication; using it could break the existing CLI login.
Recommendation: Use restricted mode, empty MCP config, disabled hooks/slash
commands/Chrome and permission-prompts none; retain supported normal auth.

## Host isolation — BACKLOG

Problem: Running repository tests executes repository code.
Candidate: Dedicated account or Podman with only checkout and needed auth mounted.
Benefit: OS-enforced confinement beyond CLI file permissions.
Cost/risk: Credential/session mounts and container tooling add operational burden.
Recommendation: Document as optional hardening; do not install infrastructure or
claim that restricted file tools sandbox arbitrary test code.
