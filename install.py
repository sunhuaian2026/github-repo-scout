#!/usr/bin/env python3
"""Agent-aware installer for GitHub Repo Scout."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from maintenance.manage_skill import (
    SKILL_NAME,
    directory_lock,
    install_all,
    status,
    uninstall_all,
)

PROJECT_ROOT = Path(__file__).resolve().parent
SKILL_SOURCE = PROJECT_ROOT / "skills" / SKILL_NAME
REGISTRY_PATH = PROJECT_ROOT / "maintenance" / "agents.json"


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    aliases: tuple[str, ...]
    skill_root: str
    commands: tuple[str, ...]
    detect_paths: tuple[str, ...]
    documentation: str


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Agent]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("agents"), list):
        raise ValueError("unsupported agent registry schema")

    agents: dict[str, Agent] = {}
    names: set[str] = {"all"}
    for raw in payload["agents"]:
        detect = raw.get("detect", {})
        agent = Agent(
            id=raw["id"],
            name=raw["name"],
            aliases=tuple(raw.get("aliases", [])),
            skill_root=raw["skill_root"],
            commands=tuple(detect.get("commands", [])),
            detect_paths=tuple(detect.get("paths", [])),
            documentation=raw["documentation"],
        )
        tokens = (agent.id,) + agent.aliases
        if not agent.id or any(not token or token in names for token in tokens):
            raise ValueError(f"duplicate or invalid agent id/alias: {agent.id}")
        names.update(tokens)
        agents[agent.id] = agent
    if not agents:
        raise ValueError("agent registry is empty")
    return agents


def render_path(template: str, home: Path, hermes_home: Path) -> Path:
    return Path(template.format(home=str(home), hermes_home=str(hermes_home))).expanduser().resolve()


def known_targets(agents: dict[str, Agent], home: Path, hermes_home: Path) -> dict[str, Path]:
    return {
        agent_id: render_path(agent.skill_root, home, hermes_home) / SKILL_NAME
        for agent_id, agent in agents.items()
    }


def detected_agents(
    agents: dict[str, Agent],
    home: Path,
    hermes_home: Path,
    system_detection: bool = True,
) -> dict[str, bool]:
    detected: dict[str, bool] = {}
    for agent_id, agent in agents.items():
        command_found = any(shutil.which(command) is not None for command in agent.commands)
        path_found = False
        for template in agent.detect_paths:
            if not system_detection and template.startswith("/"):
                continue
            if render_path(template, home, hermes_home).exists():
                path_found = True
                break
        detected[agent_id] = command_found or path_found
    return detected


def alias_map(agents: dict[str, Agent]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for agent_id, agent in agents.items():
        aliases[agent_id] = agent_id
        for alias in agent.aliases:
            aliases[alias] = agent_id
    return aliases


def resolve_requested(values: Optional[list[str]], agents: dict[str, Agent]) -> tuple[list[str], bool]:
    requested = values or ["all"]
    if "all" in requested:
        if len(requested) != 1:
            raise ValueError("--agent all cannot be combined with another --agent")
        return list(agents), False

    aliases = alias_map(agents)
    resolved: list[str] = []
    for value in requested:
        if value not in aliases:
            raise ValueError(f"unknown agent '{value}'; use --list-agents or --target PATH")
        agent_id = aliases[value]
        if agent_id not in resolved:
            resolved.append(agent_id)
    return resolved, True


def custom_targets(values: Iterable[Path]) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for raw in values:
        root = raw.expanduser().resolve()
        target = root if root.name == SKILL_NAME else root / SKILL_NAME
        if target in seen:
            continue
        seen.add(target)
        result.append((f"custom:{root}", target))
    return result


def deduplicate_targets(items: Iterable[tuple[str, Path]]) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    positions: dict[Path, int] = {}
    for label, target in items:
        resolved = target.resolve()
        if resolved in positions:
            index = positions[resolved]
            existing, _ = result[index]
            result[index] = (f"{existing}+{label}", resolved)
            continue
        positions[resolved] = len(result)
        result.append((label, resolved))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="verify installed copies")
    action.add_argument("--uninstall", action="store_true", help="remove managed copies")
    parser.add_argument("--agent", action="append", metavar="NAME", help="known agent id or alias; repeatable")
    parser.add_argument(
        "--target",
        action="append",
        type=Path,
        default=[],
        metavar="SKILLS_DIR",
        help="skills root for any other Agent Skills-compatible client; repeatable",
    )
    parser.add_argument("--list-agents", action="store_true", help="list known agents, detection state, and target paths")
    parser.add_argument("--allow-missing-agent", action="store_true", help="predeploy an explicitly named agent")
    parser.add_argument("--accept-drift", action="store_true", help="replace or remove a modified managed copy")
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    parser.add_argument("--hermes-home", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--no-system-detection", action="store_true", help=argparse.SUPPRESS)
    return parser


def print_skipped(requested: Iterable[str], detected: dict[str, bool]) -> None:
    for agent_id in requested:
        if not detected[agent_id]:
            print(f"{agent_id}: skipped (agent not detected)")


def main() -> int:
    args = build_parser().parse_args()
    home = args.home.expanduser().resolve()
    hermes_home = args.hermes_home.expanduser().resolve() if args.hermes_home else home / ".hermes"
    try:
        agents = load_registry()
        requested, explicit = resolve_requested(args.agent, agents)
        if args.agent is None and args.target:
            requested, explicit = [], True
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    mapping = known_targets(agents, home, hermes_home)
    detected = detected_agents(agents, home, hermes_home, not args.no_system_detection)
    customs = custom_targets(args.target)

    if args.list_agents:
        for agent_id, agent in agents.items():
            state = "detected" if detected[agent_id] else "not detected"
            aliases = f"; aliases: {', '.join(agent.aliases)}" if agent.aliases else ""
            print(f"{agent_id}: {state}; target: {mapping[agent_id]}{aliases}")
        return 0

    if args.check:
        known_names = requested if explicit else [agent_id for agent_id in requested if detected[agent_id]]
        if not explicit:
            print_skipped(requested, detected)
        selected = deduplicate_targets([(agent_id, mapping[agent_id]) for agent_id in known_names] + customs)
        if not selected:
            print("ERROR: no supported agents detected and no --target supplied", file=sys.stderr)
            return 2
        ok = True
        for label, target in selected:
            current = status(SKILL_SOURCE, target)
            print(f"{label}: {current} ({target})")
            ok = ok and current == "ok"
        return 0 if ok else 1

    if args.uninstall:
        candidates = deduplicate_targets([(agent_id, mapping[agent_id]) for agent_id in requested] + customs)
        selected = [(label, target) for label, target in candidates if target.exists()]
        for label, target in candidates:
            if not target.exists():
                print(f"{label}: missing ({target})")
        if not selected:
            return 0
        try:
            with ExitStack() as stack:
                for parent in sorted({target.parent for _, target in selected}, key=str):
                    stack.enter_context(directory_lock(parent))
                uninstall_all(SKILL_SOURCE, selected, args.accept_drift)
            for label, target in selected:
                print(f"{label}: uninstalled {target}")
            return 0
        except (OSError, RuntimeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if explicit:
        missing = [agent_id for agent_id in requested if not detected[agent_id]]
        if missing and not args.allow_missing_agent:
            joined = ", ".join(missing)
            print(
                f"ERROR: {joined} is not detected; install it first, pass --allow-missing-agent, or use --target PATH",
                file=sys.stderr,
            )
            return 2
        known_names = requested
    else:
        known_names = [agent_id for agent_id in requested if detected[agent_id]]
        print_skipped(requested, detected)

    selected = deduplicate_targets([(agent_id, mapping[agent_id]) for agent_id in known_names] + customs)
    if not selected:
        print(
            "ERROR: no supported agents detected; use --agent NAME --allow-missing-agent or --target PATH",
            file=sys.stderr,
        )
        return 2

    try:
        with ExitStack() as stack:
            for parent in sorted({target.parent for _, target in selected}, key=str):
                stack.enter_context(directory_lock(parent))
            install_all(SKILL_SOURCE, selected, args.accept_drift)
        for label, target in selected:
            print(f"{label}: installed {target}")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
