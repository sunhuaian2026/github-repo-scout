#!/usr/bin/env python3
"""Safely deploy one canonical Agent Skill to Hermes, Codex, and Claude Code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SKILL_NAME = "github-repo-scout"
MARKER = ".managed-skill.json"
PLATFORMS = ("hermes", "codex", "claude")


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def targets(home: Path, hermes_home: Path | None = None) -> dict[str, Path]:
    resolved_hermes = hermes_home or Path(os.environ.get("HERMES_HOME", home / ".hermes"))
    return {
        "hermes": resolved_hermes.expanduser().resolve() / "skills" / "research" / SKILL_NAME,
        "codex": home / ".agents" / "skills" / SKILL_NAME,
        "claude": home / ".claude" / "skills" / SKILL_NAME,
    }


def included_files(root: Path) -> Iterator[tuple[Path, Path]]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if (
            rel.name == MARKER
            or rel.as_posix() == "README.md"
            or ".git" in rel.parts
            or rel.parts[0] == "maintenance"
            or "__pycache__" in rel.parts
            or rel.suffix == ".pyc"
            or any(part.startswith(f"{SKILL_NAME}.previous-") for part in rel.parts)
        ):
            continue
        yield path, rel


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path, rel in included_files(root):
        digest.update(rel.as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def read_marker(path: Path) -> dict[str, str] | None:
    marker = path / MARKER
    if not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def status(source: Path, target: Path) -> str:
    if not target.exists():
        return "missing"
    if not target.is_dir() or read_marker(target) is None:
        return "unmanaged"
    return "ok" if tree_hash(source) == tree_hash(target) else "drift"


@contextmanager
def directory_lock(parent: Path) -> Iterator[None]:
    parent.mkdir(parents=True, exist_ok=True)
    lock = parent / f".{SKILL_NAME}.lock"
    try:
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps({"pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat()}) + "\n",
            encoding="utf-8",
        )
    except FileExistsError as exc:
        raise RuntimeError(f"deployment lock already exists: {lock}") from exc
    try:
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def stage_copy(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}.stage-", dir=target.parent))
    try:
        shutil.copytree(
            source,
            staged,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git", "maintenance", "README.md", "__pycache__", "*.pyc", MARKER),
        )
        marker = {
            "schema_version": "1.2",
            "canonical_source": str(source),
            "content_sha256": tree_hash(source),
        }
        (staged / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        if tree_hash(staged) != tree_hash(source):
            raise RuntimeError(f"staged copy hash mismatch for {target}")
        return staged
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise


def unique_sibling(target: Path, label: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return target.with_name(f"{target.name}.{label}-{os.getpid()}-{stamp}")


def install_all(source: Path, selected: list[tuple[str, Path]], accept_drift: bool) -> None:
    preflight_errors: list[str] = []
    for platform, target in selected:
        current = status(source, target)
        if current == "unmanaged":
            preflight_errors.append(f"{platform}: refusing unmanaged target {target}")
        elif current == "drift" and not accept_drift:
            preflight_errors.append(
                f"{platform}: managed target has drift; inspect it or pass --accept-drift: {target}"
            )
    if preflight_errors:
        raise RuntimeError("\n".join(preflight_errors))

    staged: dict[str, Path] = {}
    backups: dict[str, Path | None] = {}
    committed: list[tuple[str, Path]] = []
    try:
        for platform, target in selected:
            staged[platform] = stage_copy(source, target)

        for platform, target in selected:
            backup: Path | None = None
            if target.exists():
                backup = unique_sibling(target, "previous")
                target.replace(backup)
            backups[platform] = backup
            try:
                staged[platform].replace(target)
                if status(source, target) != "ok":
                    raise RuntimeError(f"post-install hash verification failed: {target}")
                committed.append((platform, target))
            except Exception:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                if backup and backup.exists():
                    backup.replace(target)
                raise
    except Exception:
        for platform, target in reversed(committed):
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            backup = backups.get(platform)
            if backup and backup.exists():
                backup.replace(target)
        raise
    finally:
        for staged_path in staged.values():
            shutil.rmtree(staged_path, ignore_errors=True)

    for backup in backups.values():
        if backup and backup.exists():
            shutil.rmtree(backup)


def uninstall_all(source: Path, selected: list[tuple[str, Path]], accept_drift: bool) -> None:
    errors: list[str] = []
    for platform, target in selected:
        current = status(source, target)
        if current == "unmanaged":
            errors.append(f"{platform}: refusing unmanaged target {target}")
        elif current == "drift" and not accept_drift:
            errors.append(
                f"{platform}: managed target has drift; inspect it or pass --accept-drift: {target}"
            )
    if errors:
        raise RuntimeError("\n".join(errors))

    moved: list[tuple[Path, Path]] = []
    try:
        for _, target in selected:
            if not target.exists():
                continue
            trash = unique_sibling(target, "uninstalling")
            target.replace(trash)
            moved.append((target, trash))
    except Exception:
        for target, trash in reversed(moved):
            if trash.exists() and not target.exists():
                trash.replace(target)
        raise

    for _, trash in moved:
        shutil.rmtree(trash)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--platform", choices=("all",) + PLATFORMS, default="all")
    common.add_argument("--home", type=Path, default=Path.home(), help="override user home")
    common.add_argument("--hermes-home", type=Path, help="override HERMES_HOME")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", parents=[common], help="show targets and conflicts without writing")
    install = sub.add_parser("install", parents=[common], help="install managed copies transactionally")
    install.add_argument("--accept-drift", action="store_true", help="replace modified managed copies")
    sub.add_parser("check", parents=[common], help="verify deployed copies")
    uninstall = sub.add_parser("uninstall", parents=[common], help="remove only managed copies")
    uninstall.add_argument("--accept-drift", action="store_true", help="remove a modified managed copy")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source = skill_root()
    home = args.home.expanduser().resolve()
    hermes_home = args.hermes_home.expanduser().resolve() if args.hermes_home else None
    mapping = targets(home, hermes_home)
    names = PLATFORMS if args.platform == "all" else (args.platform,)
    selected = [(name, mapping[name]) for name in names]

    if args.command == "plan":
        for platform, target in selected:
            print(f"{platform}: {status(source, target)} -> {target}")
        return 0

    if args.command == "check":
        ok = True
        for platform, target in selected:
            current = status(source, target)
            print(f"{platform}: {current} ({target})")
            ok = ok and current == "ok"
        return 0 if ok else 1

    try:
        with ExitStack() as stack:
            for parent in sorted({target.parent for _, target in selected}, key=str):
                stack.enter_context(directory_lock(parent))
            if args.command == "install":
                install_all(source, selected, args.accept_drift)
                action = "installed"
            else:
                uninstall_all(source, selected, args.accept_drift)
                action = "uninstalled"
        for platform, target in selected:
            print(f"{platform}: {action} {target}")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
