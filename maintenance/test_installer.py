#!/usr/bin/env python3
"""Public CLI tests for the one-command installer."""

from __future__ import annotations

import json
import os
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
                "--no-system-detection",
                *args,
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def target(self, home: Path, agent: str) -> Path:
        roots = {
            "hermes": home / ".hermes" / "skills" / "research",
            "codex": home / ".agents" / "skills",
            "claude": home / ".claude" / "skills",
            "cursor": home / ".cursor" / "skills",
            "gemini": home / ".agents" / "skills",
            "copilot": home / ".agents" / "skills",
            "opencode": home / ".agents" / "skills",
            "windsurf": home / ".codeium" / "windsurf" / "skills",
        }
        return roots[agent] / "github-repo-scout"

    def test_list_agents_comes_from_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_installer(Path(temp), "--list-agents")
            self.assertEqual(result.returncode, 0, result.stderr)
            for agent in ("hermes", "codex", "claude", "cursor", "gemini", "copilot", "opencode", "windsurf"):
                self.assertIn(agent, result.stdout)

    def test_cursor_is_detected_from_its_home_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".cursor").mkdir()
            result = self.run_installer(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.target(home, "cursor").is_dir())
            self.assertFalse(self.target(home, "claude").exists())

    def test_agent_alias_resolves_through_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            result = self.run_installer(home, "--agent", "gemini-cli", "--allow-missing-agent")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.target(home, "gemini").is_dir())

    def test_every_registered_agent_supports_predeploy_and_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            for agent in ("hermes", "codex", "claude", "cursor", "gemini", "copilot", "opencode", "windsurf"):
                with self.subTest(agent=agent):
                    install = self.run_installer(home, "--agent", agent, "--allow-missing-agent")
                    self.assertEqual(install.returncode, 0, install.stderr)
                    self.assertTrue(self.target(home, agent).is_dir())
                    check = self.run_installer(home, "--check", "--agent", agent)
                    self.assertEqual(check.returncode, 0, check.stderr)

    def test_custom_target_supports_install_check_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            root = home / "custom-agent" / "skills"
            install = self.run_installer(home, "--target", str(root))
            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertTrue((root / "github-repo-scout" / "SKILL.md").is_file())
            check = self.run_installer(home, "--check", "--target", str(root))
            self.assertEqual(check.returncode, 0, check.stderr)
            uninstall = self.run_installer(home, "--uninstall", "--target", str(root))
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertFalse((root / "github-repo-scout").exists())

    def test_target_only_uninstall_preserves_known_agent_copies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            known = self.run_installer(home, "--agent", "hermes", "--allow-missing-agent")
            self.assertEqual(known.returncode, 0, known.stderr)
            root = home / "custom-agent" / "skills"
            custom = self.run_installer(home, "--target", str(root))
            self.assertEqual(custom.returncode, 0, custom.stderr)

            uninstall = self.run_installer(home, "--uninstall", "--target", str(root))
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertTrue(self.target(home, "hermes").is_dir())
            self.assertFalse((root / "github-repo-scout").exists())

    def test_unknown_agent_points_to_custom_target_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            result = self.run_installer(home, "--agent", "future-agent")
            self.assertEqual(result.returncode, 2)
            self.assertIn("--target", result.stderr)
            self.assertEqual(list(home.iterdir()), [])

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

    def test_agents_alias_root_is_shared_without_duplicate_native_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".codex").mkdir()
            (home / ".gemini").mkdir()
            result = self.run_installer(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("codex+gemini", result.stdout)
            self.assertIn("copilot: skipped", result.stdout)
            self.assertIn("opencode: skipped", result.stdout)
            self.assertTrue(self.target(home, "codex").is_dir())
            self.assertFalse((home / ".gemini" / "skills" / "github-repo-scout").exists())

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
            target = self.target(home, "hermes")
            self.assertTrue(target.is_dir())
            self.assertFalse((target / "install.py").exists())
            self.assertFalse((target / "README.md").exists())
            self.assertFalse((target / "maintenance").exists())
            self.assertTrue((target / "scripts" / "github_repos.py").is_file())
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
            skill.write_text(skill.read_text(encoding="utf-8").replace('version: "2.3.5"', 'version: "2.2.1"'), encoding="utf-8")
            marker = target / ".managed-skill.json"
            payload = json.loads(marker.read_text(encoding="utf-8"))
            payload["content_sha256"] = tree_hash(target)
            marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

            upgrade = self.run_installer(home, "--agent", "hermes")
            self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
            self.assertIn('version: "2.3.5"', skill.read_text(encoding="utf-8"))

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
