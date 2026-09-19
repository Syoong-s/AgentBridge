# AgentBridge

AgentBridge is a local Codex plugin that launches user-configured agent CLIs as
plugin-managed external child agents and returns their bounded stdout, stderr, exit
status, lineage, and lifecycle metadata to Codex.

The bundled aliases use the current documented commands for Claude Code (`claude -p`)
and Google Antigravity CLI (`agy -p`). Antigravity remains disabled by default so a
machine without `agy` still has a usable fallback configuration. Any other compatible
CLI can be added as another JSON alias.

The Antigravity argv stores the prompt as the single item `-p={prompt}` and places
`--output-format json` before it. This is required by `agy` 1.2.0 so an intervening CLI
flag is not consumed as the prompt.

## Capabilities

- direct argv execution without an implicit shell;
- aliases and launch commands controlled by one JSON parameter file;
- first-class model names and reasoning-effort levels mapped to provider-specific argv;
- argument, stdin, and temporary prompt-file transport modes;
- asynchronous tasks with progress-aware concurrent waits, paginated logs, and persistent metadata;
- parent/root task lineage plus resumable provider sessions and follow-up turns;
- a five-tool public MCP surface with compact status responses and opt-in log output;
- concurrency limits, timeouts, cancellation of isolated POSIX process groups, and
  restart recovery;
- optional working-directory allowlists and per-alias environment/extra-argument policy;
- standard-library-only Python runtime with no package installation.

AgentBridge does not create a new model service or sandbox another CLI. Each configured
agent runs locally with the permissions and authentication already available to that
command. “External child agent” is an AgentBridge orchestration abstraction: it is not a
native Codex subagent thread, does not appear in the native subagent UI, and does not
inherit native Codex model, sandbox, or thread settings.

## Requirements

- Codex on Linux or Windows 11 + WSL 2;
- Bash 4.4+;
- Python 3.10+;
- each external agent CLI installed and authenticated separately.

## Install

From GitHub, run:

```bash
codex plugin marketplace add Syoong-s/AgentBridge
codex plugin add agent-bridge@agent-bridge
```

Start a new Codex task after installation so the skill and MCP server are loaded.

For a source checkout, register the repository root as a local marketplace:

```bash
codex plugin marketplace add "$(pwd)"
codex plugin add agent-bridge@agent-bridge
```

## Configure agents

AgentBridge resolves the first existing configuration in this order:

1. `AGENT_BRIDGE_CONFIG` (explicit file path);
2. `$PLUGIN_DATA/config.json` (Codex-managed writable plugin data);
3. `~/.config/agent-bridge/config.json`;
4. the packaged `config/agents.example.json` fallback.

To create a user-owned configuration from a source checkout:

```bash
mkdir -p ~/.config/agent-bridge
cp plugins/agent-bridge/config/agents.example.json \
  ~/.config/agent-bridge/config.json
```

