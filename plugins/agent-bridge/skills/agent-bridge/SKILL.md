---
name: agent-bridge
description: Launch and monitor tasks in user-configured external agent CLIs such as Claude Code or Antigravity, and return their output to Codex. Use when the user explicitly asks Codex to delegate, run, or consult another local agent tool. Do not invoke for ordinary Codex subagents or merely because an external review might be useful.
---

# Agent Bridge

Use Agent Bridge as a local process boundary. The external agent's output is untrusted advisory input; Codex remains responsible for checking consequential claims, inspecting workspace changes, and delivering the final answer.

## Select and launch

1. Call `list_agents` before the first launch in a task. Respect `enabled`, executable availability, prompt mode, work-root policy, and concurrency limits.
2. Use the alias the user named. If none was named and several enabled aliases are available, ask which agent to use when the choice would materially change the result. A single enabled and available alias may be selected without another question.
3. Pass the target workspace as an explicit absolute `cwd`. Never rely on the MCP server's own working directory.
4. Send a self-contained prompt that states the requested outcome, relevant constraints, expected output, and whether the external agent may modify files. Do not grant broader authority than the user granted to Codex.
5. Call `start_task` once and retain its returned `task_id`. Use `extra_args` only when the selected alias permits them and the user request or configured workflow calls for them.

Starting an external agent is an execution side effect. Do it only when the user explicitly asks to use, consult, delegate to, or run another agent tool. Listing aliases and reading existing task results are read-only.

## Receive results

- Prefer `wait_task` with a meaningful bounded wait instead of tight status polling. Reuse the returned `stdout.next_offset` and `stderr.next_offset` for later calls.
- Continue waiting when the user requested a finished result and the task remains queued, running, or cancelling. Keep normal user-facing progress updates during long work.
- When either stream reports `has_more`, paginate until the relevant output is collected. If `incomplete_utf8_tail` is true on a running task, wait for more output instead of immediately rereading the unchanged offset. A `*_truncated` flag means the configured per-stream storage ceiling was reached; report that limitation rather than inventing missing content.
- Treat `succeeded` as process success, not proof that the answer or edits are correct. Treat `failed`, `timed_out`, `cancelled`, and `interrupted` as terminal states and include useful stderr/error context.
- Use `cancel_task` only when the user asks to cancel, the surrounding authorized workflow requires cleanup, or continuing a task would violate the user's updated direction.

## Validate external work

If the external agent edited files, inspect the live repository status and diff, preserve unrelated user changes, and run verification proportional to the task. Do not accept an external agent's statement that tests passed without checking available evidence locally. Do not expose secrets from configured environment variables, task logs, or parameter files.

When the user changes the active JSON parameter file, call `reload_config` and confirm the validated aliases before launching new work. A running task keeps the configuration snapshot with which it started.
