#!/usr/bin/env python3
"""Configuration validation tests for Agent Bridge."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SCRIPTS = REPOSITORY_ROOT / "plugins" / "agent-bridge" / "scripts"
sys.path.insert(0, str(RUNTIME_SCRIPTS))

from agent_bridge.config import ConfigError, load_config  # noqa: E402


# ==========================================
# Class: Agent Bridge parameter-file schema tests.
# Method: Materialize isolated JSON inputs and assert semantic validation behavior.
# ==========================================
class ConfigTests(unittest.TestCase):
    # ==========================================
    # Function: Write one minimal configuration to a temporary path.
    # Method: Merge caller-provided agent values into a versioned document.
    # ==========================================
    def write_config(self, root: Path, agent: dict[str, object]) -> Path:
        path = root / "config.json"
        path.write_text(
            json.dumps({"version": 1, "defaults": {}, "agents": {"test": agent}}),
            encoding="utf-8",
        )
        return path

    # ==========================================
    # Function: Accept all three supported prompt transport modes.
    # Method: Load independent argument, stdin, and file aliases and inspect normalized modes.
    # ==========================================
    def test_prompt_modes(self) -> None:
        cases = {
            "argument": ["tool", "{prompt}"],
            "stdin": ["tool"],
            "file": ["tool", "{prompt_file}"],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for mode, command in cases.items():
                with self.subTest(mode=mode):
                    path = self.write_config(
                        root,
                        {"command": command, "prompt_mode": mode},
                    )
                    config = load_config(path, state_dir=root / f"state-{mode}")
                    self.assertEqual(config.agents["test"].prompt_mode, mode)

    # ==========================================
    # Function: Accept generic model, effort, and both provider session-ID strategies.
    # Method: Parse strict nested mappings and inspect their immutable normalized values.
    # ==========================================
    def test_selection_and_session_mappings(self) -> None:
        cases = (
            {
                "model": {"default": "sonnet", "arguments": ["--model", "{model}"]},
                "reasoning_effort": {
                    "default": "high",
                    "arguments": ["--effort", "{reasoning_effort}"],
                    "allowed_values": ["low", "high"],
                },
                "session": {
                    "id_source": "generated_uuid",
                    "start_arguments": ["--session-id", "{session_id}"],
                    "resume_arguments": ["--resume", "{session_id}"],
                },
            },
            {
                "session": {
                    "id_source": "stdout_json",
                    "resume_arguments": ["--conversation", "{session_id}"],
                    "id_json_path": ["metadata", "conversation_id"],
                }
            },
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for index, extra in enumerate(cases):
                with self.subTest(index=index):
                    path = self.write_config(
                        root,
                        {"command": ["tool", "{prompt}"], **extra},
                    )
                    agent = load_config(path, state_dir=root / f"state-{index}").agents["test"]
                    self.assertIsNotNone(agent.session)
            self.assertEqual(agent.session.id_json_path, ("metadata", "conversation_id"))

    # ==========================================
    # Function: Reject ambiguous or unsafe command template shapes.
    # Method: Exercise missing, misspelled, duplicate, and executable-position prompt placeholders.
    # ==========================================
    def test_invalid_placeholders_are_rejected(self) -> None:
        invalid_commands = (
            ["tool"],
            ["tool", "{promt}"],
            ["tool", "{prompt}", "{prompt}"],
            ["{prompt}", "tool"],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for command in invalid_commands:
                with self.subTest(command=command):
                    path = self.write_config(
                        root,
                        {"command": command, "prompt_mode": "argument"},
                    )
                    with self.assertRaises(ConfigError):
                        load_config(path, state_dir=root / "state")

    # ==========================================
    # Function: Reject invalid versions, aliases, environments, and limit ordering.
    # Method: Mutate one schema dimension at a time and require an actionable ConfigError.
    # ==========================================
    def test_invalid_schema_values_are_rejected(self) -> None:
        documents = (
            {"version": 2, "agents": {"test": {"command": ["tool", "{prompt}"]}}},
            {"version": 1, "agents": {"bad alias": {"command": ["tool", "{prompt}"]}}},
            {
                "version": 1,
                "agents": {
                    "test": {
                        "command": ["tool", "{prompt}"],
                        "environment": {"BAD-NAME": "x"},
                    }
                },
            },
            {
                "version": 1,
                "defaults": {"default_timeout_sec": 10, "max_timeout_sec": 5},
                "agents": {"test": {"command": ["tool", "{prompt}"]}},
            },
            {
                "version": 1,
                "defaults": {"allowed_work_roots": ["relative/path"]},
                "agents": {"test": {"command": ["tool", "{prompt}"]}},
            },
            {
                "version": 1,
                "agents": {
                    "test": {"command": ["tool", "{prompt}"], "enabledd": False}
                },
            },
            {
                "version": 1,
                "agents": {
                    "test": {
                        "command": ["tool", "{prompt}"],
                        "prompt_mode": ["argument"],
                    }
                },
            },
            {
                "version": 1,
                "defaults": {"max_concurrent_task": 2},
                "agents": {"test": {"command": ["tool", "{prompt}"]}},
            },
            {
                "version": 1,
                "unexpected": True,
                "agents": {"test": {"command": ["tool", "{prompt}"]}},
            },
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "config.json"
            for document in documents:
                with self.subTest(document=document):
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_config(path, state_dir=root / "state")

    # ==========================================
    # Function: Reject malformed nested selector and session contracts.
    # Method: Exercise unknown keys, placeholder counts, allowlists, and ID-source invariants.
    # ==========================================
    def test_invalid_selection_and_session_mappings_are_rejected(self) -> None:
        invalid_extras = (
            {"model": {"arguments": ["--model", "literal"]}},
            {"model": {"arguments": ["--model", "{reasoning_effort}"]}},
            {
                "reasoning_effort": {
                    "default": "medium",
                    "arguments": ["--effort", "{reasoning_effort}"],
                    "allowed_values": ["low", "high"],
                }
            },
            {
                "model": {
                    "arguments": ["--model", "{model}"],
                    "unknown": True,
                }
            },
            {
                "session": {
                    "id_source": ["generated_uuid"],
                    "start_arguments": ["--session-id", "{session_id}"],
                    "resume_arguments": ["--resume", "{session_id}"],
                }
            },
            {
                "session": {
                    "id_source": "generated_uuid",
                    "resume_arguments": ["--resume", "{session_id}"],
                }
            },
            {
                "session": {
                    "id_source": "stdout_json",
                    "resume_arguments": ["--conversation", "{session_id}"],
                }
            },
            {
                "session": {
                    "id_source": "stdout_json",
                    "start_arguments": ["--session-id", "{session_id}"],
                    "resume_arguments": ["--conversation", "{session_id}"],
                    "id_json_path": ["conversation_id"],
                }
            },
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for index, extra in enumerate(invalid_extras):
                with self.subTest(index=index):
                    path = self.write_config(
                        root,
                        {"command": ["tool", "{prompt}"], **extra},
                    )
                    with self.assertRaises(ConfigError):
                        load_config(path, state_dir=root / f"state-invalid-{index}")

    # ==========================================
    # Function: Lock the bundled Claude Code and Antigravity aliases to documented CLI contracts.
    # Method: Load the packaged JSON and assert executable, headless, selector, and resume mappings.
    # ==========================================
    def test_packaged_provider_commands(self) -> None:
        path = (
            REPOSITORY_ROOT
            / "plugins"
            / "agent-bridge"
            / "config"
            / "agents.example.json"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = load_config(path, state_dir=Path(temporary_directory) / "state")
        claude = config.agents["claude"]
        self.assertEqual(claude.command, ("claude", "-p", "--output-format", "text", "{prompt}"))
        self.assertEqual(claude.model.arguments, ("--model", "{model}"))
        self.assertEqual(claude.session.id_source, "generated_uuid")
        self.assertEqual(claude.session.resume_arguments, ("--resume", "{session_id}"))
        antigravity = config.agents["antigravity"]
        self.assertEqual(
            antigravity.command,
            ("agy", "-p", "--output-format", "json", "{prompt}"),
        )
        self.assertEqual(antigravity.reasoning_effort.allowed_values, ("low", "medium", "high"))
        self.assertEqual(antigravity.session.id_source, "stdout_json")
        self.assertEqual(antigravity.session.id_json_path, ("conversation_id",))
        self.assertEqual(
            antigravity.session.resume_arguments,
            ("--conversation", "{session_id}"),
        )


if __name__ == "__main__":
    unittest.main()
