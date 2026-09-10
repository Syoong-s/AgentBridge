"""Load and validate Agent Bridge JSON configuration without third-party packages."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
PROMPT_MODES = {"argument", "stdin", "file"}
KNOWN_PLACEHOLDERS = {"{prompt}", "{prompt_file}", "{cwd}", "{task_id}"}
SELECTION_FIELDS = {"default", "arguments", "allowed_values"}
SESSION_FIELDS = {"id_source", "start_arguments", "resume_arguments", "id_json_path"}
SESSION_ID_SOURCES = {"generated_uuid", "stdout_json"}
TOP_LEVEL_FIELDS = {"version", "defaults", "agents"}
DEFAULT_FIELDS = {
    "max_concurrent_tasks",
    "max_retained_tasks",
    "default_timeout_sec",
    "max_timeout_sec",
    "default_max_output_bytes",
    "allowed_work_roots",
}
AGENT_FIELDS = {
    "enabled",
    "description",
    "command",
    "prompt_mode",
    "inherit_env",
    "environment",
    "timeout_sec",
    "max_output_bytes",
    "allow_extra_args",
    "model",
    "reasoning_effort",
    "session",
}


# ==========================================
# Class: Configuration validation failure with an actionable user-facing message.
# Method: Specialize ValueError so MCP startup and reload can report schema problems cleanly.
# ==========================================
class ConfigError(ValueError):
    """Raised when the parameter file violates the Agent Bridge schema."""


# ==========================================
# Class: Immutable mapping from one portable selector to provider-specific argv.
# Method: Store an optional default, an argv template, and an optional allowlist.
# ==========================================
@dataclass(frozen=True)
class SelectionConfig:
    default: str | None
    arguments: tuple[str, ...]
    allowed_values: tuple[str, ...] | None


# ==========================================
# Class: Immutable provider session creation, extraction, and resume contract.
# Method: Represent generated UUID or stdout-JSON identifiers with direct argv templates.
# ==========================================
@dataclass(frozen=True)
class SessionConfig:
    id_source: str
    start_arguments: tuple[str, ...]
    resume_arguments: tuple[str, ...]
    id_json_path: tuple[str, ...]


# ==========================================
# Class: Immutable configuration for one external agent alias.
# Method: Store validated argv, portable selectors, session behavior, and execution limits.
# ==========================================
@dataclass(frozen=True)
class AgentConfig:
    alias: str
    description: str
    command: tuple[str, ...]
    prompt_mode: str
    enabled: bool
    inherit_env: bool
    environment: Mapping[str, str]
    timeout_sec: int
    max_output_bytes: int
    allow_extra_args: bool
    model: SelectionConfig | None
    reasoning_effort: SelectionConfig | None
    session: SessionConfig | None


# ==========================================
# Class: Immutable top-level bridge configuration.
# Method: Bind validated defaults, work-root policy, aliases, and source path.
# ==========================================
@dataclass(frozen=True)
class BridgeConfig:
    path: Path
    state_dir: Path
    max_concurrent_tasks: int
    max_retained_tasks: int
    max_timeout_sec: int
    allowed_work_roots: tuple[Path, ...]
    agents: Mapping[str, AgentConfig]


# ==========================================
# Function: Resolve the active Agent Bridge parameter file.
# Method: Prefer an explicit environment override, then plugin data, user config, and packaged example.
# ==========================================
def resolve_config_path(plugin_root: Path) -> Path:
    explicit = os.environ.get("AGENT_BRIDGE_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()

    plugin_data = os.environ.get("PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    candidates: list[Path] = []
    if plugin_data:
        candidates.append(Path(plugin_data).expanduser() / "config.json")
    candidates.append(Path.home() / ".config" / "agent-bridge" / "config.json")
    candidates.append(plugin_root / "config" / "agents.example.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ConfigError("no Agent Bridge configuration file could be resolved")


# ==========================================
# Function: Resolve the writable task-state directory.
# Method: Prefer an explicit override or plugin data, falling back to XDG/user state storage.
# ==========================================
def resolve_state_dir(config_path: Path) -> Path:
    explicit = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    plugin_data = os.environ.get("PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data:
        return (Path(plugin_data).expanduser() / "tasks").resolve()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return (base / "agent-bridge" / "tasks").resolve()


# ==========================================
# Function: Load a JSON object with a path-specific diagnostic.
# Method: Decode UTF-8 JSON and reject non-object document roots.
# ==========================================
def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise ConfigError(f"configuration root in {path} must be a JSON object")
    return value


# ==========================================
# Function: Read one bounded integer configuration value.
# Method: Reject booleans and values outside the caller-provided inclusive range.
# ==========================================
def bounded_int(source: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = source.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


# ==========================================
# Function: Reject misspelled or unsupported object keys.
# Method: Compare exact JSON field names and report every unknown entry in sorted order.
# ==========================================
def reject_unknown_keys(source: Mapping[str, Any], allowed: set[str], location: str) -> None:
    unknown = set(source) - allowed
    if unknown:
        raise ConfigError(f"{location} contains unknown fields: {sorted(unknown)}")


# ==========================================
# Function: Validate one environment mapping.
# Method: Require portable variable names and string values without NUL bytes.
# ==========================================
def parse_environment(value: Any, alias: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"agents.{alias}.environment must be an object")
    parsed: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise ConfigError(f"agents.{alias}.environment contains invalid variable name {key!r}")
        if not isinstance(item, str) or "\0" in item:
            raise ConfigError(f"agents.{alias}.environment.{key} must be a NUL-free string")
        parsed[key] = item
    return parsed


# ==========================================
# Function: Find supported placeholders inside a configured string.
# Method: Scan brace-delimited names and reject misspellings instead of leaving them literal.
# ==========================================
def validate_placeholders(
    value: str,
    location: str,
    allowed: set[str] | None = None,
) -> None:
    accepted = KNOWN_PLACEHOLDERS if allowed is None else allowed
    for match in re.findall(r"\{[^{}]+\}", value):
        if match not in accepted:
            raise ConfigError(f"{location} contains unsupported placeholder {match!r}")


# ==========================================
# Function: Parse one portable model or reasoning selector mapping.
# Method: Validate its exact placeholder, optional default, and optional value allowlist.
# ==========================================
def parse_selection(
    value: Any,
    alias: str,
    field_name: str,
    placeholder: str,
) -> SelectionConfig | None:
    if value is None:
        return None
    location = f"agents.{alias}.{field_name}"
    if not isinstance(value, dict):
        raise ConfigError(f"{location} must be an object")
    reject_unknown_keys(value, SELECTION_FIELDS, location)
    arguments = value.get("arguments")
    if not isinstance(arguments, list) or not arguments:
        raise ConfigError(f"{location}.arguments must be a non-empty string array")
    parsed_arguments: list[str] = []
    for index, argument in enumerate(arguments):
        if not isinstance(argument, str) or not argument or "\0" in argument:
            raise ConfigError(f"{location}.arguments[{index}] must be a non-empty NUL-free string")
        validate_placeholders(argument, f"{location}.arguments[{index}]", {placeholder})
        parsed_arguments.append(argument)
    if "\0".join(parsed_arguments).count(placeholder) != 1:
        raise ConfigError(f"{location}.arguments requires exactly one {placeholder} placeholder")

    default = value.get("default")
    if default is not None and (
        not isinstance(default, str)
        or not default
        or "\0" in default
        or len(default.encode("utf-8")) > 256
    ):
        raise ConfigError(f"{location}.default must be null or a non-empty NUL-free string up to 256 bytes")

    raw_allowed = value.get("allowed_values")
    allowed_values: tuple[str, ...] | None = None
    if raw_allowed is not None:
        if (
            not isinstance(raw_allowed, list)
            or not raw_allowed
            or not all(
                isinstance(item, str)
                and item
                and "\0" not in item
                and len(item.encode("utf-8")) <= 256
                for item in raw_allowed
            )
        ):
            raise ConfigError(
                f"{location}.allowed_values must be a non-empty array of NUL-free strings up to 256 bytes"
            )
        if len(set(raw_allowed)) != len(raw_allowed):
            raise ConfigError(f"{location}.allowed_values must not contain duplicates")
        allowed_values = tuple(raw_allowed)
        if default is not None and default not in allowed_values:
            raise ConfigError(f"{location}.default must appear in allowed_values")
    return SelectionConfig(
        default=default,
        arguments=tuple(parsed_arguments),
        allowed_values=allowed_values,
    )


# ==========================================
# Function: Parse one provider session lifecycle mapping.
# Method: Validate generated or extracted IDs and the exact resume placeholder contract.
# ==========================================
def parse_session(value: Any, alias: str) -> SessionConfig | None:
    if value is None:
        return None
    location = f"agents.{alias}.session"
    if not isinstance(value, dict):
        raise ConfigError(f"{location} must be an object")
    reject_unknown_keys(value, SESSION_FIELDS, location)
    id_source = value.get("id_source")
    if not isinstance(id_source, str) or id_source not in SESSION_ID_SOURCES:
        raise ConfigError(f"{location}.id_source must be one of {sorted(SESSION_ID_SOURCES)}")

    parsed_groups: dict[str, tuple[str, ...]] = {}
    for field_name in ("start_arguments", "resume_arguments"):
        raw_arguments = value.get(field_name, [])
        if not isinstance(raw_arguments, list):
            raise ConfigError(f"{location}.{field_name} must be a string array")
        parsed_arguments: list[str] = []
        for index, argument in enumerate(raw_arguments):
            if not isinstance(argument, str) or not argument or "\0" in argument:
                raise ConfigError(
                    f"{location}.{field_name}[{index}] must be a non-empty NUL-free string"
                )
            validate_placeholders(
                argument,
                f"{location}.{field_name}[{index}]",
                {"{session_id}"},
            )
            parsed_arguments.append(argument)
        parsed_groups[field_name] = tuple(parsed_arguments)

    start_arguments = parsed_groups["start_arguments"]
    resume_arguments = parsed_groups["resume_arguments"]
    if "\0".join(resume_arguments).count("{session_id}") != 1:
        raise ConfigError(
            f"{location}.resume_arguments requires exactly one {{session_id}} placeholder"
        )
    start_id_count = "\0".join(start_arguments).count("{session_id}")
    raw_json_path = value.get("id_json_path", [])
    if (
        not isinstance(raw_json_path, list)
        or not all(isinstance(item, str) and item and "\0" not in item for item in raw_json_path)
    ):
        raise ConfigError(f"{location}.id_json_path must be a string array")
    if id_source == "generated_uuid":
        if start_id_count != 1:
            raise ConfigError(
                f"{location}.start_arguments requires exactly one {{session_id}} placeholder"
            )
        if raw_json_path:
            raise ConfigError(f"{location}.id_json_path is only valid for stdout_json")
    else:
        if start_id_count:
            raise ConfigError(
                f"{location}.start_arguments cannot use {{session_id}} with stdout_json"
            )
        if not raw_json_path:
            raise ConfigError(f"{location}.id_json_path must be non-empty for stdout_json")
    return SessionConfig(
        id_source=id_source,
        start_arguments=start_arguments,
        resume_arguments=resume_arguments,
        id_json_path=tuple(raw_json_path),
    )


# ==========================================
# Function: Parse and validate one external-agent alias.
# Method: Enforce direct argv execution and prompt-mode-specific placeholder contracts.
# ==========================================
def parse_agent(
    alias: str,
    value: Any,
    default_timeout_sec: int,
    max_timeout_sec: int,
    default_max_output_bytes: int,
) -> AgentConfig:
    if ALIAS_PATTERN.fullmatch(alias) is None:
        raise ConfigError(f"invalid agent alias {alias!r}")
    if not isinstance(value, dict):
        raise ConfigError(f"agents.{alias} must be an object")
    reject_unknown_keys(value, AGENT_FIELDS, f"agents.{alias}")

    command = value.get("command")
    if not isinstance(command, list) or not command:
        raise ConfigError(f"agents.{alias}.command must be a non-empty string array")
    parsed_command: list[str] = []
    for index, argument in enumerate(command):
        if not isinstance(argument, str) or not argument or "\0" in argument:
            raise ConfigError(f"agents.{alias}.command[{index}] must be a non-empty NUL-free string")
        validate_placeholders(argument, f"agents.{alias}.command[{index}]")
        parsed_command.append(argument)
    if "{prompt}" in parsed_command[0] or "{prompt_file}" in parsed_command[0]:
        raise ConfigError(f"agents.{alias}.command[0] cannot contain a prompt placeholder")

    prompt_mode = value.get("prompt_mode", "argument")
    if not isinstance(prompt_mode, str) or prompt_mode not in PROMPT_MODES:
        raise ConfigError(f"agents.{alias}.prompt_mode must be one of {sorted(PROMPT_MODES)}")
    joined_command = "\0".join(parsed_command)
    prompt_count = joined_command.count("{prompt}")
    prompt_file_count = joined_command.count("{prompt_file}")
    if prompt_mode == "argument" and prompt_count != 1:
        raise ConfigError(f"agents.{alias} argument mode requires exactly one {{prompt}} placeholder")
    if prompt_mode == "file" and prompt_file_count != 1:
        raise ConfigError(f"agents.{alias} file mode requires exactly one {{prompt_file}} placeholder")
    if prompt_mode == "stdin" and (prompt_count or prompt_file_count):
        raise ConfigError(f"agents.{alias} stdin mode cannot include prompt placeholders")
    if prompt_mode != "argument" and prompt_count:
        raise ConfigError(f"agents.{alias} may use {{prompt}} only in argument mode")
    if prompt_mode != "file" and prompt_file_count:
        raise ConfigError(f"agents.{alias} may use {{prompt_file}} only in file mode")

    description = value.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"agents.{alias}.description must be a string")
    enabled = value.get("enabled", True)
    inherit_env = value.get("inherit_env", True)
    allow_extra_args = value.get("allow_extra_args", False)
    for key, flag in (
        ("enabled", enabled),
        ("inherit_env", inherit_env),
        ("allow_extra_args", allow_extra_args),
    ):
        if not isinstance(flag, bool):
            raise ConfigError(f"agents.{alias}.{key} must be a boolean")

    timeout_sec = bounded_int(value, "timeout_sec", default_timeout_sec, 1, max_timeout_sec)
    max_output_bytes = bounded_int(
        value,
        "max_output_bytes",
        default_max_output_bytes,
        1024,
        100 * 1024 * 1024,
    )
    environment = parse_environment(value.get("environment"), alias)
    for key, item in environment.items():
        validate_placeholders(item, f"agents.{alias}.environment.{key}")
    model = parse_selection(value.get("model"), alias, "model", "{model}")
    reasoning_effort = parse_selection(
        value.get("reasoning_effort"),
        alias,
        "reasoning_effort",
        "{reasoning_effort}",
    )
    session = parse_session(value.get("session"), alias)

    return AgentConfig(
        alias=alias,
        description=description,
        command=tuple(parsed_command),
        prompt_mode=prompt_mode,
        enabled=enabled,
        inherit_env=inherit_env,
        environment=environment,
        timeout_sec=timeout_sec,
        max_output_bytes=max_output_bytes,
        allow_extra_args=allow_extra_args,
        model=model,
        reasoning_effort=reasoning_effort,
        session=session,
    )


# ==========================================
# Function: Load and fully validate the active bridge configuration.
# Method: Parse defaults first, canonicalize allowed roots, then validate every alias.
# ==========================================
def load_config(path: Path, state_dir: Path | None = None) -> BridgeConfig:
    resolved_path = path.expanduser().resolve()
    document = load_json_object(resolved_path)
    reject_unknown_keys(document, TOP_LEVEL_FIELDS, "configuration")
    if document.get("version") != 1:
        raise ConfigError("configuration version must be integer 1")
    defaults = document.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError("defaults must be an object")
    reject_unknown_keys(defaults, DEFAULT_FIELDS, "defaults")

    max_concurrent_tasks = bounded_int(defaults, "max_concurrent_tasks", 4, 1, 32)
    max_retained_tasks = bounded_int(defaults, "max_retained_tasks", 100, 1, 10000)
    default_timeout_sec = bounded_int(defaults, "default_timeout_sec", 3600, 1, 86400)
    max_timeout_sec = bounded_int(defaults, "max_timeout_sec", 86400, 1, 604800)
    if default_timeout_sec > max_timeout_sec:
        raise ConfigError("default_timeout_sec cannot exceed max_timeout_sec")
    default_max_output_bytes = bounded_int(
        defaults,
        "default_max_output_bytes",
        2 * 1024 * 1024,
        1024,
        100 * 1024 * 1024,
    )

    raw_roots = defaults.get("allowed_work_roots", [])
    if not isinstance(raw_roots, list) or not all(isinstance(item, str) for item in raw_roots):
        raise ConfigError("allowed_work_roots must be a string array")
    allowed_roots: list[Path] = []
    for raw_root in raw_roots:
        if not raw_root or "\0" in raw_root:
            raise ConfigError("allowed work roots must be non-empty NUL-free paths")
        candidate = Path(raw_root).expanduser()
        if not candidate.is_absolute():
            raise ConfigError(f"allowed work root must be absolute: {raw_root!r}")
        try:
            root = candidate.resolve()
        except (OSError, ValueError) as exc:
            raise ConfigError(f"invalid allowed work root {raw_root!r}: {exc}") from exc
        allowed_roots.append(root)

    raw_agents = document.get("agents")
    if not isinstance(raw_agents, dict) or not raw_agents:
        raise ConfigError("agents must be a non-empty object")
    agents = {
        alias: parse_agent(
            alias,
            value,
            default_timeout_sec,
            max_timeout_sec,
            default_max_output_bytes,
        )
        for alias, value in raw_agents.items()
    }
    resolved_state = (state_dir or resolve_state_dir(resolved_path)).expanduser().resolve()
    return BridgeConfig(
        path=resolved_path,
        state_dir=resolved_state,
        max_concurrent_tasks=max_concurrent_tasks,
        max_retained_tasks=max_retained_tasks,
        max_timeout_sec=max_timeout_sec,
        allowed_work_roots=tuple(allowed_roots),
        agents=agents,
    )


# ==========================================
# Function: Resolve and load configuration for the installed plugin root.
# Method: Apply the documented search order and optional state-directory override.
# ==========================================
def load_active_config(plugin_root: Path) -> BridgeConfig:
    path = resolve_config_path(plugin_root)
    return load_config(path)
