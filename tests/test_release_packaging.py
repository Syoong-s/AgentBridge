#!/usr/bin/env python3
"""Release archive integrity and reproducibility tests for AgentBridge."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from package_release import build_release, validate_layout  # noqa: E402


# ==========================================
# Class: Deterministic AgentBridge release packaging tests.
# Method: Build isolated archives twice and inspect layout, metadata, modes, and digests.
# ==========================================
class ReleasePackagingTests(unittest.TestCase):
    # ==========================================
    # Function: Build a release with a fixed timestamp inside one temporary directory.
    # Method: Bind the package tag to the current manifest version.
    # ==========================================
    def build_in(self, output_dir: Path) -> dict[str, Path]:
        version = validate_layout(REPOSITORY_ROOT)
        return build_release(
            REPOSITORY_ROOT,
            output_dir,
            tag=f"v{version}",
            requested_epoch=1_700_000_000,
        )

    # ==========================================
    # Function: Validate install roots, required files, executable mode, and checksums.
    # Method: Inspect both archive formats and compare their normalized member sets.
    # ==========================================
    def test_layout_modes_and_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifacts = self.build_in(Path(temporary_directory))
            with zipfile.ZipFile(artifacts["zip"]) as archive:
                zip_names = set(archive.namelist())
                roots = {Path(name).parts[0] for name in zip_names}
                self.assertEqual(roots, {".agents", "plugins"})
                self.assertIn("plugins/agent-bridge/skills/agent-bridge/SKILL.md", zip_names)
                mode = (
                    archive.getinfo("plugins/agent-bridge/scripts/start.sh").external_attr >> 16
                ) & 0o777
                self.assertEqual(mode, 0o755)
                self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in zip_names))
            with tarfile.open(artifacts["tar.gz"], "r:gz") as archive:
                tar_names = set(archive.getnames())
                self.assertEqual(zip_names, tar_names)
                self.assertEqual(archive.getmember("plugins/agent-bridge/scripts/start.sh").mode, 0o755)
            expected = {
                f"{self.digest(artifacts['zip'])}  {artifacts['zip'].name}",
                f"{self.digest(artifacts['tar.gz'])}  {artifacts['tar.gz'].name}",
            }
            self.assertEqual(
                set(artifacts["checksums"].read_text(encoding="utf-8").splitlines()),
                expected,
            )

    # ==========================================
    # Function: Ensure identical source and epoch produce byte-identical artifacts.
    # Method: Build twice in separate destinations and compare every output byte.
    # ==========================================
    def test_artifacts_are_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as first_directory:
            with tempfile.TemporaryDirectory() as second_directory:
                first = self.build_in(Path(first_directory))
                second = self.build_in(Path(second_directory))
                for key in ("zip", "tar.gz", "checksums"):
                    self.assertEqual(first[key].read_bytes(), second[key].read_bytes(), key)

    # ==========================================
    # Function: Prevent publishing a tag that disagrees with plugin metadata.
    # Method: Validate a deliberate mismatch and require a clear ValueError.
    # ==========================================
    def test_tag_must_match_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_layout(REPOSITORY_ROOT, tag="v9.9.9")

    # ==========================================
    # Function: Compute one test artifact digest.
    # Method: Hash its complete small byte sequence for exact checksum comparison.
    # ==========================================
    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
