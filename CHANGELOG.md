# Changelog

All notable changes to AgentBridge are documented here.

## Unreleased

- Reduce the advertised MCP surface from nine tools to five unified tools.
- Make task status and agent discovery compact by default; output and diagnostics are opt-in.
- Add bounded public log reads, lifecycle-only waits, and compact JSON tool responses.
- Disable implicit skill invocation and retain the former RPC names only as compatibility aliases.

## 0.2.2 - 2026-09-13

- Fail tasks when stdout/stderr capture is unhealthy instead of accepting a zero process exit.
- Make terminal metadata persistence conservative, diagnostic, retried, and worker-safe.
- Return waits on incremental progress while keeping stdio responsive to ping and cancellation.
- Treat MCP request cancellation as wait-local and keep provider termination explicit.
- Synchronize each child's `PWD` environment value with its resolved task working directory.

## 0.2.1 - 2026-09-10

- Attach the Antigravity prompt directly to `-p` so CLI flags cannot be consumed as
  the prompt by `agy` 1.2.0.
- Validate the corrected command through a live authenticated AgentBridge launch with
  Gemini 3.8 Flash at high reasoning effort.

## 0.2.0 - 2026-09-10

- Add first-class, alias-mapped model and reasoning-effort launch controls.
- Add external child-agent identity, parent/root lineage, and visible child task IDs.
- Add provider session capture plus safe linear follow-ups through `send_followup`.
- Add official Claude Code (`claude`) and Google Antigravity CLI (`agy`) commands.
- Keep `start_task` compatible while making `start_child_agent` the preferred launch tool.

## 0.1.0 - 2026-09-10

- Add the installable `agent-bridge` Codex plugin and repo-local marketplace.
- Add JSON-configured external-agent aliases with argument, stdin, and file prompt modes.
- Add asynchronous task launch, bounded output, pagination, timeout, cancellation,
  concurrency limits, persistence, and restart recovery.
- Add the Agent Bridge skill, bilingual documentation, and end-to-end tests.
