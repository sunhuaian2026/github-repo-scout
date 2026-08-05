#!/usr/bin/env python3
"""Deterministic GitHub evidence collector for github-repo-scout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MANIFESTS = {
    "package.json", "pyproject.toml", "requirements.txt", "setup.py", "Cargo.toml",
    "go.mod", "Gemfile", "pom.xml", "build.gradle", "Dockerfile",
    "docker-compose.yml", "compose.yml", "Makefile", "install.sh",
}
DEFAULT_TIMEOUT = 120
NOISE_TERMS = (
    "awesome", "curated list", "collection of", "resources for", "tutorial",
)
PLATFORM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,40}$")
COST_TYPES = {"permanent_free", "recurring_free_tier", "one_time_trial", "paid", "unknown"}
GATE_TYPES = {"pass", "conditional", "reject"}
LICENSE_USES = {"use_only", "modify_or_distribute"}
LICENSE_STATUSES = {"known", "missing", "unknown"}
ACCESS_ROUTES = {"official_api", "public_feed", "scraping", "third_party_archive", "mixed", "unknown"}
TERMS_STATUSES = {"permitted", "permitted_with_conditions", "separate_contract_required", "prohibited", "unknown"}
ROLE_WEIGHTS = {"api-wrapper": 1.25, "cli-scraper": 1.0, "mcp": 1.0, "agent-skill": 0.9}
DEFAULT_ROUTE_PRIORITY = ["api-wrapper", "mcp", "agent-skill", "cli-scraper"]


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
    return {"schema_version": "2.4", "generated_at": now_iso()}


def optional_token() -> tuple[str | None, str]:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value, name
    gh = shutil.which("gh")
    if gh:
        result = run([gh, "auth", "token"], 10)
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            return value, "gh"
    return None, "none"


class GitHubClient:
    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.token, self.auth_source = optional_token()
        self.timeout = timeout
        self.last_rate_limit: dict[str, Any] = {}

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def request(self, endpoint: str, accept: str = "application/vnd.github+json") -> tuple[bytes | None, str | None, str | None]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {
            "Accept": accept,
            "User-Agent": "github-repo-scout/2.4.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urlrequest.Request(url, headers=headers, method="GET")
        try:
            with urlrequest.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                self.last_rate_limit = {
                    "limit": response.headers.get("X-RateLimit-Limit"),
                    "remaining": response.headers.get("X-RateLimit-Remaining"),
                    "reset": response.headers.get("X-RateLimit-Reset"),
                    "resource": response.headers.get("X-RateLimit-Resource"),
                }
                return body, None, None
        except urlerror.HTTPError as exc:
            remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
            reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
            if exc.code in (403, 429) and remaining == "0":
                return None, "rate_limited", f"GitHub API rate limit exhausted; reset={reset or 'unknown'}"
            if exc.code == 404:
                return None, "not_found", f"GitHub API returned HTTP 404 for {endpoint}"
            return None, "http_error", f"GitHub API returned HTTP {exc.code} for {endpoint}"
        except urlerror.URLError as exc:
            return None, "network_error", str(exc.reason)
        except (OSError, TimeoutError) as exc:
            return None, "network_error", str(exc)

    def json(self, endpoint: str) -> tuple[Any | None, str | None, str | None]:
        body, kind, message = self.request(endpoint)
        if kind:
            return None, kind, message
        try:
            return json.loads((body or b"").decode("utf-8")), None, None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, "invalid_json", f"invalid JSON from {endpoint}: {exc}"

    def raw(self, endpoint: str) -> tuple[str | None, str | None, str | None]:
        body, kind, message = self.request(endpoint, "application/vnd.github.raw+json")
        if kind:
            return None, kind, message
        try:
            return (body or b"").decode("utf-8"), None, None
        except UnicodeDecodeError as exc:
            return None, "invalid_text", f"invalid UTF-8 from {endpoint}: {exc}"


def cmd_doctor(args: argparse.Namespace) -> int:
    client = GitHubClient(args.timeout)
    data, kind, message = client.json("rate_limit")
    errors: list[dict[str, str]] = []
    if kind:
        errors.append(error("github_api", kind, message or "GitHub API check failed"))
    resources = data.get("resources", {}) if isinstance(data, dict) else {}
    exhausted = [
        name for name in ("core", "search")
        if isinstance(resources.get(name), dict) and resources[name].get("remaining") == 0
    ]
    if exhausted:
        errors.append(error("github_api", "rate_limited", f"GitHub API quota exhausted: {', '.join(exhausted)}"))
    payload = {
        **base_payload(),
        "ok": not errors,
        "partial": bool(errors),
        "errors": errors,
        "api_url": client.base_url,
        "access_mode": "authenticated" if client.authenticated else "anonymous",
        "authenticated": client.authenticated,
        "auth_source": client.auth_source,
        "rate_limit": resources,
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


def metadata_gate_reason(row: dict[str, Any], license_required: bool = False) -> str | None:
    if row.get("isArchived"):
        return "archived"
    if row.get("isDisabled"):
        return "disabled"
    if row.get("isPrivate"):
        return "private"
    if row.get("isFork"):
        return "fork"
    repo_name = str(row.get("fullName") or "").rsplit("/", 1)[-1].lower()
    if re.search(r"(^|[-_.])deprecated($|[-_.])", repo_name):
        return "deprecated"
    if license_required and not license_key(row):
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
        if not name or metadata_gate_reason(row, bool(task.get("license_required"))):
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
        reason = metadata_gate_reason(row, bool(task.get("license_required")))
        if reason:
            excluded.append({"fullName": str(row.get("fullName") or ""), "reason": reason})
            continue
        score = 0.0
        roles: set[str] = set()
        for match in row.get("matches") or []:
            role = str(match.get("role") or "")
            roles.add(role)
            phase_weight = 0.55 if match.get("phase") == "expansion" else 1.0
            role_weight = ROLE_WEIGHTS.get(role, 1.0)
            rank = max(1, int(match.get("rank") or 1))
            score += phase_weight * role_weight / (5 + rank)
        text = norm_text(row)
        if contains_any(text, task["relevance_terms"]):
            score += 0.2
        if contains_any(text, task["constraint_terms"]):
            score += 0.08
        if not license_key(row):
            score -= 0.08
        if len(roles) > 1:
            score += 0.1 * (len(roles) - 1)
        copied = dict(row)
        copied["licenseStatus"] = "known" if license_key(row) else "missing"
        copied["selectionScore"] = round(score, 6)
        ranked.append(copied)
    ranked.sort(key=lambda item: (-item["selectionScore"], str(item.get("fullName") or "").lower()))
    return {"selected": ranked[:maximum], "excluded": excluded}


def build_platform_plan(platform: str, prefer: str = "sdk") -> dict[str, Any]:
    name = platform.strip()
    if not PLATFORM_RE.fullmatch(name):
        raise ValueError("platform must be 1-41 safe letters, numbers, spaces or ._+- characters")
    normalized = name.lower()
    route_priorities = {
        "sdk": DEFAULT_ROUTE_PRIORITY,
        "agent": ["mcp", "agent-skill", "api-wrapper", "cli-scraper"],
        "cli": ["cli-scraper", "api-wrapper", "mcp", "agent-skill"],
    }
    if prefer not in route_priorities:
        raise ValueError(f"unsupported platform route preference: {prefer}")
    return {
        "plan_type": "platform_tools",
        "platform": normalized,
        "license_required": False,
        "route_priority": route_priorities[prefer],
        "relevance_terms": [normalized, f"{normalized} api", f"{normalized} scraper"],
        "constraint_terms": [],
        "queries": [
            {"role": "api-wrapper", "phase": "base", "query": f'{normalized} API wrapper in:name,description,topics archived:false'},
            {"role": "cli-scraper", "phase": "base", "query": f'{normalized} scraper CLI in:name,description,topics archived:false'},
            {"role": "mcp", "phase": "base", "query": f'{normalized} MCP in:name,description,topics archived:false'},
            {"role": "agent-skill", "phase": "base", "query": f'{normalized} skill in:name,description,topics archived:false'},
        ],
    }


def deep_review_sort_key(item: dict[str, Any]) -> tuple[bool, float, str]:
    return (
        item.get("licenseStatus") != "known",
        -float(item.get("selectionScore") or 0),
        str(item.get("fullName") or "").lower(),
    )


def select_deep_review(
    candidates: list[dict[str, Any]],
    maximum: int = 5,
    route_priority: list[str] | None = None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for role in route_priority or DEFAULT_ROUTE_PRIORITY:
        matches = [
            item for item in candidates
            if any(match.get("role") == role for match in item.get("matches") or [])
        ]
        matches.sort(key=deep_review_sort_key)
        if not matches:
            continue
        item = matches[0]
        name = str(item.get("fullName") or "")
        if name and name.lower() not in seen:
            roles = sorted({str(match.get("role")) for match in item.get("matches") or [] if match.get("role")})
            selected.append({"fullName": name, "reason": f"top_{role}", "roles": roles})
            seen.add(name.lower())
    for item in sorted(candidates, key=deep_review_sort_key):
        if len(selected) >= maximum:
            break
        name = str(item.get("fullName") or "")
        if name and name.lower() not in seen:
            roles = sorted({str(match.get("role")) for match in item.get("matches") or [] if match.get("role")})
            selected.append({"fullName": name, "reason": "highest_remaining", "roles": roles})
            seen.add(name.lower())
    return selected


def cmd_platform_plan(args: argparse.Namespace) -> int:
    emit(build_platform_plan(args.platform, args.prefer), args.output)
    return 0


def load_query_plan(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("query plan must be a JSON object")
    if not isinstance(data.get("license_required", False), bool):
        raise ValueError("query plan field 'license_required' must be a boolean")
    data.setdefault("license_required", False)
    route_priority = data.get("route_priority", DEFAULT_ROUTE_PRIORITY)
    if (
        not isinstance(route_priority, list)
        or not all(isinstance(role, str) for role in route_priority)
        or len(route_priority) != len(DEFAULT_ROUTE_PRIORITY)
        or set(route_priority) != set(DEFAULT_ROUTE_PRIORITY)
    ):
        raise ValueError(f"query plan route_priority must be a permutation of {DEFAULT_ROUTE_PRIORITY}")
    data.setdefault("route_priority", list(DEFAULT_ROUTE_PRIORITY))
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
    if not 2 <= base_count <= 4 or not 0 <= expansion_count <= 2:
        raise ValueError("query plan requires 2-4 base queries and 0-2 expansion queries")
    for field in ("relevance_terms", "constraint_terms"):
        if not all(isinstance(term, str) and term.strip() for term in data[field]):
            raise ValueError(f"query plan field {field!r} must contain non-empty strings")
    if not data["relevance_terms"]:
        raise ValueError("query plan requires at least one relevance term")
    return data


def normalize_search_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "fullName": row.get("full_name"),
        "url": row.get("html_url"),
        "description": row.get("description"),
        "stargazersCount": row.get("stargazers_count"),
        "forksCount": row.get("forks_count"),
        "openIssuesCount": row.get("open_issues_count"),
        "language": row.get("language"),
        "license": row.get("license"),
        "isArchived": row.get("archived"),
        "isDisabled": row.get("disabled"),
        "isFork": row.get("fork"),
        "isPrivate": row.get("private"),
        "pushedAt": row.get("pushed_at"),
        "updatedAt": row.get("updated_at"),
    }


def search_rows(
    client: GitHubClient, query: str, limit: int, timeout: int,
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    del timeout
    endpoint = f"search/repositories?q={urlparse.quote_plus(query)}&per_page={min(limit, 100)}"
    data, kind, message = client.json(endpoint)
    if kind:
        return [], error(query, kind, message or "GitHub repository search failed")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return [], error(query, "invalid_shape", "GitHub search response items is not a list")
    rows = [normalize_search_row(row) for row in items if isinstance(row, dict)]
    valid = [row for row in rows if row.get("fullName")]
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
    client = GitHubClient(args.timeout)
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
        rows, failure = search_rows(client, query, args.limit_per_query, args.timeout)
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
    base_search_complete = not any(
        item.get("phase") == "base" and item.get("status") == "error"
        for item in decisions
    )
    fingerprint_rows = [
        {
            "fullName": item.get("fullName"),
            "matches": item.get("matches"),
            "license": license_key(item),
        }
        for item in ranked["selected"]
    ]
    candidate_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    deep_review_limit = 5 if client.authenticated else 3
    deep_review_candidates = select_deep_review(
        ranked["selected"], maximum=deep_review_limit, route_priority=plan.get("route_priority")
    )
    payload = {
        **base_payload(),
        "partial": bool(errors),
        "base_search_complete": base_search_complete,
        "recommendation_eligible": base_search_complete,
        "candidate_fingerprint": candidate_fingerprint,
        "deep_review_candidates": deep_review_candidates,
        "access_mode": "authenticated" if client.authenticated else "anonymous",
        "deep_review_limit": deep_review_limit,
        "rate_limit": client.last_rate_limit,
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
    client = GitHubClient(args.timeout)
    errors: list[dict[str, str]] = []
    rows_by_query: list[list[dict[str, Any]]] = []
    merged: dict[str, dict[str, Any]] = {}
    raw_hits = 0

    for query in args.query:
        rows, failure = search_rows(client, query, args.limit_per_query, args.timeout)
        if failure:
            errors.append(failure)
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


def gh_api_json(client: GitHubClient, endpoint: str, timeout: int) -> tuple[Any | None, str | None, str | None]:
    del timeout
    return client.json(endpoint)


def gh_api_raw(client: GitHubClient, endpoint: str, timeout: int) -> tuple[str | None, str | None, str | None]:
    del timeout
    return client.raw(endpoint)


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


def classify_commit_activity(files: list[str]) -> str:
    if not files:
        return "unknown"
    documentation_names = {
        "code_of_conduct.md",
        "contributing.md",
        "funding.yml",
        "license",
        "license.md",
        "security.md",
    }
    media_suffixes = {".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
    saw_documentation = False
    saw_media = False
    for raw_path in files:
        path = raw_path.strip().lower()
        while path.startswith("./"):
            path = path[2:]
        name = Path(path).name
        if path.startswith((".github/", "docs/")) or name.startswith("readme") or name in documentation_names:
            saw_documentation = True
            continue
        if Path(path).suffix in media_suffixes:
            saw_media = True
            continue
        return "code"
    if saw_documentation:
        return "non_core"
    if saw_media:
        return "unknown"
    return "non_core"


def summarize_activity(commits: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"code": 0, "non_core": 0, "unknown": 0}
    code_dates: list[str] = []
    for commit in commits:
        kind = str(commit.get("activity_kind") or "unknown")
        if kind not in counts:
            kind = "unknown"
        counts[kind] += 1
        date = commit.get("date")
        if kind == "code" and isinstance(date, str) and date:
            code_dates.append(date)
    return {
        "scope": "recent_commits_only",
        "sampled_commit_count": len(commits),
        "code_commits": counts["code"],
        "non_core_commits": counts["non_core"],
        "unknown_commits": counts["unknown"],
        "latest_observed_code_commit_at": max(code_dates) if code_dates else None,
    }


def cmd_inspect(args: argparse.Namespace) -> int:
    if not REPO_RE.fullmatch(args.repo):
        payload = {
            **base_payload(), "repository": args.repo, "partial": True,
            "errors": [error("repository", "invalid_value", f"invalid repository: {args.repo}")],
        }
        emit(payload, args.output)
        return 1

    client = GitHubClient(args.timeout)
    repo = args.repo
    errors: list[dict[str, str]] = []
    missing_optional: list[str] = []
    omitted_for_quota: list[str] = []

    def collect(label: str, endpoint: str, optional: bool = False) -> Any | None:
        data, kind, message = gh_api_json(client, endpoint, args.timeout)
        if kind:
            if optional and kind == "not_found":
                missing_optional.append(label)
            else:
                errors.append(error(label, kind, message or "unknown error"))
        return data

    def collect_raw(label: str, endpoint: str, optional: bool = False) -> str | None:
        data, kind, message = gh_api_raw(client, endpoint, args.timeout)
        if kind:
            if optional and kind == "not_found":
                missing_optional.append(label)
            else:
                errors.append(error(label, kind, message or "unknown error"))
        return data

    metadata_raw = collect("metadata", f"repos/{repo}")
    commits_raw = collect("commits", f"repos/{repo}/commits?per_page=5")
    release_raw = collect("latest_release", f"repos/{repo}/releases/latest", optional=True)
    tags_raw = collect("tags", f"repos/{repo}/tags?per_page=5", optional=True) if client.authenticated else None
    license_raw = collect("license", f"repos/{repo}/license", optional=True)
    root_raw = collect("root_contents", f"repos/{repo}/contents")
    issues_raw = collect("issues", f"repos/{repo}/issues?state=open&sort=updated&per_page=5", optional=True)
    pulls_raw = collect("pulls", f"repos/{repo}/pulls?state=open&sort=updated&per_page=5", optional=True)
    contributors_raw = collect("contributors", f"repos/{repo}/contributors?per_page=5", optional=True) if client.authenticated else None
    if not client.authenticated:
        omitted_for_quota.extend(["tags", "contributors"])
    readme = collect_raw("readme", f"repos/{repo}/readme", optional=True)

    security = collect_raw("security_policy", f"repos/{repo}/contents/SECURITY.md", optional=True)
    if security is None:
        security = collect_raw("security_policy_dotgithub", f"repos/{repo}/contents/.github/SECURITY.md", optional=True)
        if security is not None and "security_policy" in missing_optional:
            missing_optional.remove("security_policy")

    root_files = simplify_items(root_raw, ("name", "type", "html_url"), limit=500)
    manifests: dict[str, dict[str, Any]] = {}
    manifest_limit = None if client.authenticated else 4
    manifest_count = 0
    for item in root_files:
        name = item.get("name")
        if name in MANIFESTS:
            if manifest_limit is not None and manifest_count >= manifest_limit:
                omitted_for_quota.append(f"manifest:{name}")
                continue
            manifest_count += 1
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
            sha = item.get("sha") if isinstance(item, dict) else None
            detail = collect(f"commit_detail:{sha}", f"repos/{repo}/commits/{sha}") if sha else None
            changed_files = [
                str(file.get("filename"))
                for file in ((detail or {}).get("files") or [])
                if isinstance(file, dict) and file.get("filename")
            ]
            commits.append({
                "sha": sha,
                "url": item.get("html_url") if isinstance(item, dict) else None,
                "date": commit.get("committer", {}).get("date"),
                "message": (commit.get("message") or "").splitlines()[0],
                "changed_files": changed_files,
                "activity_kind": classify_commit_activity(changed_files),
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
        "access_mode": "authenticated" if client.authenticated else "anonymous",
        "collection_profile": "full" if client.authenticated else "anonymous_budgeted",
        "omitted_for_quota": omitted_for_quota,
        "rate_limit": client.last_rate_limit,
        "partial": bool(errors),
        "errors": errors,
        "missing_optional": sorted(set(missing_optional)),
        "metadata": simplify_metadata(metadata_raw),
        "recent_commits": commits,
        "activity_summary": summarize_activity(commits),
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


def validate_decision(payload: Any, search_payload: Any | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["decision must be a JSON object"]
    base_complete = payload.get("base_search_complete")
    requires_free = payload.get("requires_long_term_free")
    platform_access_required = payload.get("platform_access_required")
    license_use = payload.get("license_use")
    candidates = payload.get("candidates")
    if not isinstance(base_complete, bool):
        errors.append("base_search_complete must be a boolean")
    if not isinstance(requires_free, bool):
        errors.append("requires_long_term_free must be a boolean")
    if not isinstance(platform_access_required, bool):
        errors.append("platform_access_required must be a boolean")
    if license_use not in LICENSE_USES:
        errors.append(f"license_use must be one of {sorted(LICENSE_USES)}")
    if not isinstance(candidates, list):
        errors.append("candidates must be a list")
        return errors

    expected_repositories: set[str] | None = None
    deep_review_roles: dict[str, list[str]] = {}
    route_priority = list(DEFAULT_ROUTE_PRIORITY)
    if search_payload is not None:
        if not isinstance(search_payload, dict):
            errors.append("search results must be a JSON object")
        else:
            search_complete = search_payload.get("base_search_complete") is True
            if not search_complete or search_payload.get("recommendation_eligible") is not True:
                errors.append("search results are not recommendation eligible")
            if base_complete is not search_complete:
                errors.append("decision base_search_complete does not match search results")
            search_fingerprint = search_payload.get("candidate_fingerprint")
            if payload.get("candidate_fingerprint") != search_fingerprint:
                errors.append("decision candidate_fingerprint does not match search results")
            deep_review = search_payload.get("deep_review_candidates")
            if not isinstance(deep_review, list):
                errors.append("search results deep_review_candidates must be a list")
            else:
                expected_repositories = {
                    str(item.get("fullName") or "").lower()
                    for item in deep_review if isinstance(item, dict) and item.get("fullName")
                }
                deep_review_roles = {
                    str(item.get("fullName") or "").lower(): [str(role) for role in item.get("roles") or []]
                    for item in deep_review if isinstance(item, dict) and item.get("fullName")
                }
            search_plan = search_payload.get("plan")
            plan_priority = search_plan.get("route_priority") if isinstance(search_plan, dict) else None
            if (
                isinstance(plan_priority, list)
                and all(isinstance(role, str) for role in plan_priority)
                and len(plan_priority) == len(DEFAULT_ROUTE_PRIORITY)
                and set(plan_priority) == set(DEFAULT_ROUTE_PRIORITY)
            ):
                route_priority = [str(role) for role in plan_priority]

    seen: set[str] = set()
    recommended_rows: list[tuple[str, int]] = []
    for index, item in enumerate(candidates, start=1):
        prefix = f"candidate {index}"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        repository = item.get("repository")
        if not isinstance(repository, str) or not REPO_RE.fullmatch(repository):
            errors.append(f"{prefix} repository must be OWNER/REPO")
        elif repository.lower() in seen:
            errors.append(f"{prefix} duplicates repository {repository}")
        else:
            seen.add(repository.lower())
        gate = item.get("gate")
        cost_type = item.get("cost_type")
        license_status = item.get("license_status")
        access_route = item.get("access_route")
        terms_status = item.get("terms_status")
        terms_evidence_url = item.get("terms_evidence_url")
        recommended = item.get("recommended")
        deprecated = item.get("deprecated")
        recommendation_rank = item.get("recommendation_rank")
        if gate not in GATE_TYPES:
            errors.append(f"{prefix} gate must be one of {sorted(GATE_TYPES)}")
        if cost_type not in COST_TYPES:
            errors.append(f"{prefix} cost_type must be one of {sorted(COST_TYPES)}")
        if license_status not in LICENSE_STATUSES:
            errors.append(f"{prefix} license_status must be one of {sorted(LICENSE_STATUSES)}")
        if access_route is not None and access_route not in ACCESS_ROUTES:
            errors.append(f"{prefix} access_route must be one of {sorted(ACCESS_ROUTES)}")
        if terms_status is not None and terms_status not in TERMS_STATUSES:
            errors.append(f"{prefix} terms_status must be one of {sorted(TERMS_STATUSES)}")
        if platform_access_required and access_route not in ACCESS_ROUTES:
            errors.append(f"{prefix} platform task requires access_route")
        if platform_access_required and terms_status not in TERMS_STATUSES:
            errors.append(f"{prefix} platform task requires terms_status")
        if not isinstance(recommended, bool):
            errors.append(f"{prefix} recommended must be a boolean")
            recommended = False
        if not isinstance(deprecated, bool):
            errors.append(f"{prefix} deprecated must be a boolean")
            deprecated = False
        if not recommended:
            if recommendation_rank is not None:
                errors.append(f"{prefix} non-recommended candidate must have null recommendation_rank")
            continue
        if not isinstance(recommendation_rank, int) or isinstance(recommendation_rank, bool) or recommendation_rank < 1:
            errors.append(f"{prefix} recommended candidate requires a positive integer recommendation_rank")
        elif isinstance(repository, str):
            recommended_rows.append((repository.lower(), recommendation_rank))
        if base_complete is False:
            errors.append(f"{prefix} cannot be recommended because base search is incomplete")
        if gate == "reject":
            errors.append(f"{prefix} cannot be recommended with reject gate")
        if cost_type == "recurring_free_tier" and gate != "conditional":
            errors.append(f"{prefix} recurring_free_tier requires gate=conditional")
        if deprecated:
            errors.append(f"{prefix} cannot recommend a deprecated repository")
        if platform_access_required and terms_status not in {"permitted", "permitted_with_conditions"}:
            errors.append(f"{prefix} recommendation requires a permitted terms_status")
        if platform_access_required and terms_status == "permitted_with_conditions" and gate != "conditional":
            errors.append(f"{prefix} permitted_with_conditions requires gate=conditional")
        if platform_access_required and (
            not isinstance(terms_evidence_url, str) or not terms_evidence_url.startswith(("https://", "http://"))
        ):
            errors.append(f"{prefix} recommendation requires an official platform terms_evidence_url")
        if license_use == "modify_or_distribute" and license_status != "known":
            errors.append(f"{prefix} requires a known license for modification or distribution")
        if requires_free:
            if cost_type not in {"permanent_free", "recurring_free_tier"}:
                errors.append(f"{prefix} cost_type {cost_type} does not satisfy long-term free use")
            evidence_url = item.get("cost_evidence_url")
            if not isinstance(evidence_url, str) or not evidence_url.startswith(("https://", "http://")):
                errors.append(f"{prefix} requires an official cost_evidence_url")
            if cost_type == "recurring_free_tier" and not item.get("cost_reset_period"):
                errors.append(f"{prefix} recurring_free_tier requires cost_reset_period")
    if expected_repositories is not None and seen != expected_repositories:
        missing = sorted(expected_repositories - seen)
        extra = sorted(seen - expected_repositories)
        errors.append(f"decision repositories must exactly match deep review set; missing={missing}, extra={extra}")
    if recommended_rows:
        actual_ranks = sorted(rank for _, rank in recommended_rows)
        if actual_ranks != list(range(1, len(recommended_rows) + 1)):
            errors.append("recommended candidates must use contiguous recommendation_rank values starting at 1")
        role_index = {role: index for index, role in enumerate(route_priority)}
        expected_order = sorted(
            (repository for repository, _ in recommended_rows),
            key=lambda repository: (
                min((role_index.get(role, len(role_index)) for role in deep_review_roles.get(repository, [])), default=len(role_index)),
                repository,
            ),
        )
        actual_order = [repository for repository, _ in sorted(recommended_rows, key=lambda row: row[1])]
        if actual_order != expected_order:
            errors.append(f"recommendation_rank violates route priority; expected={expected_order}, actual={actual_order}")
    return errors


def cmd_validate_decision(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
    search_payload = json.loads(Path(args.search_results).expanduser().read_text(encoding="utf-8"))
    errors = validate_decision(payload, search_payload)
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    emit({
        **base_payload(),
        "ok": not errors,
        "errors": errors,
        "candidate_fingerprint": search_payload.get("candidate_fingerprint") if isinstance(search_payload, dict) else None,
        "decision_fingerprint": fingerprint,
    }, args.output)
    return 0 if not errors else 1


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

    doctor = sub.add_parser("doctor", help="check GitHub API access and available quota")
    doctor.add_argument("--output")
    doctor.set_defaults(func=cmd_doctor)

    platform_plan = sub.add_parser("platform-plan", help="create a deterministic platform-tool query plan")
    platform_plan.add_argument("platform", help="platform name, for example Reddit")
    platform_plan.add_argument("--prefer", choices=("sdk", "agent", "cli"), default="sdk")
    platform_plan.add_argument("--output")
    platform_plan.set_defaults(func=cmd_platform_plan)

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

    decision = sub.add_parser("validate-decision", help="validate structured recommendation gates")
    decision.add_argument("--input", required=True, help="decision JSON file")
    decision.add_argument("--search-results", required=True, help="adaptive-search JSON file")
    decision.add_argument("--output")
    decision.set_defaults(func=cmd_validate_decision)
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
