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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = PROJECT_ROOT / "skills" / "github-repo-scout"
SKILL = SKILL_ROOT / "SKILL.md"
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
    if name != SKILL_ROOT.name:
        errors.append(f"name {name!r} must match directory {SKILL_ROOT.name!r}")
    if not 1 <= len(description) <= 1024:
        errors.append("description must contain 1-1024 characters")
    if compatibility and len(compatibility) > 500:
        errors.append("compatibility must not exceed 500 characters")
    package_license = SKILL_ROOT / "LICENSE"
    project_license = PROJECT_ROOT / "LICENSE"
    if frontmatter.get("license") != "MIT" or not package_license.is_file():
        errors.append("MIT license field and bundled Skill LICENSE are required")
    elif not project_license.is_file() or package_license.read_bytes() != project_license.read_bytes():
        errors.append("project and bundled Skill LICENSE files must match")
    if (PROJECT_ROOT / "SKILL.md").exists():
        errors.append("root SKILL.md would make npx copy maintainer files; use skills/github-repo-scout only")
    if any((SKILL_ROOT / name).exists() for name in ("README.md", "install.py", "maintenance")):
        errors.append("runtime Skill package contains maintainer-only files")
    if not (PROJECT_ROOT / "install.py").is_file():
        errors.append("root install.py entrypoint is required")
    if "python3 install.py" not in (PROJECT_ROOT / "README.md").read_text(encoding="utf-8"):
        errors.append("README must document the one-command installer")
    if "version: \"2.3.2\"" not in raw_frontmatter:
        errors.append("metadata.version must be 2.3.2")
    if "## 不可信内容边界" not in text or "第三方不可信数据" not in text:
        errors.append("SKILL.md must define the third-party untrusted-content boundary")
    if "one_time_trial" not in text or "不能用试用额度替代长期免费" not in text:
        errors.append("SKILL.md must distinguish sustainable free use from one-time trials")
    if "activity_summary" not in text or "不能单独证明核心代码仍在维护" not in text:
        errors.append("SKILL.md must distinguish code maintenance from documentation-only activity")
    if "相同硬门槛和同一证据集必须得到相同 Gate" not in text:
        errors.append("SKILL.md must require recommendation stability checks")
    registry_path = PROJECT_ROOT / "maintenance" / "agents.json"
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

    for markdown in sorted(SKILL_ROOT.rglob("*.md")):
        content = markdown.read_text(encoding="utf-8")
        for ref in sorted(set(REF_RE.findall(content))):
            if not (SKILL_ROOT / ref).is_file():
                errors.append(f"{markdown.relative_to(SKILL_ROOT)} references missing file: {ref}")

    for script in sorted(path for path in PROJECT_ROOT.rglob("*.py") if ".git" not in path.parts):
        try:
            compile(script.read_text(encoding="utf-8"), str(script), "exec")
        except SyntaxError as exc:
            errors.append(f"Python syntax error in {script.name}: {exc}")
    return errors


def validate_smoke() -> list[str]:
    errors: list[str] = []
    collector = SKILL_ROOT / "scripts" / "github_repos.py"
    installer = PROJECT_ROOT / "maintenance" / "manage_skill.py"

    help_result = run([sys.executable, str(collector), "--help"])
    if help_result.returncode != 0:
        errors.append(f"collector --help failed: {help_result.stderr.strip()}")

    tests = run(
        [
            sys.executable,
            "-m",
            "unittest",
            str(PROJECT_ROOT / "maintenance" / "test_github_repos.py"),
            str(PROJECT_ROOT / "maintenance" / "test_installer.py"),
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
            hermes_home / "skills" / "research" / SKILL_ROOT.name,
            home / ".agents" / "skills" / SKILL_ROOT.name,
            home / ".claude" / "skills" / SKILL_ROOT.name,
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
    print(f"OK: {SKILL_ROOT.name}; {mode} validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
