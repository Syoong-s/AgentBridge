# Changelog

All notable changes to AgentBridge are documented here.

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