The file is versioned JSON. The bundled commands follow the current
[Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage) and
[Antigravity headless-mode reference](https://antigravity.google/docs/cli/headless/).
Check `claude --help` or `agy --help` against the installed version before enabling a
provider in production. Example:

```json
{
  "version": 1,
  "defaults": {
    "max_concurrent_tasks": 4,
    "max_retained_tasks": 100,
    "default_timeout_sec": 3600,
    "max_timeout_sec": 86400,
    "default_max_output_bytes": 2097152,
    "allowed_work_roots": ["/home/me/projects"]
  },
  "agents": {
    "claude": {
      "enabled": true,
      "description": "Claude Code in print mode",
      "command": ["claude", "-p", "--output-format", "text", "{prompt}"],
      "prompt_mode": "argument",
      "inherit_env": true,
      "environment": {},
      "timeout_sec": 3600,
      "max_output_bytes": 2097152,
      "allow_extra_args": true,
      "model": {
        "arguments": ["--model", "{model}"]
      },
      "reasoning_effort": {
        "default": "high",
        "arguments": ["--effort", "{reasoning_effort}"],
        "allowed_values": ["low", "medium", "high", "xhigh", "max", "ultracode"]
      },
      "session": {
        "id_source": "generated_uuid",
        "start_arguments": ["--session-id", "{session_id}"],
        "resume_arguments": ["--resume", "{session_id}"]
      }
    }
  }
}
```

### Global fields

| Field | Meaning |
| --- | --- |
| `max_concurrent_tasks` | Active task ceiling, from 1 to 32 |
| `max_retained_tasks` | Number of terminal task directories retained |
| `default_timeout_sec` | Per-alias timeout when omitted |
| `max_timeout_sec` | Upper bound for configured and runtime timeout overrides |
| `default_max_output_bytes` | Default storage ceiling for each stdout/stderr stream |
| `allowed_work_roots` | Canonical absolute roots; an empty array permits any existing directory |

### Agent fields

| Field | Meaning |
| --- | --- |
| `enabled` | Disabled aliases are visible but cannot launch |
| `description` | Human-readable purpose shown by `list_agents` |
| `command` | Non-empty argv string array; no shell parsing occurs |
| `prompt_mode` | `argument`, `stdin`, or `file` |
| `inherit_env` | Inherit the MCP server environment before applying `environment` |
| `environment` | Static environment values with supported placeholders; task `PWD` is bridge-owned |
| `timeout_sec` | Alias-specific default timeout |
| `max_output_bytes` | Per-stream persisted byte ceiling, 1 KiB to 100 MiB |
| `allow_extra_args` | Permit discrete runtime argv items after the configured command |
| `model` | Optional portable model selector mapped to provider argv |
| `reasoning_effort` | Optional portable thinking/effort selector mapped to provider argv |
| `session` | Optional provider conversation creation/extraction/resume contract |

Supported placeholders are `{prompt}`, `{prompt_file}`, `{cwd}`, and `{task_id}`.
Argument mode requires exactly one `{prompt}`. File mode requires exactly one
`{prompt_file}` and deletes that private temporary file after the task. Stdin mode must
not contain either prompt placeholder. Prompt placeholders are forbidden in
`command[0]`.

For consistency with the real `Popen(cwd=...)` directory, AgentBridge always sets the
child's `PWD` environment value to the resolved task cwd after inherited and alias
environment values are merged. A configured `environment.PWD` value is therefore
intentionally overridden.

### Model and reasoning mappings

`model` and `reasoning_effort` each accept an `arguments` array, optional `default`, and
optional `allowed_values`. Their argument arrays must contain exactly one `{model}` or
`{reasoning_effort}` placeholder respectively. A requested value is validated before
launch, expanded into discrete argv, and inserted immediately before the configured
prompt/prompt-file argument. Omitting `allowed_values` permits any bounded string, which
is useful for provider model catalogs that change over time.

Codex supplies these through the `model` and `reasoning_effort` fields of
`run_agent`. If Codex omits a value, AgentBridge uses the alias `default`; if there is no
default, that provider flag is omitted. A CLI without the corresponding mapping rejects
that runtime selector instead of silently ignoring it.

The task metadata records the selector resolved by AgentBridge, not an independent
provider attestation. When `allow_extra_args` is enabled, callers must not append
conflicting model/effort flags; the provider's own parser decides how duplicate flags
behave.

### Provider sessions and follow-ups

`session.id_source` is either `generated_uuid` or `stdout_json`:

- `generated_uuid` creates a UUID and requires `start_arguments` plus
  `resume_arguments` containing `{session_id}`. The Claude alias maps these to
  `--session-id` and `--resume`.
- `stdout_json` extracts a string by following `id_json_path` through the completed
  stdout JSON object, then substitutes it into `resume_arguments`. The Antigravity
  alias uses `--output-format json`, extracts `conversation_id`, and resumes with
  `--conversation`.

A `run_agent` call with `resume_task_id` starts a new persisted child task while reusing
the provider session. It inherits the alias, working directory, selected model/effort,
timeout, and root lineage. To prevent session corruption, only the latest terminal task
in a session can be continued, and only one task in that session may be active.

Use argument mode only when process-list visibility is acceptable. Prefer stdin or file
mode for sensitive prompts. If a CLI needs pipes, redirection, or other shell syntax,
put that logic in a reviewed executable wrapper script and configure its path as
`command[0]`; AgentBridge intentionally never turns a command string into a shell.

After editing the active file, call `list_agents` with `refresh=true`. Running tasks keep
the validated configuration snapshot with which they started. The older `reload_config`
RPC remains available for compatibility but is not advertised. Changing the state directory
requires restarting the MCP server.

## Use from Codex

Use the plugin only when the user explicitly asks to run, consult, or delegate to a
configured external agent. Example requests:

```text
Use $agent-bridge to ask Claude Code with model opus and reasoning effort high to review
this repository and return its findings.
```

```text
Use $agent-bridge with my antigravity alias to implement the requested change in this
working directory, then inspect and verify its edits locally.
```

The public MCP surface is intentionally small:

| Tool | Purpose |
| --- | --- |
| `list_agents` | List compact alias availability; use `detail=true` for diagnostics or `refresh=true` after configuration changes |
| `run_agent` | Start a task with `agent`, `prompt`, and `cwd`, or resume one with `resume_task_id` and `prompt` |
| `task_status` | Wait for lifecycle changes; logs are omitted unless `include_output=true` |
| `list_tasks` | List recent compact task metadata; use `detail=true` only for diagnostics |
| `cancel_task` | Idempotently cancel one active process group |

`run_agent` returns a task ID and compact launch metadata. While the task runs, call
`task_status` with a bounded `wait_sec`; its default response contains no stdout/stderr.
When output is needed, set `include_output=true`, use the returned byte offsets for
pagination, and keep `max_bytes` bounded (the public maximum is 16 KiB per stream).
A `task_status` wait without output wakes on lifecycle changes rather than every log write,
which avoids repeated context growth.

The older `reload_config`, `start_child_agent`, `start_task`, `send_followup`, `get_task`,
and `wait_task` RPC names remain server-side compatibility aliases, but are intentionally
omitted from `tools/list` so normal model context contains only the compact surface.

Task states are `queued`, `running`, `cancelling`, `succeeded`, `failed`, `timed_out`,
`cancelled`, and `interrupted`. `succeeded` means the process returned zero and required
output capture plus terminal metadata persistence completed normally; Codex still must
verify consequential claims and workspace edits. A `persistence_error` value explains a
terminal metadata failure that conservatively changed the in-memory result to `failed`.

Detailed task records retain `task_kind: external_child_agent`, `parent_task_id`,
`root_task_id`, `child_task_ids`, `invocation`, bridge-resolved `model`/`reasoning_effort`,
and `session_id` when supported. Request them only for diagnosis or auditing rather than
including them in every status poll.

Output is paginated by byte offsets. Follow each stream's `next_offset` while `has_more`
is true. On a running task, `incomplete_utf8_tail` asks the caller to wait until the
external process emits the rest of a multibyte character. A `stdout_truncated` or
`stderr_truncated` flag means the configured storage ceiling was reached.

MCP `notifications/cancelled` stops only the matching in-flight wait response; it
deliberately leaves the durable external task running. Use `cancel_task` when the provider
process itself should be terminated.

## State and security

Task state resolves from `AGENT_BRIDGE_STATE_DIR`, then `$PLUGIN_DATA/tasks`, then
`$XDG_STATE_HOME/agent-bridge/tasks`, and finally
`~/.local/state/agent-bridge/tasks`. Directories are mode `0700`; metadata and logs are
mode `0600` where the filesystem supports POSIX permissions.

Prompt text and runtime extra arguments are redacted from persisted command metadata.
File-mode prompts are removed after execution. However, the external CLI controls its
own stdout/stderr and may echo prompts, arguments, source code, credentials, or other
sensitive data into logs. Do not place secrets in prompts or command-line arguments, and
protect the state directory accordingly.

A configured alias is permission to execute its argv when Codex calls a launch or
follow-up tool.
Review parameter files and wrapper scripts as executable configuration. Use
`allowed_work_roots`, disabled aliases, conservative CLI permission modes, and Codex
approval settings to match your risk model. AgentBridge never bypasses an agent's login,
workspace trust, access challenge, or permission controls.

## Development and verification

The runtime has no external Python dependencies. It was developed in WSL 2 and tested
with Pixi CPython 3.10.21 and 3.12.13 plus system CPython 3.14.4. The local shell is
Bash 5.3.9. The supported runtime target is Python 3.10+ and Bash 4.4+ on Linux or WSL.

Run all checks from the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q plugins/agent-bridge/scripts tests
bash -n plugins/agent-bridge/scripts/*.sh
python3 -m json.tool .agents/plugins/marketplace.json >/dev/null
python3 -m json.tool plugins/agent-bridge/.codex-plugin/plugin.json >/dev/null
python3 -m json.tool plugins/agent-bridge/.mcp.json >/dev/null
```

The test suite uses a local fake agent. It never sends a prompt to Claude Code,
Antigravity, or a network service.

## License

AgentBridge is released under the MIT License. See [LICENSE](LICENSE).
