# Material assumptions

## Initial project objective

Decision: Build this autonomous engineering system as the initial project goal.
Evidence: The workspace was empty and the current conversation specifies the
system in detail; no independent product specification was present.
Reason: Avoid inventing a separate application or roadmap.
Risk: The intended existing project might be available in a different checkout.
Reversibility: The tool is portable; do not autonomously rewrite the established
GOAL.md or inspect unrelated projects to guess a replacement.

## Coordination location

Decision: Use `.agent-loop/` instead of `.agents/`.
Evidence: `.agents/` is an empty read-only sandbox placeholder.
Reason: Implement within the writable workspace without changing protection.
Risk: Existing tooling may expect `.agents`; all prompts use the configured path.
Reversibility: `--agent-dir` supports explicit paths in an appropriate checkout.

## Missing repository and conversation

Decision: Finish independent implementation/testing, then record live operation
as BLOCKED without asking questions or creating a new conversation.
Evidence: Git root/status/log fail; exact project Claude metadata directory is
absent; no AGENT_LOOP_CLAUDE_SESSION or CLAUDE_SESSION_ID is configured.
Reason: A fabricated repository/session would not preserve the claimed context.
Risk: Real integration cannot be verified in this supplied environment.
Reversibility: Existing checkout/session availability permits preflight to pass.

Update: The human subsequently supplied `https://github.com/sugerdarco/pd24.git`
and explicitly authorized direct publishing. Git initialization is now completed;
the existing-conversation prerequisite is still unresolved. The original no-push
runtime constraint remains in force for unattended cycles.

## Permissions and audit checkpoints

Decision: Use sandboxed/no-approval Codex, restricted/no-prompt Claude and
private file/patch checkpoints rather than automatic commits.
Evidence: Installed CLI help supports the selected controls; Git may start dirty.
Reason: Preserve user work and avoid implicitly committing unrelated staged files.
Risk: Claude's shell test permission is specific to this Python unittest project;
new test tooling needs a reviewed policy change. Test code is executable code.
Reversibility: Small command construction functions allow a reviewed extension;
no permanent infrastructure or external service is introduced.
