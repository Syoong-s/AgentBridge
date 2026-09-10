#!/usr/bin/env python3
"""End-to-end task lifecycle tests for the Agent Bridge manager."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SCRIPTS = REPOSITORY_ROOT / "plugins" / "agent-bridge" / "scripts"
FAKE_AGENT = REPOSITORY_ROOT / "tests" / "fixtures" / "fake_agent.py"
sys.path.insert(0, str(RUNTIME_SCRIPTS))

from agent_bridge.config import load_config  # noqa: E402
from agent_bridge.tasks import TaskError, TaskManager, process_start_ticks  # noqa: E402


# ==========================================
# Class: External process lifecycle integration tests.
# Method: Run a deterministic fake CLI through real subprocesses, pipes, state files, and signals.
# ==========================================
class TaskManagerTests(unittest.TestCase):
    # ==========================================
    # Function: Allocate an isolated config, state root, and permitted workspace.
    # Method: Build every test from a private temporary directory and track managers for cleanup.
    # ==========================================
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "state"
        self.managers: list[TaskManager] = []

    # ==========================================
    # Function: Stop live tasks and remove the isolated filesystem.
    # Method: Close every constructed manager before releasing TemporaryDirectory.
    # ==========================================
    def tearDown(self) -> None:
        for manager in reversed(self.managers):
            manager.close()
        self.temporary.cleanup()

    # ==========================================
    # Function: Produce the standard fake-agent alias set.
    # Method: Encode each prompt mode and terminal behavior as direct Python argv.
    # ==========================================
    def agent_definitions(self) -> dict[str, dict[str, object]]:
        python = sys.executable
        fake = str(FAKE_AGENT)
        return {
            "argument": {
                "command": [python, fake, "--transport", "argument", "{prompt}"],
                "prompt_mode": "argument",
                "allow_extra_args": True,
            },
            "stdin": {
                "command": [python, fake, "--transport", "stdin"],
                "prompt_mode": "stdin",
            },
            "file": {
                "command": [python, fake, "--transport", "file", "{prompt_file}"],
                "prompt_mode": "file",
            },
            "failing": {
                "command": [
                    python,
                    fake,
                    "--transport",
                    "argument",
                    "--fail",
                    "7",
                    "--stderr",
                    "expected-error",
                    "{prompt}",
                ],
                "prompt_mode": "argument",
            },
            "slow": {
                "command": [python, fake, "--sleep", "10", "{prompt}"],
                "prompt_mode": "argument",
                "timeout_sec": 20,
            },
            "timeout": {
                "command": [python, fake, "--sleep", "2", "{prompt}"],
                "prompt_mode": "argument",
                "timeout_sec": 1,
            },
            "noisy": {
                "command": [python, fake, "--repeat", "5000", "{prompt}"],
                "prompt_mode": "argument",
                "max_output_bytes": 1024,
            },
            "environment": {
                "command": [python, fake, "--environment", "BRIDGE_VALUE", "{prompt}"],
                "prompt_mode": "argument",
                "environment": {"BRIDGE_VALUE": "task-{task_id}"},
            },
            "tree": {
                "command": [
                    python,
                    fake,
                    "--spawn-child",
                    "--child-pid-file",
                    "{cwd}/child.pid",
                    "--sleep",
                    "10",
                    "{prompt}",
                ],
                "prompt_mode": "argument",
                "timeout_sec": 20,
            },
            "background": {
                "command": [
                    python,
                    fake,
                    "--spawn-child",
                    "--child-pid-file",
                    "{cwd}/background-child.pid",
                    "{prompt}",
                ],
                "prompt_mode": "argument",
            },
            "streaming": {
                "command": [
                    python,
                    fake,
                    "--early-output",
                    "ready|",
                    "--sleep",
                    "10",
                    "{prompt}",
                ],
                "prompt_mode": "argument",
                "timeout_sec": 20,
            },
        }

    # ==========================================
    # Function: Build a manager from caller-selected defaults and aliases.
    # Method: Write a real JSON parameter file, load it, and register the manager for cleanup.
    # ==========================================
    def make_manager(
        self,
        *,
        agents: dict[str, dict[str, object]] | None = None,
        max_concurrent_tasks: int = 4,
        max_retained_tasks: int = 100,
        state: Path | None = None,
    ) -> TaskManager:
        state_path = state or self.state
        config_path = self.root / f"config-{len(self.managers)}.json"
        document = {
            "version": 1,
            "defaults": {
                "max_concurrent_tasks": max_concurrent_tasks,
                "max_retained_tasks": max_retained_tasks,
                "default_timeout_sec": 20,
                "max_timeout_sec": 30,
                "allowed_work_roots": [str(self.work)],
            },
            "agents": agents or self.agent_definitions(),
        }
        config_path.write_text(json.dumps(document), encoding="utf-8")
        manager = TaskManager(load_config(config_path, state_dir=state_path))
        self.managers.append(manager)
        return manager

    # ==========================================
    # Function: Wait until one task enters a terminal state.
    # Method: Use bounded manager waits and fail with the last record after a test deadline.
    # ==========================================
    def wait_terminal(self, manager: TaskManager, task_id: str, deadline_sec: float = 8.0) -> dict[str, object]:
        deadline = time.monotonic() + deadline_sec
        result: dict[str, object] = {}
        while time.monotonic() < deadline:
            result = manager.wait_task(task_id, wait_sec=1)
            if result["status"] in {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}:
                return result
        self.fail(f"task did not finish before test deadline: {result}")

    # ==========================================
    # Function: Verify argument, stdin, and file prompt transports.
    # Method: Launch each real subprocess and compare the collected stdout to Unicode input.
    # ==========================================
    def test_all_prompt_modes_return_output(self) -> None:
        manager = self.make_manager()
        for alias in ("argument", "stdin", "file"):
            with self.subTest(alias=alias):
                started = manager.start_task(alias, "hello-桥", str(self.work))
                result = self.wait_terminal(manager, started["task_id"])
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["stdout"]["text"], "hello-桥")
                self.assertFalse((self.state / started["task_id"] / "prompt.txt").exists())

    # ==========================================
    # Function: Preserve placeholder-looking text inside the user prompt literally.
    # Method: Ensure one-pass template expansion never rewrites inserted prompt content.
    # ==========================================
    def test_prompt_placeholders_are_not_recursively_expanded(self) -> None:
        manager = self.make_manager()
        prompt = "literal {cwd} and {task_id} and {prompt_file}"
        started = manager.start_task("argument", prompt, str(self.work))
        result = self.wait_terminal(manager, started["task_id"])
        self.assertEqual(result["stdout"]["text"], prompt)

    # ==========================================
    # Function: Avoid leaving task state or prompt files when an executable is unavailable.
    # Method: Validate the command before creating the private file-mode task directory.
    # ==========================================
    def test_missing_executable_leaves_no_task_directory(self) -> None:
        agents = {
            "missing": {
                "command": ["/definitely/not/an/agent", "{prompt_file}"],
                "prompt_mode": "file",
            }
        }
        manager = self.make_manager(agents=agents)
        with self.assertRaisesRegex(TaskError, "not runnable"):
            manager.start_task("missing", "sensitive prompt", str(self.work))
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual(manager.list_tasks()["matching"], 0)

    # ==========================================
    # Function: Preserve nonzero exit evidence and stderr.
    # Method: Run a deterministic exit-code seven task and inspect terminal metadata/logs.
    # ==========================================
    def test_failure_is_reported_with_stderr(self) -> None:
        manager = self.make_manager()
        started = manager.start_task("failing", "failed-output", str(self.work))
        result = self.wait_terminal(manager, started["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["return_code"], 7)
        self.assertEqual(result["stdout"]["text"], "failed-output")
        self.assertEqual(result["stderr"]["text"], "expected-error")

    # ==========================================
    # Function: Enforce timeout and explicit cancellation terminal states.
    # Method: Exercise both manager deadline termination and a caller-issued cancellation.
    # ==========================================
    def test_timeout_and_cancellation(self) -> None:
        manager = self.make_manager()
        timed = manager.start_task("timeout", "late", str(self.work))
        timed_result = self.wait_terminal(manager, timed["task_id"])
        self.assertEqual(timed_result["status"], "timed_out")

        cancelled = manager.start_task("slow", "cancel-me", str(self.work))
        cancel_result = manager.cancel_task(cancelled["task_id"])
        self.assertIn(cancel_result["status"], {"cancelling", "cancelled"})
        final_result = self.wait_terminal(manager, cancelled["task_id"])
        self.assertEqual(final_result["status"], "cancelled")

    # ==========================================
    # Function: Bound persisted output while draining the full child stream.
    # Method: Emit 5000 bytes into a 1024-byte ceiling and check truncation metadata.
    # ==========================================
    def test_output_is_truncated_at_configured_limit(self) -> None:
        manager = self.make_manager()
        started = manager.start_task("noisy", "x", str(self.work))
        result = self.wait_terminal(manager, started["task_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["stdout_truncated"])
        self.assertEqual(result["stdout"]["available_bytes"], 1024)
        self.assertEqual(len(result["stdout"]["text"]), 1024)

    # ==========================================
    # Function: Reassemble a bounded log through byte-offset pagination.
    # Method: Follow every next_offset from a 1024-byte stored stream using 73-byte pages.
    # ==========================================
    def test_output_pagination_roundtrip(self) -> None:
        manager = self.make_manager()
        started = manager.start_task("noisy", "x", str(self.work))
        self.wait_terminal(manager, started["task_id"])
        offset = 0
        pages: list[str] = []
        while True:
            result = manager.get_task(
                started["task_id"],
                stdout_offset=offset,
                max_bytes=73,
            )
            pages.append(result["stdout"]["text"])
            offset = result["stdout"]["next_offset"]
            if not result["stdout"]["has_more"]:
                break
        self.assertEqual("".join(pages), "x" * 1024)
        self.assertEqual(offset, 1024)

    # ==========================================
    # Function: Preserve multibyte Unicode characters across byte-oriented log pages.
    # Method: Reassemble Chinese and emoji output from the minimum four-byte page size.
    # ==========================================
    def test_unicode_output_pagination_preserves_characters(self) -> None:
        manager = self.make_manager()
        expected = "桥🙂a桥🙂b"
        started = manager.start_task("argument", expected, str(self.work))
        self.wait_terminal(manager, started["task_id"])
        offset = 0
        pages: list[str] = []
        while True:
            result = manager.get_task(
                started["task_id"],
                stdout_offset=offset,
                max_bytes=4,
            )
            pages.append(result["stdout"]["text"])
            next_offset = result["stdout"]["next_offset"]
            self.assertGreater(next_offset, offset)
            offset = next_offset
            if not result["stdout"]["has_more"]:
                break
        self.assertEqual("".join(pages), expected)

    # ==========================================
    # Function: Pause pagination on an incomplete live UTF-8 tail without spinning.
    # Method: Expose one leading byte, then append the remainder and resume from the unchanged cursor.
    # ==========================================
    def test_running_incomplete_utf8_tail_waits_for_more_bytes(self) -> None:
        manager = self.make_manager()
        path = self.root / "partial.log"
        encoded = "桥".encode("utf-8")
        path.write_bytes(encoded[:1])
        partial = manager._read_log(path, 0, 4, stream_complete=False)
        self.assertEqual(partial["text"], "")
        self.assertEqual(partial["next_offset"], 0)
        self.assertFalse(partial["has_more"])
        self.assertTrue(partial["incomplete_utf8_tail"])

        path.write_bytes(encoded)
        complete = manager._read_log(path, partial["next_offset"], 4, stream_complete=False)
        self.assertEqual(complete["text"], "桥")
        self.assertEqual(complete["next_offset"], len(encoded))
        self.assertFalse(complete["incomplete_utf8_tail"])

    # ==========================================
    # Function: Match child executable lookup for a relative PATH entry.
    # Method: Put a Python symlink under task-cwd/bin and launch with inheritance disabled.
    # ==========================================
    def test_relative_path_is_resolved_against_task_cwd(self) -> None:
        executable_dir = self.work / "bin"
        executable_dir.mkdir()
        executable = executable_dir / "relative-agent"
        executable.symlink_to(sys.executable)
        agents = {
            "relative-path": {
                "command": ["relative-agent", str(FAKE_AGENT), "{prompt}"],
                "prompt_mode": "argument",
                "inherit_env": False,
                "environment": {"PATH": "bin"},
            }
        }
        manager = self.make_manager(agents=agents)
        started = manager.start_task("relative-path", "path-result", str(self.work))
        result = self.wait_terminal(manager, started["task_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["stdout"]["text"], "path-result")

    # ==========================================
    # Function: Enforce allowed roots and active concurrency limits.
    # Method: Reject an outside cwd and a second task while one slot is occupied.
    # ==========================================
    def test_work_root_and_concurrency_policy(self) -> None:
        manager = self.make_manager(max_concurrent_tasks=1)
        with self.assertRaisesRegex(TaskError, "outside configured"):
            manager.start_task("argument", "x", str(self.root))
        first = manager.start_task("slow", "one", str(self.work))
        with self.assertRaisesRegex(TaskError, "maximum concurrent"):
            manager.start_task("argument", "two", str(self.work))
        manager.cancel_task(first["task_id"])
        self.wait_terminal(manager, first["task_id"])

    # ==========================================
    # Function: Cancel descendants that remain in the external agent's process group.
    # Method: Capture a spawned child PID, cancel the task, and require the child to stop running.
    # ==========================================
    def test_cancellation_terminates_process_group_descendants(self) -> None:
        manager = self.make_manager()
        pid_file = self.work / "child.pid"
        started = manager.start_task("tree", "tree-task", str(self.work))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pid_file.is_file():
            time.sleep(0.02)
        self.assertTrue(pid_file.is_file())
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        manager.cancel_task(started["task_id"])
        self.assertEqual(self.wait_terminal(manager, started["task_id"])["status"], "cancelled")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stat_path = Path(f"/proc/{child_pid}/stat")
            if not stat_path.is_file() or stat_path.read_text(encoding="utf-8").split()[2] == "Z":
                break
            time.sleep(0.02)
        else:
            self.fail(f"descendant process {child_pid} survived process-group cancellation")

    # ==========================================
    # Function: Expose flushed output while the external agent is still running.
    # Method: Read an early marker before cancelling a deliberately sleeping fake agent.
    # ==========================================
    def test_running_task_output_is_observable(self) -> None:
        manager = self.make_manager()
        started = manager.start_task("streaming", "later", str(self.work))
        deadline = time.monotonic() + 5
        result: dict[str, object] = {}
        while time.monotonic() < deadline:
            result = manager.get_task(started["task_id"])
            if result["stdout"]["text"] == "ready|":
                break
            time.sleep(0.02)
        else:
            self.fail(f"flushed running output was not observable: {result}")
        self.assertEqual(result["status"], "running")
        manager.cancel_task(started["task_id"])
        self.assertEqual(self.wait_terminal(manager, started["task_id"])["status"], "cancelled")

    # ==========================================
    # Function: Clean up same-group background descendants when the CLI leader exits.
    # Method: Let the fake parent return zero, then require terminal success and a dead child.
    # ==========================================
    def test_successful_parent_exit_does_not_leave_background_descendants(self) -> None:
        manager = self.make_manager()
        pid_file = self.work / "background-child.pid"
        started = manager.start_task("background", "parent-result", str(self.work))
        result = self.wait_terminal(manager, started["task_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["stdout"]["text"], "parent-result")
        self.assertTrue(pid_file.is_file())
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        stat_path = Path(f"/proc/{child_pid}/stat")
        if stat_path.is_file():
            self.assertEqual(stat_path.read_text(encoding="utf-8").split()[2], "Z")

    # ==========================================
    # Function: Keep prompts and runtime extra arguments out of persisted command metadata.
    # Method: Launch with sentinel secrets and inspect both API and raw metadata JSON.
    # ==========================================
    def test_persisted_command_is_redacted(self) -> None:
        manager = self.make_manager()
        started = manager.start_task(
            "argument",
            "PROMPT-SECRET",
            str(self.work),
            extra_args=["EXTRA-SECRET"],
        )
        result = self.wait_terminal(manager, started["task_id"])
        serialized_command = json.dumps(result["command"])
        metadata = (self.state / started["task_id"] / "metadata.json").read_text(encoding="utf-8")
        self.assertNotIn("PROMPT-SECRET", serialized_command)
        self.assertNotIn("EXTRA-SECRET", serialized_command)
        self.assertNotIn("PROMPT-SECRET", metadata)
        self.assertNotIn("EXTRA-SECRET", metadata)

    # ==========================================
    # Function: Substitute task metadata into configured environment values.
    # Method: Check that a task-specific ID reaches the child without exposing the environment map.
    # ==========================================
    def test_environment_placeholder(self) -> None:
        manager = self.make_manager()
        started = manager.start_task("environment", "payload", str(self.work))
        result = self.wait_terminal(manager, started["task_id"])
        self.assertEqual(
            result["stdout"]["text"],
            f"task-{started['task_id']}|payload",
        )

    # ==========================================
    # Function: Mark stale running metadata interrupted on server restart.
    # Method: Seed a schema-valid task without a live PID and construct a fresh manager.
    # ==========================================
    def test_restart_recovery_marks_stale_task_interrupted(self) -> None:
        stale_id = "20260101T000000Z-deadbeef0000"
        task_dir = self.state / stale_id
        task_dir.mkdir(parents=True)
        metadata = {
            "schema_version": 1,
            "task_id": stale_id,
            "agent": "argument",
            "status": "running",
            "cwd": str(self.work),
            "command": [sys.executable, str(FAKE_AGENT), "<prompt>"],
            "created_at": "2026-01-01T00:00:00Z",
            "started_at": "2026-01-01T00:00:01Z",
            "finished_at": None,
            "timeout_sec": 10,
            "max_output_bytes": 1024,
            "return_code": None,
            "error": None,
            "pid": None,
            "process_start_ticks": None,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
        (task_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        manager = self.make_manager(state=self.state)
        result = manager.get_task(stale_id)
        self.assertEqual(result["status"], "interrupted")
        self.assertIn("restarted", result["error"])

    # ==========================================
    # Function: Escalate restart cleanup when a stale process ignores SIGTERM.
    # Method: Seed matching PID/start-tick metadata for a real signal-resistant process group.
    # ==========================================
    def test_restart_recovery_kills_sigterm_resistant_process(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)",
            ],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "ready")
            ticks = process_start_ticks(process.pid)
            self.assertIsNotNone(ticks)
            stale_id = "20260101T000001Z-feedface0000"
            task_dir = self.state / stale_id
            task_dir.mkdir(parents=True)
            metadata = {
                "schema_version": 1,
                "task_id": stale_id,
                "agent": "argument",
                "status": "running",
                "cwd": str(self.work),
                "command": ["<recovered>"],
                "created_at": "2026-01-01T00:00:01Z",
                "started_at": "2026-01-01T00:00:02Z",
                "finished_at": None,
                "timeout_sec": 30,
                "max_output_bytes": 1024,
                "return_code": None,
                "error": None,
                "pid": process.pid,
                "process_start_ticks": ticks,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "stdout_truncated": False,
                "stderr_truncated": False,
            }
            (task_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            manager = self.make_manager(state=self.state)
            result = manager.get_task(stale_id)
            self.assertEqual(result["status"], "interrupted")
            process.wait(timeout=5)
            self.assertEqual(process.returncode, -signal.SIGKILL)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()

    # ==========================================
    # Function: Keep no more than the configured number of terminal task directories.
    # Method: Complete three tasks under a retention limit of two and inspect state/API history.
    # ==========================================
    def test_terminal_history_retention(self) -> None:
        manager = self.make_manager(max_retained_tasks=2)
        task_ids: list[str] = []
        for prompt in ("one", "two", "three"):
            started = manager.start_task("argument", prompt, str(self.work))
            task_ids.append(started["task_id"])
            self.wait_terminal(manager, started["task_id"])
        listed = manager.list_tasks(limit=10)
        self.assertEqual(listed["matching"], 2)
        self.assertFalse((self.state / task_ids[0]).exists())
        self.assertTrue((self.state / task_ids[-1]).exists())


if __name__ == "__main__":
    unittest.main()
