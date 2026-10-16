# Codex-owned backlog

- [x] P1 Environment prerequisite: actual Git repository at the workspace root.
  Resolved after the human supplied `https://github.com/sugerdarco/pd24.git` and
  authorized local initialization and direct publishing of this implementation.
- [ ] P1 Environment prerequisite: existing project-associated Claude UUID and
  local transcript. Deferred: neither is available; creating a session is forbidden.
- [ ] P1 Validate one bounded real cycle against GOAL.md after both prerequisites
  exist. Exact command: `python3 tools/agent_loop.py run --rounds 1`.
- [x] P1 Implement sequential boss/worker/review protocol and explicit session pinning.
- [x] P1 Implement atomic state, locks, timeout handling and bounded recovery.
- [x] P1 Verify core behavior with unit tests and fake-executable CLI integration.
- [x] P2 Document commands, lifecycle, safety boundaries and environment blockers.
- [ ] P3 Consider Podman packaging only if stronger OS isolation is required.
