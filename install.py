#!/usr/bin/env python3
"""One-command installer for GitHub Repo Scout."""

from __future__ import annotations

import argparse
import shutil
import sys
from contextlib import ExitStack
from pathlib import Path

from maintenance.manage_skill import (
    PLATFORMS,
    directory_lock,
    install_all,
    status,
    targets,
    uninstall_all,
)

ROOT = Path(__file__).resolve().parent


def detected_agents(home: Path, hermes_home: Path | None) -> dict[str, bool]:
    resolved_hermes = hermes_home or home / ".hermes"
    return {
        "hermes": shutil.which("hermes") is not None or resolved_hermes.exists(),
        "codex": shutil.which("codex") is not None or (home / ".codex").exists(),
        "claude": shutil.which("claude") is not None or (home / ".claude").exists(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="verify installed copies")
    action.add_argument("--uninstall", action="store_true", help="remove managed copies")
    parser.add_argument("--agent", choices=("all",) + PLATFORMS, default="all")
    parser.add_argument("--allow-missing-agent", action="store_true", help="predeploy even when the agent is not detected")
    parser.add_argument("--accept-drift", action="store_true", help="replace or remove a modified managed copy")
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    parser.add_argument("--hermes-home", type=Path, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    home = args.home.expanduser().resolve()
    hermes_home = args.hermes_home.expanduser().resolve() if args.hermes_home else None
    mapping = targets(home, hermes_home)
    requested = PLATFORMS if args.agent == "all" else (args.agent,)
    detected = detected_agents(home, hermes_home)

    if args.check:
        check_names = list(requested) if args.agent != "all" else [agent for agent in requested if detected[agent]]
        if args.agent == "all":
            for agent in requested:
                if not detected[agent]:
                    print(f"{agent}: skipped (agent not detected)")
        if not check_names:
            print("ERROR: no supported agents detected", file=sys.stderr)
            return 2
        ok = True
        for agent in check_names:
            current = status(ROOT, mapping[agent])
            print(f"{agent}: {current} ({mapping[agent]})")
            ok = ok and current == "ok"
        return 0 if ok else 1

    if args.uninstall:
        selected = [(agent, mapping[agent]) for agent in requested if mapping[agent].exists()]
        for agent in requested:
            if not mapping[agent].exists():
                print(f"{agent}: missing ({mapping[agent]})")
        if not selected:
            return 0
        try:
            with ExitStack() as stack:
                for parent in sorted({target.parent for _, target in selected}, key=str):
                    stack.enter_context(directory_lock(parent))
                uninstall_all(ROOT, selected, args.accept_drift)
            for agent, target in selected:
                print(f"{agent}: uninstalled {target}")
            return 0
        except (OSError, RuntimeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if args.allow_missing_agent:
        selected_names = list(requested)
    elif args.agent == "all":
        selected_names = [agent for agent in requested if detected[agent]]
        for agent in requested:
            if not detected[agent]:
                print(f"{agent}: skipped (agent not detected)")
        if not selected_names:
            print("ERROR: no supported agents detected; use --agent NAME --allow-missing-agent to predeploy", file=sys.stderr)
            return 2
    elif not detected[args.agent]:
        print(
            f"ERROR: {args.agent} is not detected; install it first or pass --allow-missing-agent",
            file=sys.stderr,
        )
        return 2
    else:
        selected_names = [args.agent]

    selected = [(agent, mapping[agent]) for agent in selected_names]
    try:
        with ExitStack() as stack:
            for parent in sorted({target.parent for _, target in selected}, key=str):
                stack.enter_context(directory_lock(parent))
            install_all(ROOT, selected, args.accept_drift)
        for agent, target in selected:
            print(f"{agent}: installed {target}")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
