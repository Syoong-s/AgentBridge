#!/usr/bin/env python3
"""Raw stdio MCP roundtrip test for the packaged Agent Bridge launcher."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
    # Function: Send one request and decode its single response line.
    # Method: Flush JSON to server stdin and require valid object output from stdout.
    # ==========================================
    def send(self, process: subprocess.Popen[str], request: object) -> dict[str, object]:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        self.assertTrue(line, "MCP server closed stdout before responding")
        response = json.loads(line)
        self.assertIsInstance(response, dict)
        return response

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
                        "reload_config",
                        "start_child_agent",
                        "start_task",
                        "send_followup",
                        "get_task",
                        "wait_task",
                        "list_tasks",
                        "cancel_task",
                    },
                )
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
                            "params": {"name": "reload_config", "arguments": {}},
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
                            "params": {"name": "list_agents", "arguments": {}},
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
                                "name": "start_task",
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
                completed = self.tool_payload(
                    self.send(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": 4,
                            "method": "tools/call",
                            "params": {
                                "name": "wait_task",
                                "arguments": {"task_id": started["task_id"], "wait_sec": 5},
                            },
                        },
                    )
                )
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
                                "name": "send_followup",
                                "arguments": {
                                    "parent_task_id": completed["task_id"],
                                    "prompt": "followup-result",
                                },
                            },
                        },
                    )
                )
                followed = self.tool_payload(
                    self.send(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": 6,
                            "method": "tools/call",
                            "params": {
                                "name": "wait_task",
                                "arguments": {"task_id": followup["task_id"], "wait_sec": 5},
                            },
                        },
                    )
                )
                self.assertEqual(followed["status"], "succeeded")
                self.assertEqual(followed["stdout"]["text"], "followup-result")
                self.assertEqual(followed["parent_task_id"], completed["task_id"])
                self.assertEqual(followed["root_task_id"], completed["task_id"])
                self.assertEqual(followed["session_id"], completed["session_id"])
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


if __name__ == "__main__":
    unittest.main()
