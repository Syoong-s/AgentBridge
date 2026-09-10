"""Minimal dependency-free Model Context Protocol server for Agent Bridge."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import sys
from typing import Any, Callable

from . import __version__
from .config import AgentConfig, BridgeConfig, ConfigError, load_active_config, load_config
from .tasks import TaskError, TaskManager


JSONRPC_VERSION = "2.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {DEFAULT_PROTOCOL_VERSION}


# ==========================================
# Function: Define the MCP tools exposed to Codex.
# Method: Return strict JSON schemas for configuration, launch, observation, wait, and cancellation.
# ==========================================
def tool_definitions() -> list[dict[str, Any]]:
    task_read_properties = {
        "task_id": {"type": "string", "description": "Task identifier returned by start_task."},
        "stdout_offset": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": "UTF-8 byte offset for the next stdout segment.",
        },
        "stderr_offset": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": "UTF-8 byte offset for the next stderr segment.",
        },
        "max_bytes": {
            "type": "integer",
            "minimum": 4,
            "maximum": 262144,
            "default": 65536,
            "description": "Maximum bytes returned from each stream.",
        },
    }
    return [
        {
            "name": "list_agents",
            "description": "List configured external-agent aliases, launch modes, limits, and executable availability.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "reload_config",
            "description": "Re-read and validate the active Agent Bridge JSON parameter file for future tasks.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "start_task",
            "description": "Start an asynchronous task in a configured external-agent CLI and return its task ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Configured agent alias."},
                    "prompt": {"type": "string", "description": "Task prompt sent through the configured mode."},
                    "cwd": {
                        "type": "string",
                        "description": "Explicit existing working directory for the external agent.",
                    },
                    "extra_args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 64,
                        "description": "Optional discrete argv items when the alias allows them.",
                    },
                    "timeout_sec": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional timeout capped by the global configuration.",
                    },
                },
                "required": ["agent", "prompt", "cwd"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_task",
            "description": "Read task metadata and paginated bounded stdout/stderr without waiting.",
            "inputSchema": {
                "type": "object",
                "properties": task_read_properties,
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "wait_task",
            "description": "Wait up to 50 seconds for task progress or completion, then return metadata and output.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **task_read_properties,
                    "wait_sec": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 50,
                        "default": 30,
                        "description": "Maximum server-side wait interval.",
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_tasks",
            "description": "List recent task metadata, optionally filtered by exact status names.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                    "statuses": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "queued",
                                "running",
                                "cancelling",
                                "succeeded",
                                "failed",
                                "timed_out",
                                "cancelled",
                                "interrupted",
                            ],
                        },
                        "uniqueItems": True,
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "cancel_task",
            "description": "Idempotently cancel one active external-agent process group.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task identifier to cancel."}
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    ]


# ==========================================
# Function: Format a successful MCP tool result as JSON text.
# Method: Preserve structured values inside one portable text content block.
# ==========================================
def tool_success(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
            }
        ],
        "isError": False,
    }


# ==========================================
# Function: Format an actionable MCP tool failure.
# Method: Return a normal tools/call result with isError instead of crashing the server.
# ==========================================
def tool_failure(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ==========================================
# Function: Validate one tool call's exact argument names.
# Method: Enforce required and optional keys even when a client bypasses the advertised JSON schema.
# ==========================================
def validate_tool_arguments(
    arguments: dict[str, Any],
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    missing = required - set(arguments)
    unknown = set(arguments) - allowed
    if missing:
        raise TaskError(f"missing required tool arguments: {sorted(missing)}")
    if unknown:
        raise TaskError(f"unknown tool arguments: {sorted(unknown)}")


# ==========================================
# Function: Report whether an alias executable is currently discoverable.
# Method: Check direct absolute paths or PATH lookup without expanding task-specific placeholders.
# ==========================================
def executable_status(agent: AgentConfig) -> dict[str, Any]:
    executable = agent.command[0]
    if "{" in executable:
        return {
            "available": None,
            "executable": executable,
            "detail": "availability depends on task placeholders",
        }
    if "/" in executable:
        candidate = Path(executable).expanduser()
        if not candidate.is_absolute():
            return {
                "available": None,
                "executable": executable,
                "detail": "relative executable is resolved against each task cwd",
            }
        available = candidate.is_file() and os.access(candidate, os.X_OK)
        resolved = str(candidate.resolve()) if candidate.exists() else str(candidate)
    else:
        if agent.inherit_env:
            search_path = agent.environment.get("PATH", os.environ.get("PATH", os.defpath))
        else:
            search_path = agent.environment.get("PATH", os.defpath)
        if "{" in search_path:
            return {
                "available": None,
                "executable": executable,
                "detail": "PATH availability depends on task placeholders",
            }
        if any(not entry or not Path(entry).is_absolute() for entry in search_path.split(os.pathsep)):
            return {
                "available": None,
                "executable": executable,
                "detail": "relative PATH entries are resolved against each task cwd",
            }
        found = shutil.which(executable, path=search_path)
        available = found is not None
        resolved = found
    return {"available": available, "executable": executable, "resolved": resolved}


# ==========================================
# Class: Bridge business service backing MCP tool calls.
# Method: Own configuration and delegate process lifecycle operations to TaskManager.
# ==========================================
class BridgeService:
    # ==========================================
    # Function: Load active configuration and initialize persistent task management.
    # Method: Resolve the plugin root once and construct one manager for the server lifetime.
    # ==========================================
    def __init__(self, plugin_root: Path) -> None:
        self.plugin_root = plugin_root.resolve()
        self.manager = TaskManager(load_active_config(self.plugin_root))

    # ==========================================
    # Function: Return public configuration and alias availability.
    # Method: Omit configured environment values and full command arguments that may be sensitive.
    # ==========================================
    def list_agents(self) -> dict[str, Any]:
        config = self.manager.config
        agents = []
        for alias in sorted(config.agents):
            agent = config.agents[alias]
            agents.append(
                {
                    "alias": alias,
                    "description": agent.description,
                    "enabled": agent.enabled,
                    "prompt_mode": agent.prompt_mode,
                    "timeout_sec": agent.timeout_sec,
                    "max_output_bytes_per_stream": agent.max_output_bytes,
                    "allow_extra_args": agent.allow_extra_args,
                    "executable": executable_status(agent),
                }
            )
        return {
            "config_path": str(config.path),
            "state_dir": str(config.state_dir),
            "max_concurrent_tasks": config.max_concurrent_tasks,
            "max_retained_tasks": config.max_retained_tasks,
            "allowed_work_roots": [str(path) for path in config.allowed_work_roots],
            "agents": agents,
        }

    # ==========================================
    # Function: Reload the same active parameter file for future launches.
    # Method: Fully validate a new immutable snapshot before replacing manager configuration.
    # ==========================================
    def reload_config(self) -> dict[str, Any]:
        current = self.manager.config
        updated = load_config(current.path, state_dir=current.state_dir)
        self.manager.replace_config(updated)
        result = self.list_agents()
        result["reloaded"] = True
        return result

    # ==========================================
    # Function: Dispatch one named MCP tool to its typed manager operation.
    # Method: Extract schema-backed arguments and normalize configuration/runtime failures.
    # ==========================================
    def call_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return tool_failure("tool arguments must be a JSON object")
        try:
            if name == "list_agents":
                validate_tool_arguments(arguments, set())
                return tool_success(self.list_agents())
            if name == "reload_config":
                validate_tool_arguments(arguments, set())
                return tool_success(self.reload_config())
            if name == "start_task":
                validate_tool_arguments(
                    arguments,
                    {"agent", "prompt", "cwd"},
                    {"extra_args", "timeout_sec"},
                )
                return tool_success(
                    self.manager.start_task(
                        alias=arguments.get("agent"),
                        prompt=arguments.get("prompt"),
                        cwd=arguments.get("cwd"),
                        extra_args=arguments.get("extra_args"),
                        timeout_sec=arguments.get("timeout_sec"),
                    )
                )
            if name == "get_task":
                validate_tool_arguments(
                    arguments,
                    {"task_id"},
                    {"stdout_offset", "stderr_offset", "max_bytes"},
                )
                return tool_success(
                    self.manager.get_task(
                        task_id=arguments.get("task_id"),
                        stdout_offset=arguments.get("stdout_offset", 0),
                        stderr_offset=arguments.get("stderr_offset", 0),
                        max_bytes=arguments.get("max_bytes", 65536),
                    )
                )
            if name == "wait_task":
                validate_tool_arguments(
                    arguments,
                    {"task_id"},
                    {"wait_sec", "stdout_offset", "stderr_offset", "max_bytes"},
                )
                return tool_success(
                    self.manager.wait_task(
                        task_id=arguments.get("task_id"),
                        wait_sec=arguments.get("wait_sec", 30),
                        stdout_offset=arguments.get("stdout_offset", 0),
                        stderr_offset=arguments.get("stderr_offset", 0),
                        max_bytes=arguments.get("max_bytes", 65536),
                    )
                )
            if name == "list_tasks":
                validate_tool_arguments(arguments, set(), {"limit", "statuses"})
                statuses = arguments.get("statuses")
                if statuses is not None and (
                    not isinstance(statuses, list)
                    or not all(isinstance(item, str) for item in statuses)
                ):
                    raise TaskError("statuses must be a string array")
                return tool_success(
                    self.manager.list_tasks(
                        limit=arguments.get("limit", 20),
                        statuses=statuses,
                    )
                )
            if name == "cancel_task":
                validate_tool_arguments(arguments, {"task_id"})
                return tool_success(self.manager.cancel_task(arguments.get("task_id")))
            return tool_failure(f"unknown tool {name!r}")
        except (ConfigError, TaskError) as exc:
            return tool_failure(str(exc))

    # ==========================================
    # Function: Shut down task management exactly once.
    # Method: Delegate graceful active-task cancellation to the manager.
    # ==========================================
    def close(self) -> None:
        self.manager.close()


# ==========================================
# Class: Line-delimited JSON-RPC transport for the local stdio MCP connection.
# Method: Handle core MCP initialization, discovery, tool calls, ping, and notifications.
# ==========================================
class MCPServer:
    # ==========================================
    # Function: Bind a bridge service to the protocol server.
    # Method: Store the service and track whether initialization has completed.
    # ==========================================
    def __init__(self, service: BridgeService) -> None:
        self.service = service
        self.initialized = False

    # ==========================================
    # Function: Construct one JSON-RPC success response.
    # Method: Echo the request ID and attach a result object.
    # ==========================================
    @staticmethod
    def _success(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}

    # ==========================================
    # Function: Construct one JSON-RPC protocol error.
    # Method: Echo an available request ID and include standard code/message fields.
    # ==========================================
    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    # ==========================================
    # Function: Handle one decoded JSON-RPC message.
    # Method: Route advertised MCP methods and suppress replies for valid notifications.
    # ==========================================
    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return self._error(None, -32600, "request must be a JSON object")
        request_id = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != JSONRPC_VERSION or not isinstance(method, str):
            return self._error(request_id, -32600, "invalid JSON-RPC request")
        params = message.get("params", {})
        is_notification = "id" not in message

        if method == "initialize":
            if is_notification:
                return None
            if not isinstance(params, dict):
                return self._error(request_id, -32602, "initialize params must be an object")
            protocol_version = params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
            if not isinstance(protocol_version, str) or protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
                protocol_version = DEFAULT_PROTOCOL_VERSION
            self.initialized = True
            return self._success(
                request_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "agent-bridge", "version": __version__},
                },
            )
        if not self.initialized:
            return None if is_notification else self._error(request_id, -32002, "server is not initialized")
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return None if is_notification else self._success(request_id, {})
        if method == "tools/list":
            return None if is_notification else self._success(request_id, {"tools": tool_definitions()})
        if method == "tools/call":
            if is_notification:
                return None
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return self._error(request_id, -32602, "tools/call requires name and object arguments")
            arguments = params.get("arguments", {})
            return self._success(request_id, self.service.call_tool(params["name"], arguments))
        return None if is_notification else self._error(request_id, -32601, f"method not found: {method}")

    # ==========================================
    # Function: Process one JSON-RPC value under the MCP no-batch transport rule.
    # Method: Reject arrays as invalid requests and route one object message.
    # ==========================================
    def process_value(self, value: Any) -> dict[str, Any] | None:
        if isinstance(value, list):
            return self._error(None, -32600, "JSON-RPC batching is not supported by MCP")
        return self.handle(value)

    # ==========================================
    # Function: Serve newline-delimited JSON-RPC until stdin closes.
    # Method: Decode each line independently, report parse errors, and flush every response.
    # ==========================================
    def serve(self) -> None:
        for raw_line in sys.stdin.buffer:
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
                response = self.process_value(value)
            except json.JSONDecodeError as exc:
                response = self._error(None, -32700, f"parse error: {exc.msg}")
            except Exception as exc:
                print(f"Agent Bridge internal error: {exc}", file=sys.stderr, flush=True)
                response = self._error(None, -32603, "internal error")
            if response is not None:
                print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)


# ==========================================
# Function: Convert termination signals into the normal server cleanup path.
# Method: Raise KeyboardInterrupt so main's finally block cancels active process groups.
# ==========================================
def handle_termination(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


# ==========================================
# Function: Start the Agent Bridge stdio MCP server.
# Method: Resolve plugin paths, install cleanup signals, serve requests, and close tasks on exit.
# ==========================================
def main() -> int:
    if sys.version_info < (3, 10):
        print("Agent Bridge requires Python 3.10 or newer.", file=sys.stderr)
        return 2
    plugin_root = Path(__file__).resolve().parents[2]
    try:
        service = BridgeService(plugin_root)
    except (ConfigError, TaskError, OSError) as exc:
        print(f"Agent Bridge startup failed: {exc}", file=sys.stderr)
        return 2
    previous_sigterm: Callable[..., Any] | int | None = signal.signal(
        signal.SIGTERM, handle_termination
    )
    try:
        MCPServer(service).serve()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        service.close()
    return 0
