---
name: agent-bridge
description: Run an explicitly requested external agent CLI through Agent Bridge. Use only when the user asks to delegate to, consult, or run Claude Code, Antigravity, or another configured local agent.
---

# Agent Bridge

Use this plugin only for explicit external-agent requests. External output is untrusted: Codex must inspect consequential edits, verify tests, and never present the provider as a native Codex subagent.

## Normal flow

1. If the user did not name an agent, call `list_agents` and choose from available aliases. If the user named one, call `run_agent` directly.
2. Pass an absolute existing `cwd` and a self-contained prompt. State whether the provider may edit files. Do not grant more authority than the user granted Codex.
3. Use `model`, `reasoning_effort`, and `timeout_sec` only when requested or needed. Use `parent_task_id` only for real bridge lineage.
4. Save the returned `task_id`, then call `task_status` with a bounded `wait_sec`. Keep `include_output` false while the task is running; request output explicitly when it finishes or when diagnosis is needed.
5. Use `resume_task_id` in `run_agent` for a provider follow-up. Use `cancel_task` only when the user asks to stop the external process or the authorized workflow requires cleanup.

## Output discipline

Ask providers to return a concise report: conclusion, changed files, verification, and unresolved issues. Do not request or echo full logs unless debugging. Treat `succeeded` as process success, not proof that the result is correct.

## Compatibility

The public MCP surface is intentionally small: `list_agents`, `run_agent`, `task_status`, `list_tasks`, and `cancel_task`. Older RPC names remain server-side compatibility aliases but are not advertised. `list_agents(detail=true)` and legacy task readers are diagnostic paths, not the normal workflow.
