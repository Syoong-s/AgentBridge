#!/usr/bin/env python3
"""Raw stdio MCP roundtrip test for the packaged Agent Bridge launcher."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins" / "agent-bridge"
START_SCRIPT = PLUGIN_ROOT / "scripts" / "start.sh"
FAKE_AGENT = REPOSITORY_ROOT / "tests" / "fixtures" / "fake_agent.py"


# ==========================================
# Class: Packaged MCP launcher protocol integration test.
# Method: Exchange real newline-delimited JSON-RPC over subprocess stdin/stdout.
# ==========================================
class MCPStdioTests(unittest.TestCase):
    # ==========================================
    # Function: Write one JSON-RPC value without waiting for its response.
    # Method: Serialize one line and flush it to the live MCP server stdin.
    # ==========================================
    def write(self, process: subprocess.Popen[str], request: object) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()

    # ==========================================
    # Function: Send one request and decode its single response line.
    # Method: Flush JSON to server stdin and require valid object output from stdout.
    # ==========================================
    def send(self, process: subprocess.Popen[str], request: object) -> dict[str, object]:
        assert process.stdout is not None
        self.write(process, request)
        line = process.stdout.readline()
        self.assertTrue(line, "MCP server closed stdout before responding")
        response = json.loads(line)
        self.assertIsInstance(response, dict)
        return response

    # ==========================================
    # Function: Poll one external task to terminal while consuming incremental output.
    # Method: Advance both stream offsets across progress-aware wait_task responses.
    # ==========================================
    def wait_terminal(
        self,
        process: subprocess.Popen[str],
        task_id: str,
        request_id: int,
    ) -> dict[str, object]:
        stdout_offset = 0
        stderr_offset = 0
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        deadline = time.monotonic() + 8
        payload: dict[str, object] = {}
        while time.monotonic() < deadline:
            payload = self.tool_payload(
                self.send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {
                            "name": "wait_task",
                            "arguments": {
                                "task_id": task_id,
                                "wait_sec": 1,
                                "stdout_offset": stdout_offset,
                                "stderr_offset": stderr_offset,
                            },
                        },
                    },
                )
            )
            request_id += 1
            stdout = payload["stdout"]
            stderr = payload["stderr"]
            self.assertIsInstance(stdout, dict)
            self.assertIsInstance(stderr, dict)
            stdout_chunks.append(stdout["text"])
            stderr_chunks.append(stderr["text"])
            stdout_offset = stdout["next_offset"]
            stderr_offset = stderr["next_offset"]
            if payload["status"] in {
                "succeeded",
                "failed",
                "timed_out",
                "cancelled",
                "interrupted",
            }:
                stdout["text"] = "".join(stdout_chunks)
                stderr["text"] = "".join(stderr_chunks)
                return payload
        self.fail(f"task did not finish before stdio test deadline: {payload}")

    # ==========================================
    # Function: Parse a JSON text content block from a successful tools/call response.
    # Method: Assert the MCP envelope and decode the first text item.
    # ==========================================
    def tool_payload(self, response: dict[str, object]) -> dict[str, object]:
        result = response["result"]
        self.assertIsInstance(result, dict)
        self.assertFalse(result["isError"])
        return json.loads(result["content"][0]["text"])

    # ==========================================
    # Function: Initialize, discover, launch, wait, and collect through the packaged server.
    # Method: Use an isolated fake-agent config and real start.sh entry point.
    # ==========================================
    def test_complete_stdio_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            work = root / "work"
            work.mkdir()
            config_path = root / "config.json"
            document = {
                "version": 1,
                "defaults": {"allowed_work_roots": [str(work)]},
                "agents": {
                    "fake": {
                        "command": [sys.executable, str(FAKE_AGENT), "{prompt}"],
                        "prompt_mode": "argument",
                        "model": {"arguments": ["--model", "{model}"]},
                        "reasoning_effort": {
                            "arguments": ["--effort", "{reasoning_effort}"],
                            "allowed_values": ["low", "high"],
                        },
                        "session": {
                            "id_source": "generated_uuid",
                            "start_arguments": ["--session-id", "{session_id}"],
                            "resume_arguments": ["--resume", "{session_id}"],
                        },
                    }
                },
            }
            config_path.write_text(json.dumps(document), encoding="utf-8")
            environment = dict(os.environ)
            environment["AGENT_BRIDGE_CONFIG"] = str(config_path)
            environment["AGENT_BRIDGE_STATE_DIR"] = str(root / "state")
            process = subprocess.Popen(
                ["bash", str(START_SCRIPT)],
                cwd=PLUGIN_ROOT,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                preinitialize = self.send(
                    process,
                    {"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
                )
                self.assertEqual(preinitialize["error"]["code"], -32002)
                batch = self.send(
                    process,
                    [{"jsonrpc": "2.0", "id": 99, "method": "initialize", "params": {}}],
                )
                self.assertEqual(batch["error"]["code"], -32600)
                initialized = self.send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26"},
                    },
                )
                self.assertEqual(initialized["result"]["serverInfo"]["name"], "agent-bridge")
                self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
                tools = self.send(
                    process,
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                names = {tool["name"] for tool in tools["result"]["tools"]}
                self.assertEqual(
                    names,
                    {
                        "list_agents",
                        "run_agent",
                        "task_status",
                        "list_tasks",
                        "cancel_task",
                    },
                )
                compact_agents = self.tool_payload(
                    self.send(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": 20,
                            "method": "tools/call",
                            "params": {"name": "list_agents", "arguments": {}},
                        },
                    )
                )
                self.assertEqual(
                    set(compact_agents["agents"][0]),
                    {"alias", "available", "enabled", "followup", "efforts"},
                )
                self.assertNotIn("config_path", compact_agents)
                invalid = self.send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 21,
                        "method": "tools/call",
                        "params": {
                            "name": "start_child_agent",
                            "arguments": {
                                "agent": "missing",
                                "prompt": "x",
                                "cwd": str(work),
                            },
                        },
                    },
                )
                self.assertTrue(invalid["result"]["isError"])
                malformed = self.send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 26,
                        "method": "tools/call",
                        "params": {
                            "name": "start_task",
                            "arguments": {
                                "agent": [],
                                "prompt": "x",
                                "cwd": str(work),
                                "unexpected": True,
                            },
                        },
                    },
                )
                self.assertTrue(malformed["result"]["isError"])
                document["agents"]["second"] = {
                    "command": [sys.executable, str(FAKE_AGENT), "{prompt}"],
                    "prompt_mode": "argument",
                }
                config_path.write_text(json.dumps(document), encoding="utf-8")
                reloaded = self.tool_payload(
                    self.send(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": 22,
                            "method": "tools/call",
                            "params": {
                                "name": "list_agents",
                                "arguments": {"refresh": True},
                            },
                        },
                    )
                )
                self.assertEqual(
                    {agent["alias"] for agent in reloaded["agents"]},
                    {"fake", "second"},
                )
                config_path.write_text("{not-json", encoding="utf-8")
                bad_reload = self.send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 24,
                        "method": "tools/call",
                        "params": {"name": "reload_config", "arguments": {}},
                    },
                )
                self.assertTrue(bad_reload["result"]["isError"])
                still_loaded = self.tool_payload(
                    self.send(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": 25,
                            "method": "tools/call",
                            "params": {
                                "name": "list_agents",
                                "arguments": {"detail": True},
                            },
                        },
                    )
                )
                self.assertEqual(
                    {agent["alias"] for agent in still_loaded["agents"]},
                    {"fake", "second"},
                )
                self.assertFalse(still_loaded["native_codex_subagents"])
                fake_capabilities = next(
                    agent for agent in still_loaded["agents"] if agent["alias"] == "fake"
                )
                self.assertEqual(fake_capabilities["task_kind"], "external_child_agent")
                self.assertTrue(fake_capabilities["model"]["supported"])
                self.assertEqual(
                    fake_capabilities["reasoning_effort"]["allowed_values"],
                    ["low", "high"],
                )
                self.assertTrue(fake_capabilities["supports_followup"])
                unknown_method = self.send(
                    process,
                    {"jsonrpc": "2.0", "id": 23, "method": "unknown/method"},
                )
                self.assertEqual(unknown_method["error"]["code"], -32601)
                started = self.tool_payload(
                    self.send(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {
                                "name": "run_agent",
                                "arguments": {
                                    "agent": "fake",
                                    "prompt": "roundtrip-result",
                                    "cwd": str(work),
                                    "model": "test-model",
                                    "reasoning_effort": "high",
                                },
                            },
                        },
                    )
                )
                completed = self.wait_terminal(process, started["task_id"], 40)
                self.assertEqual(completed["status"], "succeeded")
                self.assertEqual(completed["stdout"]["text"], "roundtrip-result")
                self.assertEqual(completed["task_kind"], "external_child_agent")
                self.assertEqual(completed["model"], "test-model")
                self.assertEqual(completed["reasoning_effort"], "high")
                followup = self.tool_payload(
                    self.send(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": 5,
                            "method": "tools/call",
                            "params": {
                                "name": "run_agent",
                                "arguments": {
                                    "resume_task_id": completed["task_id"],
                                    "prompt": "followup-result",
                                },
                            },
                        },
                    )
                )
                followed = self.wait_terminal(process, followup["task_id"], 60)
                self.assertEqual(followed["status"], "succeeded")
                self.assertEqual(followed["stdout"]["text"], "followup-result")
                self.assertEqual(followed["parent_task_id"], completed["task_id"])
                self.assertEqual(followed["root_task_id"], completed["task_id"])
                self.assertEqual(followed["session_id"], completed["session_id"])

                compact_status = self.tool_payload(
                    self.send(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": 6,
                            "method": "tools/call",
                            "params": {
                                "name": "task_status",
                                "arguments": {
                                    "task_id": completed["task_id"],
                                    "wait_sec": 0,
                                },
                            },
                        },
                    )
                )
                self.assertEqual(compact_status["status"], "succeeded")
                self.assertNotIn("stdout", compact_status)
                self.assertNotIn("stderr", compact_status)
                bounded_status = self.tool_payload(
                    self.send(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": 7,
                            "method": "tools/call",
                            "params": {
                                "name": "task_status",
                                "arguments": {
                                    "task_id": completed["task_id"],
                                    "wait_sec": 0,
                                    "include_output": True,
                                    "max_bytes": 64,
                                },
                            },
                        },
                    )
                )
                self.assertEqual(bounded_status["stdout"]["text"], "roundtrip-result")
                oversized_status = self.send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 8,
                        "method": "tools/call",
                        "params": {
                            "name": "task_status",
                            "arguments": {
                                "task_id": completed["task_id"],
                                "include_output": True,
                                "max_bytes": 16385,
                            },
                        },
                    },
                )
                self.assertTrue(oversized_status["result"]["isError"])
                compact_tasks = self.tool_payload(
                    self.send(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": 9,
                            "method": "tools/call",
                            "params": {
                                "name": "list_tasks",
                                "arguments": {},
                            },
                        },
                    )
                )
                self.assertTrue(compact_tasks["tasks"])
                self.assertNotIn("invocation", compact_tasks["tasks"][0])
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                process.wait(timeout=10)
                if process.returncode != 0:
                    assert process.stderr is not None
                    self.fail(process.stderr.read())
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    # ==========================================
    # Function: Keep stdio responsive during waits and honor request-scoped cancellation.
    # Method: Interleave wait, ping, cancellation notification, observation, and task cancellation.
    # ==========================================
    def test_wait_is_concurrent_and_request_cancellation_preserves_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            work = root / "work"
            work.mkdir()
            config_path = root / "config.json"
            document = {
                "version": 1,
                "defaults": {"allowed_work_roots": [str(work)]},
                "agents": {
                    "slow": {
                        "command": [
                            sys.executable,
                            str(FAKE_AGENT),
                            "--sleep",
                            "10",
                            "{prompt}",
                        ],
                        "prompt_mode": "argument",
                        "timeout_sec": 20,
                    }
                },
            }
            config_path.write_text(json.dumps(document), encoding="utf-8")
            environment = dict(os.environ)
            environment["AGENT_BRIDGE_CONFIG"] = str(config_path)
            environment["AGENT_BRIDGE_STATE_DIR"] = str(root / "state")
            process = subprocess.Popen(
                ["bash", str(START_SCRIPT)],
                cwd=PLUGIN_ROOT,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            reader: threading.Thread | None = None
            try:
                self.send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"},
                    },
                )
                started = self.tool_payload(
                    self.send(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {
                                "name": "start_task",
                                "arguments": {
                                    "agent": "slow",
                                    "prompt": "keep-running",
                                    "cwd": str(work),
                                },
                            },
                        },
                    )
                )
                deadline = time.monotonic() + 2
                request_id = 3
                while time.monotonic() < deadline:
                    current = self.tool_payload(
                        self.send(
                            process,
                            {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "method": "tools/call",
                                "params": {
                                    "name": "get_task",
                                    "arguments": {"task_id": started["task_id"]},
                                },
                            },
                        )
                    )
                    request_id += 1
                    if current["status"] == "running":
                        break
                    time.sleep(0.01)
                else:
                    self.fail(f"slow task did not start: {current}")

                responses: queue.Queue[dict[str, object]] = queue.Queue()

                # ==========================================
                # Function: Continuously decode server responses for out-of-order assertions.
                # Method: Transfer every stdout JSON line into a thread-safe test queue.
                # ==========================================
                def collect_responses() -> None:
                    assert process.stdout is not None
                    for line in process.stdout:
                        response = json.loads(line)
                        if isinstance(response, dict):
                            responses.put(response)

                reader = threading.Thread(target=collect_responses, daemon=True)
                reader.start()
                self.write(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 100,
                        "method": "tools/call",
                        "params": {
                            "name": "wait_task",
                            "arguments": {"task_id": started["task_id"], "wait_sec": 5},
                        },
                    },
                )
                started_ping = time.monotonic()
                self.write(process, {"jsonrpc": "2.0", "id": 101, "method": "ping"})
                ping = responses.get(timeout=1)
                self.assertEqual(ping["id"], 101)
                self.assertLess(time.monotonic() - started_ping, 1)

                self.write(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": 100, "reason": "test cancellation"},
                    },
                )
                self.write(process, {"jsonrpc": "2.0", "id": 102, "method": "ping"})
                self.assertEqual(responses.get(timeout=1)["id"], 102)
                with self.assertRaises(queue.Empty):
                    responses.get(timeout=0.3)

                self.write(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 103,
                        "method": "tools/call",
                        "params": {
                            "name": "get_task",
                            "arguments": {"task_id": started["task_id"]},
                        },
                    },
                )
                current = self.tool_payload(responses.get(timeout=1))
                self.assertEqual(current["status"], "running")

                self.write(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 104,
                        "method": "tools/call",
                        "params": {
                            "name": "wait_task",
                            "arguments": {"task_id": started["task_id"], "wait_sec": 5},
                        },
                    },
                )
                self.write(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 105,
                        "method": "tools/call",
                        "params": {
                            "name": "cancel_task",
                            "arguments": {"task_id": started["task_id"]},
                        },
                    },
                )
                received: dict[object, dict[str, object]] = {}
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and set(received) != {104, 105}:
                    response = responses.get(timeout=max(0.1, deadline - time.monotonic()))
                    received[response["id"]] = response
                self.assertEqual(set(received), {104, 105})
                cancelled = self.tool_payload(received[105])
                self.assertIn(cancelled["status"], {"cancelling", "cancelled"})
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                process.wait(timeout=10)
                if reader is not None:
                    reader.join(timeout=2)
                if process.returncode != 0:
                    assert process.stderr is not None
                    self.fail(process.stderr.read())
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
