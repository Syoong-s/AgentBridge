# AgentBridge

AgentBridge is a local Codex plugin that launches tasks in user-configured agent
command-line tools and returns their bounded stdout, stderr, exit status, and lifecycle
metadata to Codex.

The bundled example is ready for Claude Code's non-interactive `-p` mode. Antigravity
is included as a disabled template because its invocation contract depends on the
installed CLI version. Any other CLI can be added as another JSON alias.

## Capabilities

- direct argv execution without an implicit shell;
- aliases and launch commands controlled by one JSON parameter file;
- argument, stdin, and temporary prompt-file transport modes;
- asynchronous tasks with bounded waits, paginated logs, and persistent metadata;
- concurrency limits, timeouts, cancellation of isolated POSIX process groups, and
  restart recovery;
- optional working-directory allowlists and per-alias environment/extra-argument policy;
- standard-library-only Python runtime with no package installation.

AgentBridge does not create a new model service or sandbox another CLI. Each configured
agent runs locally with the permissions and authentication already available to that
command.

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

The file is versioned JSON:

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
      "allow_extra_args": true
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
| `environment` | Static environment values with supported placeholders |
| `timeout_sec` | Alias-specific default timeout |
| `max_output_bytes` | Per-stream persisted byte ceiling, 1 KiB to 100 MiB |
| `allow_extra_args` | Permit discrete runtime argv items after the configured command |

Supported placeholders are `{prompt}`, `{prompt_file}`, `{cwd}`, and `{task_id}`.
Argument mode requires exactly one `{prompt}`. File mode requires exactly one
`{prompt_file}` and deletes that private temporary file after the task. Stdin mode must
not contain either prompt placeholder. Prompt placeholders are forbidden in
`command[0]`.

Use argument mode only when process-list visibility is acceptable. Prefer stdin or file
mode for sensitive prompts. If a CLI needs pipes, redirection, or other shell syntax,
put that logic in a reviewed executable wrapper script and configure its path as
`command[0]`; AgentBridge intentionally never turns a command string into a shell.

After editing the active file, ask Codex to call `reload_config`. Running tasks keep the
validated configuration snapshot with which they started. Changing the state directory
requires restarting the MCP server.

## Use from Codex

Example requests:

```text
Use $agent-bridge to ask Claude Code to review this repository and return its findings.
```

```text
Use $agent-bridge with my antigravity alias to implement the requested change in this
working directory, then inspect and verify its edits locally.
```

The skill guides Codex through these MCP tools:

| Tool | Purpose |
| --- | --- |
| `list_agents` | Inspect aliases, limits, prompt modes, and executable availability |
| `reload_config` | Validate the active file and replace configuration for future tasks |
| `start_task` | Launch one asynchronous task with explicit alias, prompt, and `cwd` |
| `get_task` | Read current metadata and paginated output without waiting |
| `wait_task` | Wait for up to 50 seconds, then return current metadata and output |
| `list_tasks` | List recent task metadata, optionally filtered by status |
| `cancel_task` | Idempotently cancel one active process group |

Task states are `queued`, `running`, `cancelling`, `succeeded`, `failed`, `timed_out`,
`cancelled`, and `interrupted`. `succeeded` means the process returned zero; Codex still
must verify consequential claims and workspace edits.

Output is paginated by byte offsets. Follow each stream's `next_offset` while `has_more`
is true. On a running task, `incomplete_utf8_tail` asks the caller to wait until the
external process emits the rest of a multibyte character. A `stdout_truncated` or
`stderr_truncated` flag means the configured storage ceiling was reached.

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

A configured alias is permission to execute its argv when Codex calls `start_task`.
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
