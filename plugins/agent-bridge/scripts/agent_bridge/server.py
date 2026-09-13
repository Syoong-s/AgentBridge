"""Minimal dependency-free Model Context Protocol server for Agent Bridge."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
from typing import Any, Callable

from . import __version__
from .config import (
    AgentConfig,
    BridgeConfig,
    ConfigError,
    SelectionConfig,
    load_active_config,
    load_config,
)
from .tasks import TaskError, TaskManager


JSONRPC_VERSION = "2.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {DEFAULT_PROTOCOL_VERSION}
MAX_INFLIGHT_WAIT_REQUESTS = 32


# ==========================================
# Function: Define the MCP tools exposed to Codex.
# Method: Return strict JSON schemas for configuration, launch, observation, wait, and cancellation.
# ==========================================
def tool_definitions() -> list[dict[str, Any]]:
    task_read_properties = {
        "task_id": {
            "type": "string",
            "description": "External child-agent task identifier returned by a launch tool.",
        },
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
    launch_properties = {
        "agent": {"type": "string", "description": "Configured external-agent alias."},
        "prompt": {"type": "string", "description": "Task prompt sent through the configured mode."},
        "cwd": {
            "type": "string",
            "description": "Explicit existing working directory for the external child agent.",
        },
        "model": {
            "type": "string",
            "description": "Optional model alias/name mapped through this agent's parameter configuration.",
        },
        "reasoning_effort": {
            "type": "string",
            "description": "Optional thinking/effort level mapped and validated by this agent alias.",
        },
        "parent_task_id": {
            "type": "string",
            "description": "Optional Agent Bridge parent task for external child-agent lineage only.",
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
            "name": "start_child_agent",
            "description": (
                "Start a plugin-managed external child-agent task with optional model, "
                "reasoning effort, and lineage, then return its task ID."
            ),
            "inputSchema": {
                "type": "object",
                "properties": launch_properties,
                "required": ["agent", "prompt", "cwd"],
                "additionalProperties": False,
            },
        },
        {
            "name": "start_task",
            "description": (
                "Backward-compatible alias of start_child_agent for launching an external CLI task."
            ),
            "inputSchema": {
                "type": "object",
                "properties": launch_properties,
                "required": ["agent", "prompt", "cwd"],
                "additionalProperties": False,
            },
        },
        {
            "name": "send_followup",
            "description": (
                "Resume the latest terminal provider session as a new linked external child-agent task."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "parent_task_id": {
                        "type": "string",
                        "description": "Latest terminal task in the provider session to resume.",
                    },
                    "prompt": {"type": "string", "description": "Follow-up instruction."},
                    "extra_args": launch_properties["extra_args"],
                    "timeout_sec": launch_properties["timeout_sec"],
                },
                "required": ["parent_task_id", "prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_task",
            "description": "Read external child-agent metadata, lineage, and paginated output without waiting.",
            "inputSchema": {
                "type": "object",
                "properties": task_read_properties,
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "wait_task",
            "description": (
                "Return on unread output, task progress/completion, cancellation, or after waiting "
                "up to 50 seconds, with current metadata and output."
            ),
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
            "description": "List recent external child-agent task metadata and lineage.",
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
# Function: Summarize one configured portable selector without exposing argv templates.
# Method: Report override support, default value, and an optional exact allowlist.
# ==========================================
def selection_status(mapping: SelectionConfig | None) -> dict[str, Any]:
    return {
        "supported": mapping is not None,
        "default": mapping.default if mapping is not None else None,
        "allowed_values": list(mapping.allowed_values) if mapping and mapping.allowed_values else None,
    }


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
                    "task_kind": "external_child_agent",
                    "model": selection_status(agent.model),
                    "reasoning_effort": selection_status(agent.reasoning_effort),
                    "supports_followup": agent.session is not None,
                    "executable": executable_status(agent),
                }
            )
        return {
            "config_path": str(config.path),
            "state_dir": str(config.state_dir),
            "max_concurrent_tasks": config.max_concurrent_tasks,
            "max_retained_tasks": config.max_retained_tasks,
            "allowed_work_roots": [str(path) for path in config.allowed_work_roots],
            "native_codex_subagents": False,
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
    def call_tool(
        self,
        name: str,
        arguments: Any,
        cancellation_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return tool_failure("tool arguments must be a JSON object")
        try:
            if name == "list_agents":
                validate_tool_arguments(arguments, set())
                return tool_success(self.list_agents())
            if name == "reload_config":
                validate_tool_arguments(arguments, set())
                return tool_success(self.reload_config())
            if name in {"start_child_agent", "start_task"}:
                validate_tool_arguments(
                    arguments,
                    {"agent", "prompt", "cwd"},
                    {
                        "extra_args",
                        "timeout_sec",
                        "model",
                        "reasoning_effort",
                        "parent_task_id",
                    },
                )
                return tool_success(
                    self.manager.start_task(
                        alias=arguments.get("agent"),
                        prompt=arguments.get("prompt"),
                        cwd=arguments.get("cwd"),
                        extra_args=arguments.get("extra_args"),
                        timeout_sec=arguments.get("timeout_sec"),
                        model=arguments.get("model"),
                        reasoning_effort=arguments.get("reasoning_effort"),
                        parent_task_id=arguments.get("parent_task_id"),
                    )
                )
            if name == "send_followup":
                validate_tool_arguments(
                    arguments,
                    {"parent_task_id", "prompt"},
                    {"extra_args", "timeout_sec"},
                )
                return tool_success(
                    self.manager.send_followup(
                        parent_task_id=arguments.get("parent_task_id"),
                        prompt=arguments.get("prompt"),
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
                        cancellation_event=cancellation_event,
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
        self._output_lock = threading.Lock()
        self._inflight_lock = threading.Lock()
        self._inflight_waits: dict[str, threading.Event] = {}
        self._wait_executor = ThreadPoolExecutor(
            max_workers=MAX_INFLIGHT_WAIT_REQUESTS,
            thread_name_prefix="agent-bridge-wait",
        )

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
    # Function: Canonicalize one JSON-RPC request ID for in-flight lookup.
    # Method: Serialize parsed JSON deterministically so string and numeric IDs remain distinct.
    # ==========================================
    @staticmethod
    def _request_key(request_id: Any) -> str:
        return json.dumps(request_id, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    # ==========================================
    # Function: Detect a response-bearing wait_task call eligible for asynchronous dispatch.
    # Method: Inspect only the JSON-RPC envelope and defer full validation to normal handling.
    # ==========================================
    @staticmethod
    def _is_wait_request(value: Any) -> bool:
        if not isinstance(value, dict) or "id" not in value:
            return False
        if value.get("method") != "tools/call":
            return False
        params = value.get("params")
        return isinstance(params, dict) and params.get("name") == "wait_task"

    # ==========================================
    # Function: Emit one complete JSON-RPC response without cross-thread interleaving.
    # Method: Serialize before acquiring a dedicated stdout lock and flush the line atomically.
    # ==========================================
    def _emit_response(self, response: dict[str, Any]) -> None:
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        with self._output_lock:
            print(encoded, flush=True)

    # ==========================================
    # Function: Cancel one matching in-flight wait request without stopping its provider task.
    # Method: Correlate the notification requestId and set only the RPC-local cancellation event.
    # ==========================================
    def _cancel_wait_request(self, params: Any) -> None:
        if not isinstance(params, dict) or "requestId" not in params:
            return
        key = self._request_key(params["requestId"])
        with self._inflight_lock:
            cancellation_event = self._inflight_waits.get(key)
            if cancellation_event is not None:
                cancellation_event.set()

    # ==========================================
    # Function: Handle one decoded JSON-RPC message.
    # Method: Route advertised MCP methods and suppress replies for valid notifications.
    # ==========================================
    def handle(
        self,
        message: Any,
        cancellation_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
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
        if method == "notifications/initialized":
            return None
        if method == "notifications/cancelled":
            self._cancel_wait_request(params)
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
            return self._success(
                request_id,
                self.service.call_tool(
                    params["name"],
                    arguments,
                    cancellation_event=cancellation_event,
                ),
            )
        return None if is_notification else self._error(request_id, -32601, f"method not found: {method}")

    # ==========================================
    # Function: Process one JSON-RPC value under the MCP no-batch transport rule.
    # Method: Reject arrays as invalid requests and route one object message.
    # ==========================================
    def process_value(
        self,
        value: Any,
        cancellation_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        if isinstance(value, list):
            return self._error(None, -32600, "JSON-RPC batching is not supported by MCP")
        return self.handle(value, cancellation_event=cancellation_event)

    # ==========================================
    # Function: Execute one wait request outside the stdio reader loop.
    # Method: Reuse normal dispatch, retire its correlation entry, and suppress cancelled replies.
    # ==========================================
    def _run_wait_request(
        self,
        value: Any,
        key: str,
        cancellation_event: threading.Event,
    ) -> None:
        try:
            response = self.process_value(value, cancellation_event=cancellation_event)
        except Exception as exc:
            print(f"Agent Bridge internal error: {exc}", file=sys.stderr, flush=True)
            request_id = value.get("id") if isinstance(value, dict) else None
            response = self._error(request_id, -32603, "internal error")
        with self._inflight_lock:
            self._inflight_waits.pop(key, None)
            cancelled = cancellation_event.is_set()
        if not cancelled and response is not None:
            self._emit_response(response)

    # ==========================================
    # Function: Admit one asynchronous wait request with bounded concurrency.
    # Method: Reject duplicate or excessive in-flight IDs before submitting to the wait pool.
    # ==========================================
    def _submit_wait_request(self, value: dict[str, Any]) -> None:
        request_id = value.get("id")
        key = self._request_key(request_id)
        rejection: dict[str, Any] | None = None
        cancellation_event: threading.Event | None = None
        with self._inflight_lock:
            if key in self._inflight_waits:
                rejection = self._error(request_id, -32600, "duplicate in-flight request id")
            elif len(self._inflight_waits) >= MAX_INFLIGHT_WAIT_REQUESTS:
                rejection = self._error(request_id, -32000, "too many in-flight wait requests")
            else:
                cancellation_event = threading.Event()
                self._inflight_waits[key] = cancellation_event
        if rejection is not None:
            self._emit_response(rejection)
            return
        assert cancellation_event is not None
        try:
            self._wait_executor.submit(
                self._run_wait_request,
                value,
                key,
                cancellation_event,
            )
        except RuntimeError:
            with self._inflight_lock:
                self._inflight_waits.pop(key, None)
            self._emit_response(self._error(request_id, -32603, "wait dispatcher is unavailable"))

    # ==========================================
    # Function: Serve newline-delimited JSON-RPC until stdin closes.
    # Method: Decode each line independently, report parse errors, and flush every response.
    # ==========================================
    def serve(self) -> None:
        try:
            for raw_line in sys.stdin.buffer:
                if not raw_line.strip():
                    continue
                try:
                    value = json.loads(raw_line)
                    if self._is_wait_request(value):
                        self._submit_wait_request(value)
                        continue
                    response = self.process_value(value)
                except json.JSONDecodeError as exc:
                    response = self._error(None, -32700, f"parse error: {exc.msg}")
                except Exception as exc:
                    print(f"Agent Bridge internal error: {exc}", file=sys.stderr, flush=True)
                    response = self._error(None, -32603, "internal error")
                if response is not None:
                    self._emit_response(response)
        finally:
            with self._inflight_lock:
                for cancellation_event in self._inflight_waits.values():
                    cancellation_event.set()
            self._wait_executor.shutdown(wait=True, cancel_futures=True)


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
