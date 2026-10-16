# Durable project knowledge

- Setup date: 2026-10-17 Asia/Kolkata. Fedora 44, Python 3.14.7.
- Workspace initially contained only read-only `.git`, `.agents`, `.codex`, and
  `.aws` placeholders. There was no Git repository, source, history or AGENTS.md.
- `.agent-loop` remains the coordination location. After setup the human supplied
  `https://github.com/sugerdarco/pd24.git` and authorized a direct initial push.
  The placeholders are gone and Git is initialized on `main` with that origin.
  This explicit publishing authorization does not enable runtime automatic push.
- Codex CLI 0.163.0 supports `-a never exec --sandbox workspace-write`, JSONL
  output, a final-response schema and `--output-last-message`.
- Claude Code 2.1.302 supports explicit UUID `--resume`, `--print`, JSON output,
  restricted tools and `--permission-prompts none`. Never use continue-latest,
  fork-session or conversation creation. The current session remains unconfigured.
- No matching project transcript exists in the configured Claude metadata root.
  No real model call has been made by setup. Preserve that fact in reports.
- Runtime checkpoints use private baseline backups, patches and inventories;
  no automatic Git commits. Every completed worker turn receives a Codex review.
- Test command: `python3 -m unittest discover -s tests -v`; tests use temporary
  Git repositories and fake CLI implementations, with no model credits consumed.
