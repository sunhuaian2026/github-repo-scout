#!/usr/bin/env python3
"""Public CLI tests for the one-command installer."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from maintenance.manage_skill import tree_hash

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "install.py"


class InstallerCliTests(unittest.TestCase):
    def run_installer(self, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = ""
        return subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--home",
                str(home),
                "--hermes-home",
                str(home / ".hermes"),
                *args,
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def target(self, home: Path, agent: str) -> Path:
        if agent == "hermes":
            return home / ".hermes" / "skills" / "research" / "github-repo-scout"
        if agent == "codex":
            return home / ".agents" / "skills" / "github-repo-scout"
        return home / ".claude" / "skills" / "github-repo-scout"

    def test_default_refuses_when_no_supported_agent_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            result = self.run_installer(home)
            self.assertEqual(result.returncode, 2)
            self.assertIn("no supported agents detected", result.stderr.lower())
            self.assertFalse(any(self.target(home, agent).exists() for agent in ("hermes", "codex", "claude")))

    def test_default_installs_only_detected_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".codex").mkdir()
            (home / ".claude").mkdir()
            result = self.run_installer(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("hermes: skipped", result.stdout)
            self.assertFalse(self.target(home, "hermes").exists())
            self.assertTrue(self.target(home, "codex").is_dir())
            self.assertTrue(self.target(home, "claude").is_dir())
            check = self.run_installer(home, "--check")
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertIn("hermes: skipped", check.stdout)

    def test_explicit_missing_agent_errors_without_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            result = self.run_installer(home, "--agent", "hermes")
            self.assertEqual(result.returncode, 2)
            self.assertIn("hermes is not detected", result.stderr.lower())
            self.assertFalse(self.target(home, "hermes").exists())

    def test_allow_missing_agent_permits_predeployment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            result = self.run_installer(home, "--agent", "hermes", "--allow-missing-agent")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.target(home, "hermes").is_dir())
            self.assertFalse((self.target(home, "hermes") / "install.py").exists())
            check = self.run_installer(home, "--check", "--agent", "hermes")
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertIn("hermes: ok", check.stdout)

    def test_clean_outdated_managed_copy_upgrades_without_accept_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            first = self.run_installer(home, "--agent", "hermes", "--allow-missing-agent")
            self.assertEqual(first.returncode, 0, first.stderr)
            target = self.target(home, "hermes")
            skill = target / "SKILL.md"
            skill.write_text(skill.read_text(encoding="utf-8").replace('version: "2.2.1"', 'version: "2.2.0"'), encoding="utf-8")
            marker = target / ".managed-skill.json"
            payload = json.loads(marker.read_text(encoding="utf-8"))
            payload["content_sha256"] = tree_hash(target)
            marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

            upgrade = self.run_installer(home, "--agent", "hermes")
            self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
            self.assertIn('version: "2.2.1"', skill.read_text(encoding="utf-8"))

    def test_modified_managed_copy_still_requires_accept_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            first = self.run_installer(home, "--agent", "hermes", "--allow-missing-agent")
            self.assertEqual(first.returncode, 0, first.stderr)
            target = self.target(home, "hermes")
            (target / "SKILL.md").write_text("locally modified\n", encoding="utf-8")

            upgrade = self.run_installer(home, "--agent", "hermes")
            self.assertEqual(upgrade.returncode, 1)
            self.assertIn("managed target has drift", upgrade.stderr)


if __name__ == "__main__":
    unittest.main()
