#!/usr/bin/env python3
"""Deterministic fake external agent used by Agent Bridge integration tests."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time


# ==========================================
# Function: Parse the fake agent's deterministic behavior controls.
# Method: Use only explicit arguments needed by runtime integration tests.
# ==========================================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--transport", choices=("argument", "stdin", "file"), default="argument")
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--fail", type=int, default=0)
    parser.add_argument("--stderr", default="")
    parser.add_argument("--environment")
    parser.add_argument("--spawn-child", action="store_true")
    parser.add_argument("--child-pid-file")
    parser.add_argument("--early-output", default="")
    return parser.parse_args()


# ==========================================
# Function: Read the prompt through the selected transport contract.
# Method: Consume argv text, all stdin, or a UTF-8 prompt file.
# ==========================================
def read_prompt(arguments: argparse.Namespace) -> str:
    if arguments.transport == "stdin":
        return sys.stdin.read()
    if arguments.transport == "file":
        if arguments.prompt is None:
            raise ValueError("file transport requires a prompt path")
        return Path(arguments.prompt).read_text(encoding="utf-8")
    if arguments.prompt is None:
        raise ValueError("argument transport requires a prompt")
    return arguments.prompt


# ==========================================
# Function: Execute the requested fake-agent behavior.
# Method: Optionally spawn, sleep, emit bounded-test data, and return a selected exit code.
# ==========================================
def main() -> int:
    arguments = parse_args()
    prompt = read_prompt(arguments)
    if arguments.spawn_child:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        if arguments.child_pid_file:
            Path(arguments.child_pid_file).write_text(str(child.pid), encoding="utf-8")
    if arguments.early_output:
        sys.stdout.write(arguments.early_output)
        sys.stdout.flush()
    if arguments.sleep:
        time.sleep(arguments.sleep)
    if arguments.stderr:
        sys.stderr.write(arguments.stderr)
    if arguments.environment:
        sys.stdout.write(os.environ.get(arguments.environment, "<missing>") + "|")
    sys.stdout.write(prompt * arguments.repeat)
    sys.stdout.flush()
    return arguments.fail


if __name__ == "__main__":
    raise SystemExit(main())
