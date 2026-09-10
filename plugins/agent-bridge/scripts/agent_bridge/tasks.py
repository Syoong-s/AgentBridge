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
PLACEHOLDER_PATTERN = re.compile(r"\{(?:prompt|prompt_file|cwd|task_id)\}")


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
    started_at: str | None = None
    finished_at: str | None = None
    return_code: int | None = None
    error: str | None = None
    pid: int | None = None
    process_start_ticks: int | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    worker: threading.Thread | None = field(default=None, repr=False)
    cancel_requested: threading.Event = field(default_factory=threading.Event, repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)


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
            "agent": record.agent,
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
        return TaskRecord(
            task_id=value["task_id"],
            agent=value["agent"],
            status=value["status"],
            cwd=value["cwd"],
            command=command,
            created_at=value["created_at"],
            timeout_sec=timeout_sec,
            max_output_bytes=max_output_bytes,
            started_at=value.get("started_at") if isinstance(value.get("started_at"), str) else None,
            finished_at=value.get("finished_at") if isinstance(value.get("finished_at"), str) else None,
            return_code=value.get("return_code") if isinstance(value.get("return_code"), int) else None,
            error=value.get("error") if isinstance(value.get("error"), str) else None,
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
    # Function: Start one configured external-agent task asynchronously.
    # Method: Validate policy, create private state, snapshot redacted argv, and launch a worker thread.
    # ==========================================
    def start_task(
        self,
        alias: str,
        prompt: str,
        cwd: str,
        extra_args: Any = None,
        timeout_sec: int | None = None,
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
            command = [substitute(argument, replacements) for argument in agent.command]
            command.extend(parsed_extra_args)
            redacted_command = [
                substitute(argument, redacted_replacements) for argument in agent.command
            ]
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
    # Function: Drain one child stream while enforcing its persisted byte ceiling.
    # Method: Continue reading after truncation so the child cannot block on a full pipe.
    # ==========================================
    def _drain_stream(
        self,
        stream: BinaryIO,
        path: Path,
        limit: int,
        record: TaskRecord,
        stream_name: str,
    ) -> None:
        written = 0
        truncated = False
        with path.open("wb") as destination:
            path.chmod(0o600)
            while True:
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                remaining = max(0, limit - written)
                if remaining:
                    accepted = chunk[:remaining]
                    destination.write(accepted)
                    destination.flush()
                    written += len(accepted)
                if len(chunk) > remaining:
                    truncated = True
        stream.close()
        with self._lock:
            if stream_name == "stdout":
                record.stdout_truncated = truncated
            else:
                record.stderr_truncated = truncated

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
    # Function: Execute one task and transition it to exactly one terminal state.
    # Method: Capture bounded streams concurrently, honor cancellation/timeout, and persist final metadata.
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
        try:
            with self._lock:
                if record.cancel_requested.is_set():
                    record.status = "cancelled"
                    record.error = "task was cancelled before process launch"
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
                self._save_record(record)

            assert process.stdout is not None
            assert process.stderr is not None
            stdout_worker = threading.Thread(
                target=self._drain_stream,
                args=(process.stdout, stdout_path, record.max_output_bytes, record, "stdout"),
                daemon=True,
            )
            stderr_worker = threading.Thread(
                target=self._drain_stream,
                args=(process.stderr, stderr_path, record.max_output_bytes, record, "stderr"),
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
            stdout_worker.join(timeout=5)
            stderr_worker.join(timeout=5)

            with self._lock:
                record.return_code = return_code
                if record.cancel_requested.is_set():
                    record.status = "cancelled"
                    record.error = "task was cancelled"
                elif timed_out:
                    record.status = "timed_out"
                    record.error = f"task exceeded timeout_sec={record.timeout_sec}"
                elif return_code == 0:
                    record.status = "succeeded"
                else:
                    record.status = "failed"
                    record.error = f"agent process exited with code {return_code}"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if process is not None:
                self._terminate_process(process)
            with self._lock:
                record.status = "cancelled" if record.cancel_requested.is_set() else "failed"
                record.error = f"could not execute agent process: {exc}"
        finally:
            if agent.prompt_mode == "file":
                (task_dir / "prompt.txt").unlink(missing_ok=True)
            with self._lock:
                record.finished_at = utc_now()
                record.process = None
                record.done.set()
                self._save_record(record)
                self._prune_history(preserve_task_id=record.task_id)

    # ==========================================
    # Function: Convert one record into a public, prompt-redacted result object.
    # Method: Reuse persisted metadata and remove internal process identity fields.
    # ==========================================
    def _public_record(self, record: TaskRecord) -> dict[str, Any]:
        value = self._metadata(record)
        value.pop("schema_version", None)
        value.pop("pid", None)
        value.pop("process_start_ticks", None)
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
    ) -> dict[str, Any]:
        if not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
            raise TaskError("task_id must be a valid string identifier")
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskError(f"unknown task_id {task_id!r}")
            public = self._public_record(record)
            task_dir = self._task_dir(task_id)
            stream_complete = record.status in TERMINAL_STATUSES
            public["stdout"] = self._read_log(
                task_dir / "stdout.log", stdout_offset, max_bytes, stream_complete
            )
            public["stderr"] = self._read_log(
                task_dir / "stderr.log", stderr_offset, max_bytes, stream_complete
            )
            return public

    # ==========================================
    # Function: Wait a bounded interval for one task and return current output.
    # Method: Block on the record event for at most 50 seconds, then delegate to get_task.
    # ==========================================
    def wait_task(
        self,
        task_id: str,
        wait_sec: int = 30,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        max_bytes: int = 65536,
    ) -> dict[str, Any]:
        if isinstance(wait_sec, bool) or not isinstance(wait_sec, int) or not 0 <= wait_sec <= 50:
            raise TaskError("wait_sec must be an integer between 0 and 50")
        if not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
            raise TaskError("task_id must be a valid string identifier")
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskError(f"unknown task_id {task_id!r}")
            event = record.done
        event.wait(wait_sec)
        return self.get_task(task_id, stdout_offset, stderr_offset, max_bytes)

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
                self._save_record(record)
            processes = [record.process for record in active if record.process is not None]
        for process in processes:
            self._terminate_process(process)
        for record in active:
            if record.worker is not None:
                record.worker.join(timeout=5)
