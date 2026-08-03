#!/usr/bin/env python3
"""Validate the portable github-repo-scout skill package."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "SKILL.md"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REF_RE = re.compile(r"(?:\]\(|`)((?:references|assets|scripts|maintenance)/[^)`\s]+)")
TOP_LEVEL_ALLOWED = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("frontmatter closing delimiter not found")
    raw = text[4:end]
    result: dict[str, str] = {}
    for number, line in enumerate(raw.splitlines(), start=2):
        if not line.strip() or line.startswith((" ", "\t")):
            continue
        if ":" not in line:
            raise ValueError(f"unsupported top-level YAML at line {number}: {line}")
        key, value = line.split(":", 1)
        key = key.strip()
        if key not in TOP_LEVEL_ALLOWED:
            raise ValueError(f"unsupported frontmatter key: {key}")
        if key in result:
            raise ValueError(f"duplicate frontmatter key: {key}")
        result[key] = value.strip().strip('"')
    return result, raw


def run(command: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False, timeout=timeout)


def validate_static() -> list[str]:
    errors: list[str] = []
    text = SKILL.read_text(encoding="utf-8")
    try:
        frontmatter, raw_frontmatter = parse_frontmatter(text)
    except ValueError as exc:
        errors.append(str(exc))
        frontmatter, raw_frontmatter = {}, ""

    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")
    compatibility = frontmatter.get("compatibility", "")
    if not NAME_RE.fullmatch(name) or len(name) > 64:
        errors.append(f"invalid name: {name!r}")
    if name != ROOT.name:
        errors.append(f"name {name!r} must match directory {ROOT.name!r}")
    if not 1 <= len(description) <= 1024:
        errors.append("description must contain 1-1024 characters")
    if compatibility and len(compatibility) > 500:
        errors.append("compatibility must not exceed 500 characters")
    if frontmatter.get("license") != "MIT" or not (ROOT / "LICENSE").is_file():
        errors.append("MIT license field and bundled LICENSE are required")
    if not (ROOT / "install.py").is_file():
        errors.append("root install.py entrypoint is required")
    if "python3 install.py" not in (ROOT / "README.md").read_text(encoding="utf-8"):
        errors.append("README must document the one-command installer")
    if "version: \"2.3.0\"" not in raw_frontmatter:
        errors.append("metadata.version must be 2.3.0")
    registry_path = ROOT / "maintenance" / "agents.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        ids = {agent["id"] for agent in registry.get("agents", [])}
        required = {"hermes", "codex", "claude", "cursor", "gemini", "copilot", "opencode", "windsurf"}
        if registry.get("schema_version") != 1 or not required.issubset(ids):
            errors.append("agent registry schema or required platform set is invalid")
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        errors.append(f"invalid agent registry: {exc}")
    if len(text.splitlines()) > 500:
        errors.append("SKILL.md exceeds 500 lines")

    for markdown in sorted(ROOT.rglob("*.md")):
        content = markdown.read_text(encoding="utf-8")
        for ref in sorted(set(REF_RE.findall(content))):
            if not (ROOT / ref).is_file():
                errors.append(f"{markdown.relative_to(ROOT)} references missing file: {ref}")

    for script in sorted(path for path in ROOT.rglob("*.py") if ".git" not in path.parts):
        try:
            compile(script.read_text(encoding="utf-8"), str(script), "exec")
        except SyntaxError as exc:
            errors.append(f"Python syntax error in {script.name}: {exc}")
    return errors


def validate_smoke() -> list[str]:
    errors: list[str] = []
    collector = ROOT / "scripts" / "github_repos.py"
    installer = ROOT / "maintenance" / "manage_skill.py"

    help_result = run([sys.executable, str(collector), "--help"])
    if help_result.returncode != 0:
        errors.append(f"collector --help failed: {help_result.stderr.strip()}")

    tests = run(
        [
            sys.executable,
            "-m",
            "unittest",
            str(ROOT / "maintenance" / "test_github_repos.py"),
            str(ROOT / "maintenance" / "test_installer.py"),
        ]
    )
    if tests.returncode != 0:
        errors.append(f"adaptive search tests failed: {(tests.stderr or tests.stdout).strip()}")

    doctor = run([sys.executable, str(collector), "doctor"])
    try:
        doctor_payload = json.loads(doctor.stdout)
    except json.JSONDecodeError:
        errors.append("collector doctor did not emit valid JSON")
    else:
        if doctor.returncode != 0 or not doctor_payload.get("ok"):
            errors.append(f"collector doctor failed: {doctor_payload.get('errors')}")

    with tempfile.TemporaryDirectory(prefix="github-repo-scout-smoke-") as temp:
        home = Path(temp) / "home"
        hermes_home = home / ".hermes"
        common = ["--home", str(home), "--hermes-home", str(hermes_home), "--platform", "all"]
        sequence = (
            ("plan", [sys.executable, str(installer), "plan", *common]),
            ("install", [sys.executable, str(installer), "install", *common]),
            ("check", [sys.executable, str(installer), "check", *common]),
            ("uninstall", [sys.executable, str(installer), "uninstall", *common]),
        )
        for label, command in sequence:
            result = run(command)
            if result.returncode != 0:
                errors.append(f"installer {label} failed: {(result.stderr or result.stdout).strip()}")
                break
        expected = (
            hermes_home / "skills" / "research" / ROOT.name,
            home / ".agents" / "skills" / ROOT.name,
            home / ".claude" / "skills" / ROOT.name,
        )
        if any(path.exists() for path in expected):
            errors.append("installer uninstall left deployed skill directories behind")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="run gh doctor and install/check/uninstall cycle")
    args = parser.parse_args()

    errors = validate_static()
    if args.smoke and not errors:
        errors.extend(validate_smoke())
    if errors:
        for item in errors:
            print(f"ERROR: {item}", file=sys.stderr)
        return 1
    mode = "static + smoke" if args.smoke else "static"
    print(f"OK: {ROOT.name}; {mode} validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
