#!/usr/bin/env python3
"""Build deterministic installable AgentBridge marketplace release archives."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
import zipfile


ARCHIVE_ROOTS = (".agents", "plugins")
REQUIRED_MEMBERS = {
    ".agents/plugins/marketplace.json",
    "plugins/agent-bridge/.codex-plugin/plugin.json",
    "plugins/agent-bridge/.mcp.json",
    "plugins/agent-bridge/LICENSE",
    "plugins/agent-bridge/scripts/start.sh",
}
IGNORED_PARTS = {"__pycache__"}
IGNORED_NAMES = {".DS_Store", "Thumbs.db"}
IGNORED_SUFFIXES = {".log", ".pyc", ".pyo"}
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


# ==========================================
# Function: Read a UTF-8 JSON object with a useful path-specific error.
# Method: Decode the file and reject non-object document roots.
# ==========================================
def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


# ==========================================
# Function: Validate marketplace and plugin identity before packaging.
# Method: Require exact names, source path, policy fields, SemVer, and optional matching tag.
# ==========================================
def validate_layout(repository_root: Path, tag: str | None = None) -> str:
    marketplace_path = repository_root / ".agents" / "plugins" / "marketplace.json"
    manifest_path = repository_root / "plugins" / "agent-bridge" / ".codex-plugin" / "plugin.json"
    for required in (marketplace_path, manifest_path, repository_root / "plugins/agent-bridge/LICENSE"):
        if not required.is_file():
            raise ValueError(f"missing required release file: {required}")

    manifest = load_json_object(manifest_path)
    if manifest.get("name") != "agent-bridge":
        raise ValueError("plugin manifest name must be 'agent-bridge'")
    version = manifest.get("version")
    if not isinstance(version, str) or SEMVER_PATTERN.fullmatch(version) is None:
        raise ValueError("plugin manifest version must be valid SemVer")
    if manifest.get("mcpServers") != "./.mcp.json" or manifest.get("skills") != "./skills/":
        raise ValueError("plugin manifest must reference bundled MCP and skill components")

    marketplace = load_json_object(marketplace_path)
    if marketplace.get("name") != "agent-bridge":
        raise ValueError("marketplace name must be 'agent-bridge'")
    entries = marketplace.get("plugins")
    if not isinstance(entries, list):
        raise ValueError("marketplace plugins must be an array")
    matching = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("name") == "agent-bridge"
    ]
    if len(matching) != 1:
        raise ValueError("marketplace must contain exactly one agent-bridge entry")
    entry = matching[0]
    if entry.get("source") != {"source": "local", "path": "./plugins/agent-bridge"}:
        raise ValueError("agent-bridge marketplace source path is invalid")
    if entry.get("policy") != {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }:
        raise ValueError("agent-bridge marketplace policy is invalid")
    if entry.get("category") != "Developer Tools":
        raise ValueError("agent-bridge marketplace category is invalid")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"release tag {tag!r} does not match version {version!r}")
    return version


# ==========================================
# Function: Decide whether one candidate is transient host output.
# Method: Match a small explicit set of cache, log, and platform-generated names.
# ==========================================
def should_ignore(relative_path: PurePosixPath) -> bool:
    return (
        any(part in IGNORED_PARTS for part in relative_path.parts)
        or relative_path.name in IGNORED_NAMES
        or relative_path.suffix in IGNORED_SUFFIXES
    )


# ==========================================
# Function: Collect the exact release file set.
# Method: Walk only marketplace/plugin roots, reject links/special files, and sort POSIX paths.
# ==========================================
def collect_files(repository_root: Path) -> list[tuple[PurePosixPath, Path]]:
    files: list[tuple[PurePosixPath, Path]] = []
    for root_name in ARCHIVE_ROOTS:
        source_root = repository_root / root_name
        if not source_root.is_dir():
            raise ValueError(f"missing release root: {source_root}")
        for source_path in source_root.rglob("*"):
            relative = PurePosixPath(source_path.relative_to(repository_root).as_posix())
            if should_ignore(relative):
                continue
            if source_path.is_symlink():
                raise ValueError(f"release roots may not contain symlinks: {source_path}")
            if source_path.is_file():
                files.append((relative, source_path))
            elif not source_path.is_dir():
                raise ValueError(f"unsupported release member type: {source_path}")
    files.sort(key=lambda item: item[0].as_posix())
    names = {relative.as_posix() for relative, _source in files}
    missing = REQUIRED_MEMBERS - names
    if missing:
        raise ValueError(f"release is missing required members: {sorted(missing)}")
    return files


# ==========================================
# Function: Resolve one reproducible timestamp for all archive members.
# Method: Prefer an explicit value or SOURCE_DATE_EPOCH, then use the current commit time.
# ==========================================
def resolve_epoch(repository_root: Path, requested_epoch: int | None = None) -> int:
    if requested_epoch is not None:
        epoch = requested_epoch
    elif os.environ.get("SOURCE_DATE_EPOCH"):
        try:
            epoch = int(os.environ["SOURCE_DATE_EPOCH"])
        except ValueError as exc:
            raise ValueError("SOURCE_DATE_EPOCH must be an integer") from exc
    else:
        try:
            completed = subprocess.run(
                ["git", "log", "-1", "--format=%ct"],
                cwd=repository_root,
                check=True,
                capture_output=True,
                text=True,
            )
            epoch = int(completed.stdout.strip())
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            raise ValueError("cannot resolve release timestamp; set SOURCE_DATE_EPOCH") from exc
    if not 0 <= epoch <= 4_294_967_295:
        raise ValueError("release timestamp must fit an unsigned 32-bit value")
    return epoch


# ==========================================
# Function: Normalize one file mode for portable extraction.
# Method: Preserve only whether any executable bit is present.
# ==========================================
def normalized_mode(path: Path) -> int:
    return 0o755 if stat.S_IMODE(path.stat().st_mode) & 0o111 else 0o644


# ==========================================
# Function: Convert an epoch to ZIP's constrained UTC timestamp tuple.
# Method: Clamp to ZIP's supported year range before conversion.
# ==========================================
def zip_timestamp(epoch: int) -> tuple[int, int, int, int, int, int]:
    bounded = min(max(epoch, 315_532_800), 4_354_819_199)
    utc = time.gmtime(bounded)
    return (utc.tm_year, utc.tm_mon, utc.tm_mday, utc.tm_hour, utc.tm_min, utc.tm_sec)


# ==========================================
# Function: Allocate an adjacent temporary output for atomic replacement.
# Method: Create and close a named file in the destination filesystem.
# ==========================================
def temporary_output(destination: Path) -> Path:
    handle = tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    )
    handle.close()
    return Path(handle.name)


# ==========================================
# Function: Write a deterministic ZIP marketplace snapshot.
# Method: Apply sorted names, stable timestamps, normalized modes, and fixed compression.
# ==========================================
def write_zip(destination: Path, files: list[tuple[PurePosixPath, Path]], epoch: int) -> None:
    temporary = temporary_output(destination)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, source in files:
                info = zipfile.ZipInfo(relative.as_posix(), date_time=zip_timestamp(epoch))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | normalized_mode(source)) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, source.read_bytes())
        os.replace(temporary, destination)
        destination.chmod(0o644)
    finally:
        temporary.unlink(missing_ok=True)


# ==========================================
# Function: Write a deterministic gzip-compressed tar marketplace snapshot.
# Method: Construct normalized TarInfo objects inside a gzip stream with stable metadata.
# ==========================================
def write_tar_gz(destination: Path, files: list[tuple[PurePosixPath, Path]], epoch: int) -> None:
    temporary = temporary_output(destination)
    try:
        with temporary.open("wb") as raw_output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=epoch) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    for relative, source in files:
                        data = source.read_bytes()
                        info = tarfile.TarInfo(relative.as_posix())
                        info.size = len(data)
                        info.mtime = epoch
                        info.mode = normalized_mode(source)
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        archive.addfile(info, fileobj=io.BytesIO(data))
        os.replace(temporary, destination)
        destination.chmod(0o644)
    finally:
        temporary.unlink(missing_ok=True)


# ==========================================
# Function: Compute the complete SHA-256 digest of one artifact.
# Method: Stream fixed-size blocks to avoid loading release archives into memory.
# ==========================================
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ==========================================
# Function: Build both release formats and their checksum manifest.
# Method: Validate identity, collect one source set, and atomically replace named outputs.
# ==========================================
def build_release(
    repository_root: Path,
    output_dir: Path,
    tag: str | None = None,
    requested_epoch: int | None = None,
) -> dict[str, Path]:
    root = repository_root.resolve()
    version = validate_layout(root, tag=tag)
    files = collect_files(root)
    epoch = resolve_epoch(root, requested_epoch)
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / f"agent-bridge-{version}.zip"
    tar_path = output_dir / f"agent-bridge-{version}.tar.gz"
    checksums_path = output_dir / "SHA256SUMS"
    write_zip(zip_path, files, epoch)
    write_tar_gz(tar_path, files, epoch)
    checksums = (
        f"{sha256(zip_path)}  {zip_path.name}\n"
        f"{sha256(tar_path)}  {tar_path.name}\n"
    )
    checksums_path.write_text(checksums, encoding="utf-8", newline="\n")
    checksums_path.chmod(0o644)
    return {"zip": zip_path, "tar.gz": tar_path, "checksums": checksums_path}


# ==========================================
# Function: Parse command-line options for release packaging.
# Method: Expose repository, output, tag, and deterministic timestamp overrides.
# ==========================================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--tag")
    parser.add_argument("--source-date-epoch", type=int)
    return parser.parse_args()


# ==========================================
# Function: Run deterministic release packaging from the command line.
# Method: Build artifacts, print their paths, and return a concise failure code.
# ==========================================
def main() -> int:
    arguments = parse_args()
    try:
        artifacts = build_release(
            arguments.repository_root,
            arguments.output_dir,
            tag=arguments.tag,
            requested_epoch=arguments.source_date_epoch,
        )
    except ValueError as exc:
        print(f"release packaging failed: {exc}", file=sys.stderr)
        return 2
    for path in artifacts.values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
