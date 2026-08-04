#!/usr/bin/env python3
"""Deterministic GitHub evidence collector for github-repo-scout."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SEARCH_FIELDS = (
    "fullName,url,description,stargazersCount,forksCount,openIssuesCount,"
    "language,license,isArchived,isDisabled,isFork,isPrivate,pushedAt,updatedAt"
)
MANIFESTS = {
    "package.json", "pyproject.toml", "requirements.txt", "setup.py", "Cargo.toml",
    "go.mod", "Gemfile", "pom.xml", "build.gradle", "Dockerfile",
    "docker-compose.yml", "compose.yml", "Makefile", "install.sh",
}
DEFAULT_TIMEOUT = 120
NOISE_TERMS = (
    "awesome", "curated list", "collection of", "resources for", "tutorial",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def error(source: str, kind: str, message: str) -> dict[str, str]:
    return {"source": source, "kind": kind, "message": message}


def run(command: list[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, text=True, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", f"command timed out after {timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(command, 126, "", str(exc))


def emit(payload: dict[str, Any], output: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output:
        path = Path(output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def base_payload() -> dict[str, Any]:
    return {"schema_version": "2.2", "generated_at": now_iso()}


def require_gh() -> str:
    path = shutil.which("gh")
    if not path:
        raise RuntimeError("GitHub CLI 'gh' is not installed or not in PATH")
    return path


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        gh = require_gh()
    except RuntimeError as exc:
        payload = {**base_payload(), "ok": False, "partial": True, "errors": [error("gh", "missing", str(exc))]}
        emit(payload, args.output)
        return 1

    version = run([gh, "--version"], args.timeout)
    auth = run([gh, "auth", "status"], args.timeout)
    errors: list[dict[str, str]] = []
    if version.returncode != 0:
        errors.append(error("gh_version", "command_failed", version.stderr.strip() or "unknown error"))
    if auth.returncode != 0:
        errors.append(error("gh_auth", "not_authenticated", "gh auth status failed"))
    payload = {
        **base_payload(),
        "ok": not errors,
        "partial": bool(errors),
        "errors": errors,
        "gh_path": gh,
        "version": version.stdout.splitlines()[0] if version.stdout else "unknown",
        "auth_ok": auth.returncode == 0,
    }
    emit(payload, args.output)
    return 0 if not errors else 1


def round_robin(rows_by_query: list[list[dict[str, Any]]], maximum: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    rank = 0
    while len(selected) < maximum:
        added = False
        for rows in rows_by_query:
            if rank >= len(rows):
                continue
            row = rows[rank]
            name = str(row.get("fullName") or "").lower()
            if name and name not in seen:
                selected.append(row)
                seen.add(name)
                added = True
                if len(selected) >= maximum:
                    break
        rank += 1
        if not added and all(rank >= len(rows) for rows in rows_by_query):
            break
    return selected


def norm_text(row: dict[str, Any]) -> str:
    return f"{row.get('fullName') or ''} {row.get('description') or ''}".lower()


def contains_any(text: str, terms: list[str]) -> bool:
    return any(term.lower() in text for term in terms)


def license_key(row: dict[str, Any]) -> str:
    value = row.get("license")
    if isinstance(value, dict):
        return str(value.get("key") or "")
    return str(value or "")


def metadata_gate_reason(row: dict[str, Any]) -> str | None:
    if row.get("isArchived"):
        return "archived"
    if row.get("isDisabled"):
        return "disabled"
    if row.get("isPrivate"):
        return "private"
    if row.get("isFork"):
        return "fork"
    if not license_key(row):
        return "missing_license"
    if any(term in norm_text(row) for term in NOISE_TERMS):
        return "obvious_noise"
    return None


def assess_expansion(
    rows: list[dict[str, Any]],
    prior_names: set[str],
    task: dict[str, Any],
    top_n: int = 10,
) -> dict[str, Any]:
    prior = {name.lower() for name in prior_names}
    novel_plausible = 0
    overlap = 0
    constraint_supported = 0
    for row in rows[:top_n]:
        name = str(row.get("fullName") or "").lower()
        if not name or metadata_gate_reason(row):
            continue
        text = norm_text(row)
        is_overlap = name in prior
        plausible = contains_any(text, task["relevance_terms"]) or is_overlap
        if is_overlap:
            overlap += 1
        if plausible and not is_overlap:
            novel_plausible += 1
        if plausible and contains_any(text, task["constraint_terms"]):
            constraint_supported += 1
    accepted = novel_plausible >= 2 or overlap >= 2
    gap_closed = accepted and (
        constraint_supported >= 3 or novel_plausible >= 3 or overlap >= 3
    )
    return {
        "accepted": accepted,
        "gap_closed": gap_closed,
        "novel_plausible": novel_plausible,
        "overlap": overlap,
        "constraint_supported": constraint_supported,
        "top_n": min(top_n, len(rows)),
    }


def rank_candidates(
    candidates: list[dict[str, Any]],
    task: dict[str, Any],
    maximum: int,
) -> dict[str, Any]:
    ranked: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for row in candidates:
        reason = metadata_gate_reason(row)
        if reason:
            excluded.append({"fullName": str(row.get("fullName") or ""), "reason": reason})
            continue
        score = 0.0
        roles: set[str] = set()
        for match in row.get("matches") or []:
            role = str(match.get("role") or "")
            roles.add(role)
            weight = 0.55 if match.get("phase") == "expansion" else 1.0
            rank = max(1, int(match.get("rank") or 1))
            score += weight / (5 + rank)
        text = norm_text(row)
        if contains_any(text, task["relevance_terms"]):
            score += 0.2
        if contains_any(text, task["constraint_terms"]):
            score += 0.08
        if len(roles) > 1:
            score += 0.1 * (len(roles) - 1)
        copied = dict(row)
        copied["selectionScore"] = round(score, 6)
        ranked.append(copied)
    ranked.sort(key=lambda item: (-item["selectionScore"], str(item.get("fullName") or "").lower()))
    return {"selected": ranked[:maximum], "excluded": excluded}


def load_query_plan(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("query plan must be a JSON object")
    for field in ("relevance_terms", "constraint_terms", "queries"):
        if not isinstance(data.get(field), list):
            raise ValueError(f"query plan field {field!r} must be a list")
    if not 2 <= len(data["queries"]) <= 7:
        raise ValueError("query plan must contain 2-7 queries")
    base_count = 0
    expansion_count = 0
    expansion_started = False
    for index, item in enumerate(data["queries"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"query plan item {index} must be an object")
        role = item.get("role")
        phase = item.get("phase")
        query = item.get("query")
        if not all(isinstance(value, str) and value.strip() for value in (role, phase, query)):
            raise ValueError(f"query plan item {index} requires non-empty role, phase and query")
        if phase not in ("base", "expansion"):
            raise ValueError(f"query plan item {index} has invalid phase: {phase}")
        if phase == "base":
            if expansion_started:
                raise ValueError("base queries must precede expansion queries")
            base_count += 1
        else:
            expansion_started = True
            expansion_count += 1
    if not 2 <= base_count <= 3 or not 0 <= expansion_count <= 2:
        raise ValueError("query plan requires 2-3 base queries and 0-2 expansion queries")
    for field in ("relevance_terms", "constraint_terms"):
        if not all(isinstance(term, str) and term.strip() for term in data[field]):
            raise ValueError(f"query plan field {field!r} must contain non-empty strings")
    if not data["relevance_terms"]:
        raise ValueError("query plan requires at least one relevance term")
    return data


def search_rows(
    gh: str, query: str, limit: int, timeout: int,
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    result = run([
        gh, "search", "repos", query,
        "--limit", str(limit), "--json", SEARCH_FIELDS,
    ], timeout)
    if result.returncode != 0:
        return [], error(query, "command_failed", result.stderr.strip() or "unknown gh error")
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return [], error(query, "invalid_json", str(exc))
    if not isinstance(rows, list):
        return [], error(query, "invalid_shape", "gh search output is not a list")
    valid = [row for row in rows if isinstance(row, dict) and row.get("fullName")]
    return valid, None


def merge_rows(
    merged: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
    role: str,
    phase: str,
    query: str,
) -> None:
    for rank, row in enumerate(rows, start=1):
        name = str(row["fullName"]).lower()
        if name not in merged:
            merged[name] = {**row, "matches": []}
        merged[name]["matches"].append({
            "role": role, "phase": phase, "query": query, "rank": rank,
        })


def cmd_adaptive_search(args: argparse.Namespace) -> int:
    gh = require_gh()
    plan = load_query_plan(args.plan)
    merged: dict[str, dict[str, Any]] = {}
    decisions: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    raw_hits = 0
    stopped = False
    for item in plan["queries"]:
        role = item["role"]
        phase = item["phase"]
        query = item["query"]
        if phase == "expansion" and stopped:
            decisions.append({"role": role, "phase": phase, "query": query, "status": "skipped_gap_closed"})
            continue
        rows, failure = search_rows(gh, query, args.limit_per_query, args.timeout)
        raw_hits += len(rows)
        if failure:
            errors.append(failure)
            decisions.append({"role": role, "phase": phase, "query": query, "status": "error"})
            continue
        if phase == "base":
            merge_rows(merged, rows, role, phase, query)
            decisions.append({"role": role, "phase": phase, "query": query, "status": "accepted"})
            continue
        decision = assess_expansion(rows, set(merged), plan)
        decision.update({"role": role, "phase": phase, "query": query})
        decision["status"] = "accepted" if decision["accepted"] else "rejected_low_yield"
        decisions.append(decision)
        if decision["accepted"]:
            merge_rows(merged, rows, role, phase, query)
        if decision["gap_closed"]:
            stopped = True
    ranked = rank_candidates(list(merged.values()), plan, args.max_candidates)
    payload = {
        **base_payload(),
        "partial": bool(errors),
        "plan": plan,
        "query_decisions": decisions,
        "selection": {
            "strategy": "adaptive_quality_weighted_v2.2",
            "truncated": len(ranked["selected"]) < len(merged) - len(ranked["excluded"]),
        },
        "counts": {
            "planned_queries": len(plan["queries"]),
            "executed_queries": sum(1 for item in decisions if item.get("status") != "skipped_gap_closed"),
            "raw_hits": raw_hits,
            "active_deduplicated": len(merged),
            "excluded": len(ranked["excluded"]),
            "returned": len(ranked["selected"]),
        },
        "errors": errors,
        "excluded": ranked["excluded"],
        "candidates": ranked["selected"],
    }
    emit(payload, args.output)
    return 2 if errors else 0


def cmd_search(args: argparse.Namespace) -> int:
    gh = require_gh()
    errors: list[dict[str, str]] = []
    rows_by_query: list[list[dict[str, Any]]] = []
    merged: dict[str, dict[str, Any]] = {}
    raw_hits = 0

    for query in args.query:
        result = run([
            gh, "search", "repos", query,
            "--limit", str(args.limit_per_query),
            "--json", SEARCH_FIELDS,
        ], args.timeout)
        if result.returncode != 0:
            errors.append(error(query, "command_failed", result.stderr.strip() or "unknown gh error"))
            rows_by_query.append([])
            continue
        try:
            rows = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            errors.append(error(query, "invalid_json", str(exc)))
            rows_by_query.append([])
            continue
        if not isinstance(rows, list):
            errors.append(error(query, "invalid_shape", "gh search output is not a list"))
            rows_by_query.append([])
            continue

        valid_rows: list[dict[str, Any]] = []
        raw_hits += len(rows)
        for row in rows:
            if not isinstance(row, dict) or not row.get("fullName"):
                errors.append(error(query, "missing_field", "candidate missing fullName"))
                continue
            name = str(row["fullName"]).lower()
            valid_rows.append(row)
            if name not in merged:
                copied = dict(row)
                copied["matchedQueries"] = []
                merged[name] = copied
            if query not in merged[name]["matchedQueries"]:
                merged[name]["matchedQueries"].append(query)
        rows_by_query.append(valid_rows)

    selected_rows = round_robin(rows_by_query, args.max_candidates)
    candidates = [merged[str(row["fullName"]).lower()] for row in selected_rows]
    truncated = len(merged) > len(candidates)
    payload = {
        **base_payload(),
        "partial": bool(errors),
        "queries": args.query,
        "selection": {
            "strategy": "round_robin_query_rank",
            "truncated": truncated,
            "dropped_after_deduplication": max(0, len(merged) - len(candidates)),
        },
        "counts": {
            "raw_hits": raw_hits,
            "deduplicated": len(merged),
            "returned": len(candidates),
        },
        "errors": errors,
        "candidates": candidates,
    }
    emit(payload, args.output)
    return 2 if errors else 0


def gh_api_json(gh: str, endpoint: str, timeout: int) -> tuple[Any | None, str | None, str | None]:
    result = run([gh, "api", endpoint], timeout)
    if result.returncode != 0:
        kind = "not_found" if "HTTP 404" in result.stderr else "command_failed"
        return None, kind, result.stderr.strip() or f"gh api failed for {endpoint}"
    try:
        return json.loads(result.stdout), None, None
    except json.JSONDecodeError as exc:
        return None, "invalid_json", f"invalid JSON from {endpoint}: {exc}"


def gh_api_raw(gh: str, endpoint: str, timeout: int) -> tuple[str | None, str | None, str | None]:
    result = run([gh, "api", endpoint, "-H", "Accept: application/vnd.github.raw+json"], timeout)
    if result.returncode != 0:
        kind = "not_found" if "HTTP 404" in result.stderr else "command_failed"
        return None, kind, result.stderr.strip() or f"gh api failed for {endpoint}"
    return result.stdout, None, None


def simplify_metadata(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    keys = (
        "full_name", "html_url", "description", "homepage", "default_branch", "fork", "archived",
        "disabled", "visibility", "created_at", "updated_at", "pushed_at", "stargazers_count",
        "forks_count", "open_issues_count", "subscribers_count", "language", "size",
    )
    return {key: data.get(key) for key in keys}


def simplify_items(items: Any, fields: tuple[str, ...], limit: int = 5) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [{key: item.get(key) for key in fields} for item in items[:limit] if isinstance(item, dict)]


def cmd_inspect(args: argparse.Namespace) -> int:
    if not REPO_RE.fullmatch(args.repo):
        payload = {
            **base_payload(), "repository": args.repo, "partial": True,
            "errors": [error("repository", "invalid_value", f"invalid repository: {args.repo}")],
        }
        emit(payload, args.output)
        return 1

    gh = require_gh()
    repo = args.repo
    errors: list[dict[str, str]] = []
    missing_optional: list[str] = []

    def collect(label: str, endpoint: str, optional: bool = False) -> Any | None:
        data, kind, message = gh_api_json(gh, endpoint, args.timeout)
        if kind:
            if optional and kind == "not_found":
                missing_optional.append(label)
            else:
                errors.append(error(label, kind, message or "unknown error"))
        return data

    def collect_raw(label: str, endpoint: str, optional: bool = False) -> str | None:
        data, kind, message = gh_api_raw(gh, endpoint, args.timeout)
        if kind:
            if optional and kind == "not_found":
                missing_optional.append(label)
            else:
                errors.append(error(label, kind, message or "unknown error"))
        return data

    metadata_raw = collect("metadata", f"repos/{repo}")
    commits_raw = collect("commits", f"repos/{repo}/commits?per_page=5")
    release_raw = collect("latest_release", f"repos/{repo}/releases/latest", optional=True)
    tags_raw = collect("tags", f"repos/{repo}/tags?per_page=5", optional=True)
    license_raw = collect("license", f"repos/{repo}/license", optional=True)
    root_raw = collect("root_contents", f"repos/{repo}/contents")
    issues_raw = collect("issues", f"repos/{repo}/issues?state=open&sort=updated&per_page=5", optional=True)
    pulls_raw = collect("pulls", f"repos/{repo}/pulls?state=open&sort=updated&per_page=5", optional=True)
    contributors_raw = collect("contributors", f"repos/{repo}/contributors?per_page=5", optional=True)
    readme = collect_raw("readme", f"repos/{repo}/readme", optional=True)

    security = collect_raw("security_policy", f"repos/{repo}/contents/SECURITY.md", optional=True)
    if security is None:
        security = collect_raw("security_policy_dotgithub", f"repos/{repo}/contents/.github/SECURITY.md", optional=True)
        if security is not None and "security_policy" in missing_optional:
            missing_optional.remove("security_policy")

    root_files = simplify_items(root_raw, ("name", "type", "html_url"), limit=500)
    manifests: dict[str, dict[str, Any]] = {}
    for item in root_files:
        name = item.get("name")
        if name in MANIFESTS:
            text = collect_raw(f"manifest:{name}", f"repos/{repo}/contents/{name}", optional=True)
            if text is not None:
                manifests[str(name)] = {
                    "content": text[: args.max_manifest_chars],
                    "truncated": len(text) > args.max_manifest_chars,
                }

    commits: list[dict[str, Any]] = []
    if isinstance(commits_raw, list):
        for item in commits_raw[:5]:
            commit = item.get("commit", {}) if isinstance(item, dict) else {}
            commits.append({
                "sha": item.get("sha") if isinstance(item, dict) else None,
                "url": item.get("html_url") if isinstance(item, dict) else None,
                "date": commit.get("committer", {}).get("date"),
                "message": (commit.get("message") or "").splitlines()[0],
            })

    latest_release = None
    if isinstance(release_raw, dict):
        latest_release = {key: release_raw.get(key) for key in ("tag_name", "name", "published_at", "html_url", "prerelease", "draft")}
    license_info = None
    if isinstance(license_raw, dict):
        license_info = {
            "name": license_raw.get("name"), "path": license_raw.get("path"),
            "html_url": license_raw.get("html_url"),
            "spdx_id": (license_raw.get("license") or {}).get("spdx_id"),
        }

    issues_only = [
        item for item in (issues_raw or [])
        if isinstance(item, dict) and "pull_request" not in item
    ]

    payload = {
        **base_payload(),
        "repository": repo,
        "partial": bool(errors),
        "errors": errors,
        "missing_optional": sorted(set(missing_optional)),
        "metadata": simplify_metadata(metadata_raw),
        "recent_commits": commits,
        "latest_release": latest_release,
        "tags": simplify_items(tags_raw, ("name", "zipball_url", "tarball_url")),
        "license": license_info,
        "root_contents": root_files,
        "manifests": manifests,
        "recent_open_issues": simplify_items(issues_only, ("number", "title", "html_url", "created_at", "updated_at", "comments")),
        "recent_open_pulls": simplify_items(pulls_raw, ("number", "title", "html_url", "created_at", "updated_at", "draft")),
        "contributors": simplify_items(contributors_raw, ("login", "html_url", "contributions")),
        "security_policy": {
            "content": (security or "")[: args.max_security_chars],
            "truncated": bool(security and len(security) > args.max_security_chars),
        },
        "readme": (readme or "")[: args.max_readme_chars],
        "readme_truncated": bool(readme and len(readme) > args.max_readme_chars),
    }
    emit(payload, args.output)
    return 2 if errors else 0


def bounded_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return number
    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=bounded_int(5, 600), default=DEFAULT_TIMEOUT)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check gh installation and authentication")
    doctor.add_argument("--output")
    doctor.set_defaults(func=cmd_doctor)

    search = sub.add_parser("search", help="run multiple GitHub repository queries")
    search.add_argument("--query", action="append", required=True)
    search.add_argument("--limit-per-query", type=bounded_int(1, 100), default=20)
    search.add_argument("--max-candidates", type=bounded_int(1, 500), default=60)
    search.add_argument("--output")
    search.set_defaults(func=cmd_search)

    adaptive = sub.add_parser("adaptive-search", help="run a phased V2.2 query plan")
    adaptive.add_argument("--plan", required=True, help="path to a V2.2 query-plan JSON file")
    adaptive.add_argument("--limit-per-query", type=bounded_int(1, 100), default=20)
    adaptive.add_argument("--max-candidates", type=bounded_int(1, 500), default=60)
    adaptive.add_argument("--output")
    adaptive.set_defaults(func=cmd_adaptive_search)

    inspect = sub.add_parser("inspect", help="collect evidence for one repository")
    inspect.add_argument("repo", help="OWNER/REPO")
    inspect.add_argument("--max-readme-chars", type=bounded_int(1000, 100000), default=20000)
    inspect.add_argument("--max-manifest-chars", type=bounded_int(500, 50000), default=12000)
    inspect.add_argument("--max-security-chars", type=bounded_int(500, 50000), default=10000)
    inspect.add_argument("--output")
    inspect.set_defaults(func=cmd_inspect)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (OSError, RuntimeError, ValueError) as exc:
        payload = {
            **base_payload(), "partial": True,
            "errors": [error("runtime", type(exc).__name__, str(exc))],
        }
        try:
            emit(payload, getattr(args, "output", None))
        except OSError as write_error:
            sys.stderr.write(json.dumps({**payload, "output_error": str(write_error)}, ensure_ascii=False) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
