"""Run, monitor, persist, and cancel configured external-agent processes."""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, BinaryIO, Iterable
import uuid

from .config import AgentConfig, BridgeConfig


TERMINAL_STATUSES = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}
RECOVERABLE_STATUSES = {"queued", "running", "cancelling"}
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_PROMPT_BYTES = 1024 * 1024
MAX_EXTRA_ARGUMENTS = 64
MAX_EXTRA_ARGUMENT_BYTES = 4096
MAX_SELECTOR_BYTES = 256
MAX_SESSION_ID_BYTES = 1024
PLACEHOLDER_PATTERN = re.compile(r"\{(?:prompt|prompt_file|cwd|task_id)\}")
_UNSET = object()


# ==========================================
# Class: Task request or lifecycle failure safe to return through MCP.
# Method: Specialize RuntimeError so tool dispatch can distinguish expected failures.
# ==========================================
class TaskError(RuntimeError):
    """Raised for invalid task requests or unavailable task state."""


# ==========================================
# Function: Produce a stable UTC timestamp for persisted task metadata.
# Method: Emit a microsecond-resolution ISO-8601 value with a Z suffix.
# ==========================================
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# ==========================================
# Function: Write one JSON object atomically.
# Method: Replace through a task-local temporary file and restrict file permissions.
# ==========================================
def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# ==========================================
# Function: Read a Linux process start marker for PID-reuse protection.
# Method: Extract field 22 from /proc/PID/stat after the parenthesized command name.
# ==========================================
def process_start_ticks(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = text.rfind(")")
        fields = text[closing + 2 :].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


# ==========================================
# Function: Test whether one prior process still matches its persisted identity.
# Method: Compare PID and Linux process-start ticks to avoid signaling a reused PID.
# ==========================================
def process_identity_matches(pid: int | None, expected_ticks: int | None) -> bool:
    if pid is None or expected_ticks is None or pid <= 0:
        return False
    return process_start_ticks(pid) == expected_ticks


# ==========================================
# Function: Terminate one recovered stale process group without risking PID reuse.
# Method: Recheck identity around SIGTERM, wait briefly, then escalate matching survivors to SIGKILL.
# ==========================================
def terminate_stale_process_group(pid: int | None, expected_ticks: int | None) -> None:
    if not process_identity_matches(pid, expected_ticks):
        return
    assert pid is not None
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if not process_identity_matches(pid, expected_ticks):
            return
        time.sleep(0.02)
    if process_identity_matches(pid, expected_ticks):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


# ==========================================
# Function: Replace supported placeholders without invoking a shell.
# Method: Replace matches in the original template once so inserted prompt text is never rewritten.
# ==========================================
def substitute(value: str, replacements: dict[str, str]) -> str:
    return PLACEHOLDER_PATTERN.sub(lambda match: replacements[match.group(0)], value)


# ==========================================
# Function: Normalize PATH entries for pre-launch executable validation.
# Method: Resolve empty and relative entries against the child task cwd, matching exec after chdir.
# ==========================================
def executable_search_path(search_path: str, cwd: Path) -> str:
    normalized: list[str] = []
    for entry in search_path.split(os.pathsep):
        candidate = Path(entry) if entry else Path(".")
        normalized.append(str(candidate if candidate.is_absolute() else cwd / candidate))
    return os.pathsep.join(normalized)


# ==========================================
# Class: Mutable in-memory state for one launched task.
# Method: Pair persisted metadata with live process/thread synchronization handles.
# ==========================================
@dataclass
class TaskRecord:
    task_id: str
    agent: str
    status: str
    cwd: str
    command: list[str]
    created_at: str
    timeout_sec: int
    max_output_bytes: int
    task_kind: str = "external_child_agent"
    invocation: str = "start"
    parent_task_id: str | None = None
    root_task_id: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    session_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    return_code: int | None = None
    error: str | None = None
    persistence_error: str | None = None
    pid: int | None = None
    process_start_ticks: int | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    worker: threading.Thread | None = field(default=None, repr=False)
    cancel_requested: threading.Event = field(default_factory=threading.Event, repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    changed: threading.Event = field(default_factory=threading.Event, repr=False)
    change_sequence: int = field(default=0, repr=False)


# ==========================================
# Class: Mutable outcome from one asynchronous stream-capture worker.
# Method: Record the first capture failure for terminal task-state reconciliation.
# ==========================================
@dataclass
class StreamCaptureState:
    error: str | None = None


# ==========================================
# Class: Thread-safe asynchronous external-agent task manager.
# Method: Validate launches, bound streams, persist state, and control POSIX process groups.
# ==========================================
class TaskManager:
    # ==========================================
    # Function: Initialize state storage and recover prior task metadata.
    # Method: Create a private state root, load valid task directories, and interrupt stale runs.
    # ==========================================
    def __init__(self, config: BridgeConfig) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._tasks: dict[str, TaskRecord] = {}
        self._closed = False
        config.state_dir.mkdir(parents=True, exist_ok=True)
        config.state_dir.chmod(0o700)
        self._recover_tasks()

    # ==========================================
    # Function: Return the currently active immutable configuration.
    # Method: Read the reference under the manager lock.
    # ==========================================
    @property
    def config(self) -> BridgeConfig:
        with self._lock:
            return self._config

    # ==========================================
    # Function: Replace configuration for future launches.
    # Method: Require the same state root so live and persisted task ownership cannot split.
    # ==========================================
    def replace_config(self, config: BridgeConfig) -> None:
        with self._lock:
            if config.state_dir != self._config.state_dir:
                raise TaskError("state_dir cannot change during a running MCP server; restart it")
            self._config = config

    # ==========================================
    # Function: Build the on-disk directory for a validated task identifier.
    # Method: Join only identifiers accepted by the strict task ID pattern.
    # ==========================================
    def _task_dir(self, task_id: str) -> Path:
        if TASK_ID_PATTERN.fullmatch(task_id) is None:
            raise TaskError("invalid task_id")
        return self._config.state_dir / task_id

    # ==========================================
    # Function: Convert mutable task state into its persisted JSON representation.
    # Method: Select non-secret metadata and derive current bounded log sizes.
    # ==========================================
    def _metadata(self, record: TaskRecord) -> dict[str, Any]:
        task_dir = self._task_dir(record.task_id)
        stdout_path = task_dir / "stdout.log"
        stderr_path = task_dir / "stderr.log"
        return {
            "schema_version": 1,
            "task_id": record.task_id,
            "task_kind": record.task_kind,
            "agent": record.agent,
            "invocation": record.invocation,
            "parent_task_id": record.parent_task_id,
            "root_task_id": record.root_task_id or record.task_id,
            "model": record.model,
            "reasoning_effort": record.reasoning_effort,
            "session_id": record.session_id,
            "status": record.status,
            "cwd": record.cwd,
            "command": record.command,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "timeout_sec": record.timeout_sec,
            "max_output_bytes": record.max_output_bytes,
            "return_code": record.return_code,
            "error": record.error,
            "persistence_error": record.persistence_error,
            "pid": record.pid,
            "process_start_ticks": record.process_start_ticks,
            "stdout_bytes": stdout_path.stat().st_size if stdout_path.is_file() else 0,
            "stderr_bytes": stderr_path.stat().st_size if stderr_path.is_file() else 0,
            "stdout_truncated": record.stdout_truncated,
            "stderr_truncated": record.stderr_truncated,
        }

    # ==========================================
    # Function: Persist one task record atomically.
    # Method: Serialize the selected metadata into the task-owned directory.
    # ==========================================
    def _save_record(self, record: TaskRecord) -> None:
        atomic_write_json(self._task_dir(record.task_id) / "metadata.json", self._metadata(record))

    # ==========================================
    # Function: Add one diagnostic without discarding an earlier task failure.
    # Method: Join distinct messages in observation order for one public error field.
    # ==========================================
    @staticmethod
    def _append_record_error(record: TaskRecord, message: str) -> None:
        record.error = f"{record.error}; {message}" if record.error else message

    # ==========================================
    # Function: Publish one task change to every current or future waiter.
    # Method: Increment a monotonic sequence under lock before setting the shared wake event.
    # ==========================================
    def _signal_change(self, record: TaskRecord) -> None:
        with self._lock:
            record.change_sequence += 1
            record.changed.set()

    # ==========================================
    # Function: Persist a terminal task result without leaking exceptions from its worker.
    # Method: Convert failed writes into a conservative failed result and retry that diagnosis once.
    # ==========================================
    def _persist_terminal_record(self, record: TaskRecord) -> None:
        try:
            self._save_record(record)
            return
        except Exception as exc:
            message = f"terminal metadata persistence failed: {exc}"
            record.status = "failed"
            record.persistence_error = message
            self._append_record_error(record, message)
        try:
            self._save_record(record)
        except Exception as exc:
            message = f"terminal metadata persistence retry failed: {exc}"
            record.persistence_error = f"{record.persistence_error}; {message}"
            self._append_record_error(record, message)

    # ==========================================
    # Function: Rehydrate a task record from validated persisted metadata.
    # Method: Type-check required fields and ignore transient process handles.
    # ==========================================
    def _record_from_metadata(self, value: dict[str, Any]) -> TaskRecord:
        required_strings = ("task_id", "agent", "status", "cwd", "created_at")
        if any(not isinstance(value.get(key), str) for key in required_strings):
            raise ValueError("missing string metadata fields")
        command = value.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValueError("invalid command metadata")
        timeout_sec = value.get("timeout_sec")
        max_output_bytes = value.get("max_output_bytes")
        if not isinstance(timeout_sec, int) or not isinstance(max_output_bytes, int):
            raise ValueError("invalid task limit metadata")
        if value["status"] not in TERMINAL_STATUSES | RECOVERABLE_STATUSES:
            raise ValueError("invalid task status metadata")
        task_kind = value.get("task_kind", "external_child_agent")
        invocation = value.get("invocation", "start")
        if task_kind != "external_child_agent" or invocation not in {"start", "followup"}:
            raise ValueError("invalid external child-agent metadata")
        optional_ids = ("parent_task_id", "root_task_id")
        for key in optional_ids:
            item = value.get(key)
            if item is not None and (
                not isinstance(item, str) or TASK_ID_PATTERN.fullmatch(item) is None
            ):
                raise ValueError(f"invalid {key} metadata")
        for key in ("model", "reasoning_effort", "session_id"):
            item = value.get(key)
            maximum = MAX_SESSION_ID_BYTES if key == "session_id" else MAX_SELECTOR_BYTES
            if item is not None and (
                not isinstance(item, str)
                or not item
                or "\0" in item
                or len(item.encode("utf-8")) > maximum
            ):
                raise ValueError(f"invalid {key} metadata")
        return TaskRecord(
            task_id=value["task_id"],
            agent=value["agent"],
            status=value["status"],
            cwd=value["cwd"],
            command=command,
            created_at=value["created_at"],
            timeout_sec=timeout_sec,
            max_output_bytes=max_output_bytes,
            task_kind=task_kind,
            invocation=invocation,
            parent_task_id=value.get("parent_task_id"),
            root_task_id=value.get("root_task_id") or value["task_id"],
            model=value.get("model"),
            reasoning_effort=value.get("reasoning_effort"),
            session_id=value.get("session_id"),
            started_at=value.get("started_at") if isinstance(value.get("started_at"), str) else None,
            finished_at=value.get("finished_at") if isinstance(value.get("finished_at"), str) else None,
            return_code=value.get("return_code") if isinstance(value.get("return_code"), int) else None,
            error=value.get("error") if isinstance(value.get("error"), str) else None,
            persistence_error=(
                value.get("persistence_error")
                if isinstance(value.get("persistence_error"), str)
                else None
            ),
            pid=value.get("pid") if isinstance(value.get("pid"), int) else None,
            process_start_ticks=(
                value.get("process_start_ticks")
                if isinstance(value.get("process_start_ticks"), int)
                else None
            ),
            stdout_truncated=value.get("stdout_truncated") is True,
            stderr_truncated=value.get("stderr_truncated") is True,
        )

    # ==========================================
    # Function: Recover task history and neutralize stale prior processes.
    # Method: Load only self-describing task directories and safely kill matching stale groups.
    # ==========================================
    def _recover_tasks(self) -> None:
        for child in sorted(self._config.state_dir.iterdir()):
            metadata_path = child / "metadata.json"
            if not child.is_dir() or not metadata_path.is_file():
                continue
            try:
                value = json.loads(metadata_path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("schema_version") != 1:
                    continue
                record = self._record_from_metadata(value)
                if record.task_id != child.name or TASK_ID_PATTERN.fullmatch(record.task_id) is None:
                    continue
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if record.status in RECOVERABLE_STATUSES:
                terminate_stale_process_group(record.pid, record.process_start_ticks)
                record.status = "interrupted"
                record.finished_at = utc_now()
                record.error = "MCP server restarted before the task reached a terminal state"
                self._save_record(record)
            if record.status in TERMINAL_STATUSES:
                record.done.set()
            self._tasks[record.task_id] = record
        self._prune_history()

    # ==========================================
    # Function: Count tasks currently consuming execution slots.
    # Method: Include queued, running, and cancellation-in-progress records.
    # ==========================================
    def _active_count(self) -> int:
        return sum(record.status in RECOVERABLE_STATUSES for record in self._tasks.values())

    # ==========================================
    # Function: Validate and canonicalize a requested working directory.
    # Method: Resolve symlinks, require an existing directory, and enforce configured roots.
    # ==========================================
    def _resolve_cwd(self, raw_cwd: str) -> Path:
        if not isinstance(raw_cwd, str) or not raw_cwd or "\0" in raw_cwd:
            raise TaskError("cwd must be a non-empty NUL-free string")
        try:
            cwd = Path(raw_cwd).expanduser().resolve()
        except (OSError, ValueError) as exc:
            raise TaskError(f"cwd could not be resolved: {exc}") from exc
        if not cwd.is_dir():
            raise TaskError(f"cwd is not an existing directory: {cwd}")
        roots = self._config.allowed_work_roots
        if roots and not any(cwd == root or root in cwd.parents for root in roots):
            raise TaskError(f"cwd is outside configured allowed_work_roots: {cwd}")
        return cwd

    # ==========================================
    # Function: Validate runtime arguments appended to a configured argv.
    # Method: Bound count and byte length while preserving every argument as a discrete value.
    # ==========================================
    def _validate_extra_args(self, raw_args: Any, agent: AgentConfig) -> list[str]:
        if raw_args is None:
            return []
        if not agent.allow_extra_args:
            raise TaskError(f"agent {agent.alias!r} does not allow extra_args")
        if not isinstance(raw_args, list) or len(raw_args) > MAX_EXTRA_ARGUMENTS:
            raise TaskError(f"extra_args must be an array with at most {MAX_EXTRA_ARGUMENTS} items")
        parsed: list[str] = []
        for argument in raw_args:
            if not isinstance(argument, str) or "\0" in argument:
                raise TaskError("every extra_args item must be a NUL-free string")
            if len(argument.encode("utf-8")) > MAX_EXTRA_ARGUMENT_BYTES:
                raise TaskError(f"each extra_args item must be at most {MAX_EXTRA_ARGUMENT_BYTES} bytes")
            parsed.append(argument)
        return parsed

    # ==========================================
    # Function: Build an execution environment for one task.
    # Method: Optionally inherit the server environment and substitute configured literal placeholders.
    # ==========================================
    def _build_environment(
        self,
        agent: AgentConfig,
        replacements: dict[str, str],
    ) -> dict[str, str]:
        environment = dict(os.environ) if agent.inherit_env else {}
        for key, value in agent.environment.items():
            environment[key] = substitute(value, replacements)
        environment["PWD"] = replacements["{cwd}"]
        return environment

    # ==========================================
    # Function: Verify that the configured executable can be launched.
    # Method: Resolve slash-containing paths against cwd or search the task PATH.
    # ==========================================
    def _validate_executable(self, executable: str, cwd: Path, environment: dict[str, str]) -> None:
        if "/" in executable:
            path = Path(executable)
            candidate = path if path.is_absolute() else cwd / path
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise TaskError(f"configured executable is not runnable: {candidate}")
            return
        search_path = executable_search_path(environment.get("PATH", os.defpath), cwd)
        if shutil.which(executable, path=search_path) is None:
            raise TaskError(f"configured executable was not found on PATH: {executable}")

    # ==========================================
    # Function: Resolve one optional model or reasoning selector.
    # Method: Apply the configured default, enforce the allowlist, and expand provider argv once.
    # ==========================================
    def _selection_arguments(
        self,
        agent: AgentConfig,
        field_name: str,
        requested: Any,
    ) -> tuple[str | None, list[str]]:
        mapping = getattr(agent, field_name)
        if requested is not None and (
            not isinstance(requested, str)
            or not requested
            or "\0" in requested
            or len(requested.encode("utf-8")) > MAX_SELECTOR_BYTES
        ):
            raise TaskError(
                f"{field_name} must be null or a non-empty NUL-free string up to "
                f"{MAX_SELECTOR_BYTES} bytes"
            )
        if mapping is None:
            if requested is not None:
                raise TaskError(f"agent {agent.alias!r} does not configure {field_name} selection")
            return None, []
        selected = mapping.default if requested is None else requested
        if selected is None:
            return None, []
        if mapping.allowed_values is not None and selected not in mapping.allowed_values:
            raise TaskError(
                f"{field_name} for agent {agent.alias!r} must be one of "
                f"{list(mapping.allowed_values)}"
            )
        placeholder = "{model}" if field_name == "model" else "{reasoning_effort}"
        arguments = [
            re.sub(re.escape(placeholder), lambda _match: selected, argument)
            for argument in mapping.arguments
        ]
        return selected, arguments

    # ==========================================
    # Function: Expand provider session arguments for a new or resumed conversation.
    # Method: Generate UUID sessions when configured and substitute exactly one session ID placeholder.
    # ==========================================
    def _session_arguments(
        self,
        agent: AgentConfig,
        resume_session_id: str | None,
    ) -> tuple[str | None, list[str]]:
        session = agent.session
        if resume_session_id is not None:
            if (
                not isinstance(resume_session_id, str)
                or not resume_session_id
                or "\0" in resume_session_id
                or len(resume_session_id.encode("utf-8")) > MAX_SESSION_ID_BYTES
            ):
                raise TaskError("provider session ID is invalid")
            if session is None:
                raise TaskError(f"agent {agent.alias!r} does not configure resumable sessions")
            session_id = resume_session_id
            templates = session.resume_arguments
        elif session is not None and session.id_source == "generated_uuid":
            session_id = str(uuid.uuid4())
            templates = session.start_arguments
        elif session is not None:
            session_id = None
            templates = session.start_arguments
        else:
            return None, []
        arguments = [
            re.sub(re.escape("{session_id}"), lambda _match: session_id or "", argument)
            for argument in templates
        ]
        return session_id, arguments

    # ==========================================
    # Function: Validate an optional parent task and derive the lineage root.
    # Method: Require a known bridge task identifier and inherit its root task ID.
    # ==========================================
    def _lineage_root(self, parent_task_id: Any) -> str | None:
        if parent_task_id is None:
            return None
        if not isinstance(parent_task_id, str) or TASK_ID_PATTERN.fullmatch(parent_task_id) is None:
            raise TaskError("parent_task_id must be a valid string identifier")
        parent = self._tasks.get(parent_task_id)
        if parent is None:
            raise TaskError(f"unknown parent_task_id {parent_task_id!r}")
        return parent.root_task_id or parent.task_id

    # ==========================================
    # Function: Start one configured external child-agent task asynchronously.
    # Method: Validate selectors and lineage, assemble direct argv, persist state, and launch a worker.
    # ==========================================
    def start_task(
        self,
        alias: str,
        prompt: str,
        cwd: str,
        extra_args: Any = None,
        timeout_sec: int | None = None,
        model: Any = None,
        reasoning_effort: Any = None,
        parent_task_id: Any = None,
        *,
        _resume_session_id: str | None = None,
        _invocation: str = "start",
        _effective_model: Any = _UNSET,
        _effective_reasoning_effort: Any = _UNSET,
    ) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise TaskError("prompt must be a non-empty string")
        if not isinstance(alias, str):
            raise TaskError("agent must be a string alias")
        prompt_bytes = prompt.encode("utf-8")
        if len(prompt_bytes) > MAX_PROMPT_BYTES:
            raise TaskError(f"prompt must be at most {MAX_PROMPT_BYTES} UTF-8 bytes")

        with self._lock:
            if self._closed:
                raise TaskError("task manager is shutting down")
            agent = self._config.agents.get(alias)
            if agent is None:
                raise TaskError(f"unknown agent alias {alias!r}")
            if not agent.enabled:
                raise TaskError(f"agent alias {alias!r} is disabled in the parameter file")
            if self._active_count() >= self._config.max_concurrent_tasks:
                raise TaskError("maximum concurrent task limit reached")
            resolved_cwd = self._resolve_cwd(cwd)
            parsed_extra_args = self._validate_extra_args(extra_args, agent)
            if _effective_model is _UNSET:
                selected_model, model_arguments = self._selection_arguments(
                    agent, "model", model
                )
            elif _effective_model is None:
                selected_model, model_arguments = None, []
            else:
                selected_model, model_arguments = self._selection_arguments(
                    agent, "model", _effective_model
                )
            if _effective_reasoning_effort is _UNSET:
                selected_effort, effort_arguments = self._selection_arguments(
                    agent, "reasoning_effort", reasoning_effort
                )
            elif _effective_reasoning_effort is None:
                selected_effort, effort_arguments = None, []
            else:
                selected_effort, effort_arguments = self._selection_arguments(
                    agent, "reasoning_effort", _effective_reasoning_effort
                )
            root_task_id = self._lineage_root(parent_task_id)
            effective_timeout = agent.timeout_sec if timeout_sec is None else timeout_sec
            if isinstance(effective_timeout, bool) or not isinstance(effective_timeout, int):
                raise TaskError("timeout_sec must be an integer")
            if not 1 <= effective_timeout <= self._config.max_timeout_sec:
                raise TaskError(
                    f"timeout_sec must be between 1 and {self._config.max_timeout_sec}"
                )

            task_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
            task_dir = self._task_dir(task_id)
            prompt_file = task_dir / "prompt.txt"
            replacements = {
                "{prompt}": prompt,
                "{prompt_file}": str(prompt_file),
                "{cwd}": str(resolved_cwd),
                "{task_id}": task_id,
            }
            redacted_replacements = {
                "{prompt}": "<prompt>",
                "{prompt_file}": "<prompt-file>",
                "{cwd}": str(resolved_cwd),
                "{task_id}": task_id,
            }
            session_id, session_arguments = self._session_arguments(
                agent,
                _resume_session_id,
            )
            base_command = [substitute(argument, replacements) for argument in agent.command]
            insertion_index = next(
                (
                    index
                    for index, argument in enumerate(agent.command)
                    if "{prompt}" in argument or "{prompt_file}" in argument
                ),
                len(base_command),
            )
            control_arguments = model_arguments + effort_arguments + session_arguments
            command = (
                base_command[:insertion_index]
                + control_arguments
                + base_command[insertion_index:]
            )
            command.extend(parsed_extra_args)
            redacted_base_command = [
                substitute(argument, redacted_replacements) for argument in agent.command
            ]
            redacted_command = (
                redacted_base_command[:insertion_index]
                + control_arguments
                + redacted_base_command[insertion_index:]
            )
            redacted_command.extend("<extra-arg>" for _argument in parsed_extra_args)
            environment = self._build_environment(agent, replacements)
            self._validate_executable(command[0], resolved_cwd, environment)
            try:
                task_dir.mkdir(mode=0o700)
                if agent.prompt_mode == "file":
                    prompt_file.write_bytes(prompt_bytes)
                    prompt_file.chmod(0o600)
            except OSError as exc:
                shutil.rmtree(task_dir, ignore_errors=True)
                raise TaskError(f"cannot initialize private task directory: {exc}") from exc

            record = TaskRecord(
                task_id=task_id,
                agent=alias,
                status="queued",
                cwd=str(resolved_cwd),
                command=redacted_command,
                created_at=utc_now(),
                timeout_sec=effective_timeout,
                max_output_bytes=agent.max_output_bytes,
                invocation=_invocation,
                parent_task_id=parent_task_id,
                root_task_id=root_task_id or task_id,
                model=selected_model,
                reasoning_effort=selected_effort,
                session_id=session_id,
            )
            self._tasks[task_id] = record
            try:
                self._save_record(record)
            except OSError as exc:
                self._tasks.pop(task_id, None)
                shutil.rmtree(task_dir, ignore_errors=True)
                raise TaskError(f"cannot initialize task state: {exc}") from exc
            worker = threading.Thread(
                target=self._run_task,
                name=f"agent-bridge-{task_id}",
                args=(record, agent, command, environment, prompt_bytes),
                daemon=True,
            )
            record.worker = worker
            worker.start()
            self._prune_history()
            return self._public_record(record)

    # ==========================================
    # Function: Resume one completed external child-agent conversation as a child task.
    # Method: Enforce linear session use and inherit provider, cwd, selectors, limits, and lineage.
    # ==========================================
    def send_followup(
        self,
        parent_task_id: Any,
        prompt: str,
        extra_args: Any = None,
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if not isinstance(parent_task_id, str) or TASK_ID_PATTERN.fullmatch(parent_task_id) is None:
                raise TaskError("parent_task_id must be a valid string identifier")
            parent = self._tasks.get(parent_task_id)
            if parent is None:
                raise TaskError(f"unknown parent_task_id {parent_task_id!r}")
            if parent.status not in TERMINAL_STATUSES:
                raise TaskError("follow-up requires a terminal parent task")
            if parent.session_id is None:
                raise TaskError("parent task has no resumable provider session ID")
            agent = self._config.agents.get(parent.agent)
            if agent is None or agent.session is None:
                raise TaskError(
                    f"agent {parent.agent!r} no longer configures resumable sessions"
                )
            same_session = [
                record
                for record in self._tasks.values()
                if record.agent == parent.agent and record.session_id == parent.session_id
            ]
            if any(record.status in RECOVERABLE_STATUSES for record in same_session):
                raise TaskError("provider session already has an active task")
            latest = max(same_session, key=lambda record: (record.created_at, record.task_id))
            if latest.task_id != parent.task_id:
                raise TaskError(
                    f"follow-up parent is stale; continue from latest task_id {latest.task_id!r}"
                )
            return self.start_task(
                alias=parent.agent,
                prompt=prompt,
                cwd=parent.cwd,
                extra_args=extra_args,
                timeout_sec=parent.timeout_sec if timeout_sec is None else timeout_sec,
                parent_task_id=parent.task_id,
                _resume_session_id=parent.session_id,
                _invocation="followup",
                _effective_model=parent.model,
                _effective_reasoning_effort=parent.reasoning_effort,
            )

    # ==========================================
    # Function: Preserve the first failure observed by one stream-capture worker.
    # Method: Attach the stream and operation names without overwriting the root cause.
    # ==========================================
    @staticmethod
    def _set_capture_error(
        state: StreamCaptureState,
        stream_name: str,
        operation: str,
        error: BaseException,
    ) -> None:
        if state.error is None:
            state.error = f"{stream_name} {operation} failed: {error}"

    # ==========================================
    # Function: Drain one child stream while enforcing its persisted byte ceiling.
    # Method: Persist bounded bytes, report I/O failures, and keep draining after write loss.
    # ==========================================
    def _drain_stream(
        self,
        stream: BinaryIO,
        path: Path,
        limit: int,
        record: TaskRecord,
        stream_name: str,
        state: StreamCaptureState,
    ) -> None:
        written = 0
        truncated = False
        destination: BinaryIO | None = None
        try:
            try:
                destination = path.open("wb")
            except OSError as exc:
                self._set_capture_error(state, stream_name, "log open", exc)
            try:
                path.chmod(0o600)
            except OSError as exc:
                self._set_capture_error(state, stream_name, "log permission update", exc)

            while True:
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except OSError as exc:
                    self._set_capture_error(state, stream_name, "pipe read", exc)
                    break
                if not chunk:
                    break
                if destination is None:
                    truncated = True
                    continue
                remaining = max(0, limit - written)
                if remaining:
                    accepted = chunk[:remaining]
                    try:
                        persisted = destination.write(accepted)
                        if persisted != len(accepted):
                            raise OSError(
                                f"short write: expected {len(accepted)} bytes, wrote {persisted}"
                            )
                        destination.flush()
                    except OSError as exc:
                        self._set_capture_error(state, stream_name, "log write", exc)
                        truncated = True
                        try:
                            destination.close()
                        except OSError as close_exc:
                            self._set_capture_error(
                                state,
                                stream_name,
                                "log close",
                                close_exc,
                            )
                        destination = None
                        continue
                    written += len(accepted)
                    self._signal_change(record)
                if len(chunk) > remaining:
                    truncated = True
        finally:
            if destination is not None:
                try:
                    destination.close()
                except OSError as exc:
                    self._set_capture_error(state, stream_name, "log close", exc)
            try:
                stream.close()
            except OSError as exc:
                self._set_capture_error(state, stream_name, "pipe close", exc)
            with self._lock:
                if stream_name == "stdout":
                    record.stdout_truncated = truncated
                else:
                    record.stderr_truncated = truncated
            self._signal_change(record)

    # ==========================================
    # Function: Contain unexpected exceptions at the stream-worker thread boundary.
    # Method: Mark capture failed, drain remaining pipe bytes best-effort, and always wake waiters.
    # ==========================================
    def _capture_stream(
        self,
        stream: BinaryIO,
        path: Path,
        limit: int,
        record: TaskRecord,
        stream_name: str,
        state: StreamCaptureState,
    ) -> None:
        try:
            self._drain_stream(stream, path, limit, record, stream_name, state)
        except Exception as exc:
            self._set_capture_error(state, stream_name, "worker", exc)
            while True:
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
            try:
                stream.close()
            except OSError:
                pass
            try:
                with self._lock:
                    if stream_name == "stdout":
                        record.stdout_truncated = True
                    else:
                        record.stderr_truncated = True
            except Exception:
                pass
        finally:
            try:
                self._signal_change(record)
            except Exception:
                pass

    # ==========================================
    # Function: Feed a prompt to an agent configured for stdin transport.
    # Method: Write bytes in a helper thread so a non-reading child cannot bypass timeout handling.
    # ==========================================
    def _write_stdin(self, process: subprocess.Popen[bytes], prompt_bytes: bytes) -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(prompt_bytes)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            try:
                process.stdin.close()
            except OSError:
                pass

    # ==========================================
    # Function: Signal a POSIX process group without raising for already-exited children.
    # Method: Address the group by its leader PID and tolerate lookup/permission races.
    # ==========================================
    @staticmethod
    def _signal_process_group(pid: int | None, selected_signal: signal.Signals) -> None:
        if pid is None or pid <= 0:
            return
        try:
            os.killpg(pid, selected_signal)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # ==========================================
    # Function: Test whether a POSIX process group still has members.
    # Method: Use signal zero and treat permission denial as evidence that the group exists.
    # ==========================================
    @staticmethod
    def _process_group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    # ==========================================
    # Function: Remove descendants left in a task group after its leader exits.
    # Method: Send TERM, allow a short grace period, then KILL remaining group members.
    # ==========================================
    def _terminate_remaining_group(self, pgid: int) -> None:
        if not self._process_group_exists(pgid):
            return
        self._signal_process_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if not self._process_group_exists(pgid):
                return
            time.sleep(0.02)
        if self._process_group_exists(pgid):
            self._signal_process_group(pgid, signal.SIGKILL)

    # ==========================================
    # Function: Terminate a live task process group with a bounded grace period.
    # Method: Send SIGTERM, wait briefly, then escalate to SIGKILL if required.
    # ==========================================
    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            self._signal_process_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._signal_process_group(process.pid, signal.SIGKILL)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self._terminate_remaining_group(process.pid)

    # ==========================================
    # Function: Extract a provider-generated session identifier from JSON stdout.
    # Method: Traverse the configured object path and require one bounded non-empty string.
    # ==========================================
    def _extract_stdout_session_id(self, record: TaskRecord, agent: AgentConfig) -> str:
        session = agent.session
        if session is None or session.id_source != "stdout_json":
            raise TaskError("agent does not configure stdout JSON session extraction")
        stdout_path = self._task_dir(record.task_id) / "stdout.log"
        try:
            value: Any = json.loads(stdout_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise TaskError(f"could not read provider JSON output: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TaskError(
                f"provider stdout is not valid JSON at line {exc.lineno}, column {exc.colno}"
            ) from exc
        for key in session.id_json_path:
            if not isinstance(value, dict) or key not in value:
                raise TaskError(
                    f"provider JSON output is missing session path {list(session.id_json_path)!r}"
                )
            value = value[key]
        if (
            not isinstance(value, str)
            or not value
            or "\0" in value
            or len(value.encode("utf-8")) > MAX_SESSION_ID_BYTES
        ):
            raise TaskError("provider session ID must be a non-empty NUL-free string up to 1024 bytes")
        return value

    # ==========================================
    # Function: Execute one task and transition it to exactly one terminal state.
    # Method: Capture bounded streams, extract provider sessions, honor cancellation, and persist state.
    # ==========================================
    def _run_task(
        self,
        record: TaskRecord,
        agent: AgentConfig,
        command: list[str],
        environment: dict[str, str],
        prompt_bytes: bytes,
    ) -> None:
        task_dir = self._task_dir(record.task_id)
        stdout_path = task_dir / "stdout.log"
        stderr_path = task_dir / "stderr.log"
        timed_out = False
        process: subprocess.Popen[bytes] | None = None
        stdout_state = StreamCaptureState()
        stderr_state = StreamCaptureState()
        final_status = "failed"
        final_error: str | None = None
        final_return_code: int | None = None
        final_session_id = record.session_id
        try:
            with self._lock:
                if record.cancel_requested.is_set():
                    final_status = "cancelled"
                    final_error = "task was cancelled before process launch"
                    return
                process = subprocess.Popen(
                    command,
                    cwd=record.cwd,
                    env=environment,
                    stdin=subprocess.PIPE if agent.prompt_mode == "stdin" else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                record.process = process
                record.pid = process.pid
                record.process_start_ticks = process_start_ticks(process.pid)
                record.status = "running"
                record.started_at = utc_now()
                self._signal_change(record)
                self._save_record(record)

            assert process.stdout is not None
            assert process.stderr is not None
            stdout_worker = threading.Thread(
                target=self._capture_stream,
                args=(
                    process.stdout,
                    stdout_path,
                    record.max_output_bytes,
                    record,
                    "stdout",
                    stdout_state,
                ),
                daemon=True,
            )
            stderr_worker = threading.Thread(
                target=self._capture_stream,
                args=(
                    process.stderr,
                    stderr_path,
                    record.max_output_bytes,
                    record,
                    "stderr",
                    stderr_state,
                ),
                daemon=True,
            )
            stdout_worker.start()
            stderr_worker.start()
            if agent.prompt_mode == "stdin":
                threading.Thread(
                    target=self._write_stdin,
                    args=(process, prompt_bytes),
                    daemon=True,
                ).start()

            deadline = time.monotonic() + record.timeout_sec
            while process.poll() is None:
                if record.cancel_requested.wait(timeout=0.1):
                    self._terminate_process(process)
                    break
                if process.poll() is not None:
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._terminate_process(process)
                    break
            return_code = process.wait()
            self._terminate_remaining_group(process.pid)
            for stream_name, worker, stream, state in (
                ("stdout", stdout_worker, process.stdout, stdout_state),
                ("stderr", stderr_worker, process.stderr, stderr_state),
            ):
                worker.join(timeout=5)
                if worker.is_alive():
                    self._set_capture_error(
                        state,
                        stream_name,
                        "worker completion",
                        TimeoutError("capture thread did not finish within 5 seconds"),
                    )
                    try:
                        stream.close()
                    except OSError as exc:
                        self._set_capture_error(state, stream_name, "pipe close", exc)
                    worker.join(timeout=1)
                    with self._lock:
                        if stream_name == "stdout":
                            record.stdout_truncated = True
                        else:
                            record.stderr_truncated = True
            capture_errors = [
                state.error for state in (stdout_state, stderr_state) if state.error is not None
            ]
            capture_error = "; ".join(capture_errors) if capture_errors else None
            session_error: str | None = None
            if (
                return_code == 0
                and not timed_out
                and not record.cancel_requested.is_set()
                and capture_error is None
                and final_session_id is None
                and agent.session is not None
                and agent.session.id_source == "stdout_json"
            ):
                try:
                    final_session_id = self._extract_stdout_session_id(record, agent)
                except TaskError as exc:
                    session_error = str(exc)

            final_return_code = return_code
            if record.cancel_requested.is_set():
                final_status = "cancelled"
                final_error = "task was cancelled"
            elif timed_out:
                final_status = "timed_out"
                final_error = f"task exceeded timeout_sec={record.timeout_sec}"
            elif capture_error is not None:
                final_status = "failed"
                final_error = f"could not capture agent output: {capture_error}"
            elif session_error is not None:
                final_status = "failed"
                final_error = f"could not capture resumable provider session: {session_error}"
            elif return_code == 0:
                final_status = "succeeded"
            else:
                final_status = "failed"
                final_error = f"agent process exited with code {return_code}"
        except Exception as exc:
            if process is not None:
                self._terminate_process(process)
            final_status = "cancelled" if record.cancel_requested.is_set() else "failed"
            final_error = f"could not execute agent process: {exc}"
        finally:
            cleanup_error: str | None = None
            if agent.prompt_mode == "file":
                try:
                    (task_dir / "prompt.txt").unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_error = f"could not remove temporary prompt file: {exc}"
            with self._lock:
                record.status = final_status
                record.error = final_error
                record.return_code = final_return_code
                record.session_id = final_session_id
                if cleanup_error is not None:
                    record.status = "failed"
                    self._append_record_error(record, cleanup_error)
                record.finished_at = utc_now()
                record.process = None
                self._persist_terminal_record(record)
                record.done.set()
                self._signal_change(record)
                try:
                    self._prune_history(preserve_task_id=record.task_id)
                except Exception:
                    pass

    # ==========================================
    # Function: Convert one record into a public, prompt-redacted result object.
    # Method: Reuse persisted metadata and remove internal process identity fields.
    # ==========================================
    def _public_record(self, record: TaskRecord) -> dict[str, Any]:
        value = self._metadata(record)
        value.pop("schema_version", None)
        value.pop("pid", None)
        value.pop("process_start_ticks", None)
        value["child_task_ids"] = [
            item.task_id
            for item in sorted(
                self._tasks.values(),
                key=lambda candidate: (candidate.created_at, candidate.task_id),
            )
            if item.parent_task_id == record.task_id
        ]
        return value

    # ==========================================
    # Function: Read one bounded binary log segment as replacement-safe UTF-8.
    # Method: Seek by byte offset, cap requested bytes, and return pagination metadata.
    # ==========================================
    def _read_log(
        self,
        path: Path,
        offset: int,
        max_bytes: int,
        stream_complete: bool,
    ) -> dict[str, Any]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise TaskError("log offsets must be non-negative integers")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 4 <= max_bytes <= 262144:
            raise TaskError("max_bytes must be an integer between 4 and 262144")
        size = path.stat().st_size if path.is_file() else 0
        bounded_offset = min(offset, size)
        if not path.is_file():
            data = b""
        else:
            with path.open("rb") as source:
                source.seek(bounded_offset)
                data = source.read(max_bytes)
        reaches_current_end = bounded_offset + len(data) >= size
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        decoded = decoder.decode(data, final=stream_complete and reaches_current_end)
        pending, _decoder_state = decoder.getstate()
        consumed = len(data) - len(pending)
        next_offset = bounded_offset + consumed
        incomplete_utf8_tail = bool(pending) and reaches_current_end and not stream_complete
        return {
            "text": decoded,
            "offset": bounded_offset,
            "next_offset": next_offset,
            "available_bytes": size,
            "has_more": next_offset < size and not incomplete_utf8_tail,
            "incomplete_utf8_tail": incomplete_utf8_tail,
        }

    # ==========================================
    # Function: Return one task plus independently paginated stdout and stderr.
    # Method: Validate ownership under lock, snapshot metadata, then read bounded log chunks.
    # ==========================================
    def get_task(
        self,
        task_id: str,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        max_bytes: int = 65536,
        include_output: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
            raise TaskError("task_id must be a valid string identifier")
        if not isinstance(include_output, bool):
            raise TaskError("include_output must be a boolean")
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskError(f"unknown task_id {task_id!r}")
            public = self._public_record(record)
            task_dir = self._task_dir(task_id)
            stream_complete = record.status in TERMINAL_STATUSES
            if include_output:
                public["stdout"] = self._read_log(
                    task_dir / "stdout.log", stdout_offset, max_bytes, stream_complete
                )
                public["stderr"] = self._read_log(
                    task_dir / "stderr.log", stderr_offset, max_bytes, stream_complete
                )
            return public

    # ==========================================
    # Function: Wait a bounded interval for task progress and return current output.
    # Method: Wake on readable bytes, status changes, stream completion, cancellation, or deadline.
    # ==========================================
    def wait_task(
        self,
        task_id: str,
        wait_sec: int = 30,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        max_bytes: int = 65536,
        include_output: bool = True,
        cancellation_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if isinstance(wait_sec, bool) or not isinstance(wait_sec, int) or not 0 <= wait_sec <= 50:
            raise TaskError("wait_sec must be an integer between 0 and 50")
        if not isinstance(include_output, bool):
            raise TaskError("include_output must be a boolean")
        if not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
            raise TaskError("task_id must be a valid string identifier")
        if cancellation_event is not None and cancellation_event.is_set():
            raise TaskError("wait request was cancelled")
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskError(f"unknown task_id {task_id!r}")
            event = record.changed
            baseline_sequence = record.change_sequence
        snapshot = self.get_task(task_id, stdout_offset, stderr_offset, max_bytes, include_output)
        baseline_status = snapshot["status"]
        if (
            snapshot["status"] in TERMINAL_STATUSES
            or (
                include_output
                and (
                    snapshot["stdout"]["next_offset"] > snapshot["stdout"]["offset"]
                    or snapshot["stderr"]["next_offset"] > snapshot["stderr"]["offset"]
                )
            )
            or wait_sec == 0
        ):
            return snapshot
        deadline = time.monotonic() + wait_sec
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                raise TaskError("wait request was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.get_task(task_id, stdout_offset, stderr_offset, max_bytes, include_output)
            with self._lock:
                changed = record.change_sequence != baseline_sequence
                status_changed = record.status != baseline_status
                if (include_output and changed) or (not include_output and status_changed):
                    return self.get_task(task_id, stdout_offset, stderr_offset, max_bytes, include_output)
                event.clear()
            interval = min(remaining, 0.1) if cancellation_event is not None else remaining
            event.wait(interval)

    # ==========================================
    # Function: List recent task metadata without embedding logs.
    # Method: Sort newest-first and cap the response to a caller-selected bounded count.
    # ==========================================
    def list_tasks(self, limit: int = 20, statuses: Iterable[str] | None = None) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise TaskError("limit must be an integer between 1 and 200")
        selected_statuses = set(statuses or [])
        unknown_statuses = selected_statuses - (TERMINAL_STATUSES | RECOVERABLE_STATUSES)
        if unknown_statuses:
            raise TaskError(f"unknown statuses: {sorted(unknown_statuses)}")
        with self._lock:
            records = sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)
            if selected_statuses:
                records = [record for record in records if record.status in selected_statuses]
            return {
                "tasks": [self._public_record(record) for record in records[:limit]],
                "returned": min(len(records), limit),
                "matching": len(records),
            }

    # ==========================================
    # Function: Request cancellation for one active task.
    # Method: Set an idempotent flag, mark cancelling, and signal the isolated process group.
    # ==========================================
    def cancel_task(self, task_id: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
            raise TaskError("task_id must be a valid string identifier")
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskError(f"unknown task_id {task_id!r}")
            if record.status in TERMINAL_STATUSES:
                return self._public_record(record)
            record.cancel_requested.set()
            record.status = "cancelling"
            self._signal_change(record)
            process = record.process
            self._save_record(record)
        if process is not None:
            self._terminate_process(process)
        return self.get_task(task_id)

    # ==========================================
    # Function: Remove oldest terminal task directories above the retention limit.
    # Method: Delete only validated, manager-owned directories with terminal metadata.
    # ==========================================
    def _prune_history(self, preserve_task_id: str | None = None) -> None:
        terminal = sorted(
            (record for record in self._tasks.values() if record.status in TERMINAL_STATUSES),
            key=lambda item: (item.created_at, item.task_id),
            reverse=True,
        )
        keep_ids: set[str] = set()
        if preserve_task_id is not None:
            keep_ids.add(preserve_task_id)
        for record in terminal:
            if len(keep_ids) >= self._config.max_retained_tasks:
                break
            keep_ids.add(record.task_id)
        for record in terminal:
            if record.task_id in keep_ids:
                continue
            task_dir = self._task_dir(record.task_id)
            metadata_path = task_dir / "metadata.json"
            try:
                persisted = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if persisted.get("task_id") != record.task_id or persisted.get("status") not in TERMINAL_STATUSES:
                continue
            shutil.rmtree(task_dir)
            self._tasks.pop(record.task_id, None)

    # ==========================================
    # Function: Cancel live work when the MCP server shuts down.
    # Method: Set cancellation on every active task and terminate each process group outside the lock.
    # ==========================================
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = [
                record
                for record in self._tasks.values()
                if record.status in RECOVERABLE_STATUSES
            ]
            for record in active:
                record.cancel_requested.set()
                record.status = "cancelling"
                self._signal_change(record)
                self._save_record(record)
            processes = [record.process for record in active if record.process is not None]
        for process in processes:
            self._terminate_process(process)
        for record in active:
            if record.worker is not None:
                record.worker.join(timeout=5)
