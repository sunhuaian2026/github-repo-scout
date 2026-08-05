#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from unittest import mock
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = ROOT / "skills" / "github-repo-scout"
SPEC = importlib.util.spec_from_file_location("github_repos", SKILL_ROOT / "scripts" / "github_repos.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@contextmanager
def github_api(responses: Mapping[str, tuple[int, Any, Mapping[str, str]]]):
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            path = urlparse(self.path).path
            status, payload, headers = responses.get(path, (404, {"message": "Not Found"}, {}))
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def row(name: str, description: str, license_key: str = "mit") -> dict:
    return {
        "fullName": name,
        "description": description,
        "license": {"key": license_key} if license_key else None,
        "isArchived": False,
        "isDisabled": False,
        "isPrivate": False,
        "isFork": False,
    }


class AdaptiveSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task = {
            "relevance_terms": ["semantic code search"],
            "constraint_terms": ["local", "offline"],
        }

    def test_doctor_allows_anonymous_public_api_access(self) -> None:
        responses = {
            "/rate_limit": (
                200,
                {"resources": {"core": {"remaining": 60, "reset": 123}, "search": {"remaining": 10, "reset": 123}}},
                {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "60", "X-RateLimit-Reset": "123"},
            )
        }
        with github_api(responses) as (base_url, requests), tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "doctor.json"
            args = argparse.Namespace(timeout=30, output=str(output))
            with mock.patch.dict(os.environ, {"GITHUB_API_URL": base_url, "GH_TOKEN": "", "GITHUB_TOKEN": ""}), mock.patch.object(MODULE.shutil, "which", return_value=None):
                self.assertEqual(MODULE.cmd_doctor(args), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["access_mode"], "anonymous")
        self.assertFalse(payload["authenticated"])
        self.assertIn("/rate_limit", requests)

    def test_doctor_fails_closed_when_anonymous_quota_is_exhausted(self) -> None:
        responses = {
            "/rate_limit": (
                200,
                {"resources": {"core": {"remaining": 0, "reset": 456}, "search": {"remaining": 0, "reset": 456}}},
                {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "456"},
            )
        }
        with github_api(responses) as (base_url, _), tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "doctor.json"
            args = argparse.Namespace(timeout=30, output=str(output))
            with mock.patch.dict(os.environ, {"GITHUB_API_URL": base_url, "GH_TOKEN": "", "GITHUB_TOKEN": ""}), mock.patch.object(MODULE.shutil, "which", return_value=None):
                self.assertEqual(MODULE.cmd_doctor(args), 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["errors"][0]["kind"], "rate_limited")

    def test_adaptive_search_works_without_gh_or_token(self) -> None:
        api_row = {
            "full_name": "example/public-tool",
            "html_url": "https://github.com/example/public-tool",
            "description": "Public local search tool",
            "stargazers_count": 10,
            "forks_count": 2,
            "open_issues_count": 1,
            "language": "Python",
            "license": {"key": "mit"},
            "archived": False,
            "disabled": False,
            "fork": False,
            "private": False,
            "pushed_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        responses = {
            "/search/repositories": (
                200,
                {"total_count": 1, "incomplete_results": False, "items": [api_row]},
                {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "9", "X-RateLimit-Reset": "123"},
            )
        }
        plan = {
            "license_required": False,
            "relevance_terms": ["public", "search"],
            "constraint_terms": [],
            "queries": [
                {"role": "category", "phase": "base", "query": "public search"},
                {"role": "task", "phase": "base", "query": "local search"},
            ],
        }
        with github_api(responses) as (base_url, requests), tempfile.TemporaryDirectory() as temp:
            plan_path = Path(temp) / "plan.json"
            output_path = Path(temp) / "results.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            args = argparse.Namespace(plan=str(plan_path), limit_per_query=10, max_candidates=10, timeout=30, output=str(output_path))
            with mock.patch.dict(os.environ, {"GITHUB_API_URL": base_url, "GH_TOKEN": "", "GITHUB_TOKEN": ""}), mock.patch.object(MODULE.shutil, "which", return_value=None):
                self.assertEqual(MODULE.cmd_adaptive_search(args), 0)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertTrue(payload["recommendation_eligible"])
        self.assertEqual(payload["candidates"][0]["fullName"], "example/public-tool")
        self.assertEqual(len([path for path in requests if path.startswith("/search/repositories?")]), 2)

    def test_low_yield_expansion_is_rejected(self) -> None:
        rows = [
            row("example/awesome-code", "A curated list of code resources"),
            row("example/sdk-sample", "Cloud SDK tutorial"),
        ]
        decision = MODULE.assess_expansion(rows, set(), self.task)
        self.assertFalse(decision["accepted"])
        self.assertFalse(decision["gap_closed"])

    def test_high_information_expansion_closes_gap(self) -> None:
        rows = [
            row(f"example/search-{index}", "Local semantic code search")
            for index in range(3)
        ]
        decision = MODULE.assess_expansion(rows, set(), self.task)
        self.assertTrue(decision["accepted"])
        self.assertTrue(decision["gap_closed"])

    def test_sample_word_alone_is_not_an_exclusion(self) -> None:
        candidate = row("example/search", "Semantic code search with sample configurations")
        self.assertIsNone(MODULE.metadata_gate_reason(candidate))

    def test_quality_merge_keeps_missing_license_for_use_only(self) -> None:
        candidates = [
            {
                **row("example/cloud-sdk", "Cloud SDK integration"),
                "matches": [{"role": "readme-local", "phase": "expansion", "rank": 1}],
            },
            {
                **row("example/local-search", "Local semantic code search"),
                "matches": [{"role": "task", "phase": "base", "rank": 5}],
            },
            {
                **row("example/unlicensed", "Local semantic code search", ""),
                "matches": [{"role": "category", "phase": "base", "rank": 1}],
            },
        ]
        result = MODULE.rank_candidates(candidates, self.task, 10)
        self.assertEqual(result["selected"][0]["fullName"], "example/local-search")
        self.assertIn("example/unlicensed", [item["fullName"] for item in result["selected"]])
        self.assertEqual(result["excluded"], [])

    def test_quality_merge_excludes_missing_license_when_distribution_requires_it(self) -> None:
        task = {**self.task, "license_required": True}
        candidate = {
            **row("example/unlicensed", "Local semantic code search", ""),
            "matches": [{"role": "category", "phase": "base", "rank": 1}],
        }
        result = MODULE.rank_candidates([candidate], task, 10)
        self.assertEqual(result["selected"], [])
        self.assertEqual(result["excluded"], [{"fullName": "example/unlicensed", "reason": "missing_license"}])

    def test_explicitly_deprecated_repository_is_excluded(self) -> None:
        candidate = row("example/RedditSharp-DEPRECATED-", "Reddit API wrapper")
        self.assertEqual(MODULE.metadata_gate_reason(candidate), "deprecated")

    def test_parser_exposes_adaptive_search(self) -> None:
        parser = MODULE.build_parser()
        args = parser.parse_args(["adaptive-search", "--plan", "plan.json"])
        self.assertEqual(args.command, "adaptive-search")
        self.assertEqual(args.plan, "plan.json")

    def test_non_core_activity_does_not_count_as_code_maintenance(self) -> None:
        self.assertEqual(
            MODULE.classify_commit_activity(["README.md", ".github/FUNDING.yml"]),
            "non_core",
        )
        self.assertEqual(MODULE.classify_commit_activity([".github/workflows/ci.yml"]), "non_core")
        self.assertEqual(MODULE.classify_commit_activity(["./.github/workflows/ci.yml"]), "non_core")
        self.assertEqual(MODULE.classify_commit_activity(["README.md", "assets/sponsor.png"]), "non_core")
        self.assertEqual(MODULE.classify_commit_activity(["assets/app-icon.png"]), "unknown")
        self.assertEqual(
            MODULE.classify_commit_activity(["README.md", "src/client.py"]),
            "code",
        )
        self.assertEqual(MODULE.classify_commit_activity([]), "unknown")

    def test_activity_summary_reports_latest_code_commit(self) -> None:
        summary = MODULE.summarize_activity(
            [
                {"date": "2026-07-11T20:53:23Z", "activity_kind": "non_core"},
                {"date": "2025-04-02T10:00:00Z", "activity_kind": "code"},
                {"date": "2026-01-01T00:00:00Z", "activity_kind": "unknown"},
            ]
        )
        self.assertEqual(summary["code_commits"], 1)
        self.assertEqual(summary["non_core_commits"], 1)
        self.assertEqual(summary["unknown_commits"], 1)
        self.assertEqual(summary["sampled_commit_count"], 3)
        self.assertEqual(summary["scope"], "recent_commits_only")
        self.assertEqual(summary["latest_observed_code_commit_at"], "2025-04-02T10:00:00Z")

    def test_platform_plan_is_deterministic_and_covers_four_routes(self) -> None:
        first = MODULE.build_platform_plan("Reddit")
        second = MODULE.build_platform_plan("Reddit")
        self.assertEqual(first, second)
        self.assertEqual(first, MODULE.build_platform_plan("reddit"))
        self.assertEqual(first["plan_type"], "platform_tools")
        self.assertEqual([item["role"] for item in first["queries"]], ["api-wrapper", "cli-scraper", "mcp", "agent-skill"])
        self.assertTrue(all(item["phase"] == "base" for item in first["queries"]))
        self.assertEqual(MODULE.build_platform_plan("reddit", "agent")["route_priority"][0], "mcp")

    def test_base_query_failure_fails_closed(self) -> None:
        plan = {
            "license_required": False,
            "relevance_terms": ["reddit"],
            "constraint_terms": [],
            "queries": [
                {"role": "api-wrapper", "phase": "base", "query": "query one"},
                {"role": "mcp", "phase": "base", "query": "query two"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            plan_path = Path(temp) / "plan.json"
            output_path = Path(temp) / "output.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            args = argparse.Namespace(plan=str(plan_path), limit_per_query=10, max_candidates=10, timeout=30, output=str(output_path))
            failures = [
                ([], MODULE.error("query one", "command_failed", "network blocked")),
                ([row("example/reddit", "Reddit MCP")], None),
            ]
            with mock.patch.object(MODULE, "search_rows", side_effect=failures):
                self.assertEqual(MODULE.cmd_adaptive_search(args), 2)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertFalse(payload["base_search_complete"])
            self.assertFalse(payload["recommendation_eligible"])

    def test_api_wrapper_route_has_priority_over_single_skill_hit(self) -> None:
        candidates = [
            {
                **row("example/wrapper", "Reddit API wrapper"),
                "matches": [{"role": "api-wrapper", "phase": "base", "rank": 1}],
            },
            {
                **row("example/skill", "Reddit skill"),
                "matches": [{"role": "agent-skill", "phase": "base", "rank": 1}],
            },
        ]
        task = {"relevance_terms": ["reddit"], "constraint_terms": []}
        result = MODULE.rank_candidates(candidates, task, 10)
        self.assertEqual(result["selected"][0]["fullName"], "example/wrapper")

    def test_deep_review_selection_covers_routes_deterministically(self) -> None:
        candidates = []
        for index, role in enumerate(("agent-skill", "mcp", "api-wrapper", "cli-scraper", "other"), start=1):
            candidates.append({
                "fullName": f"example/{role}",
                "licenseStatus": "known",
                "selectionScore": 1 / index,
                "matches": [{"role": role, "phase": "base", "rank": index}],
            })
        first = MODULE.select_deep_review(candidates)
        second = MODULE.select_deep_review(candidates)
        self.assertEqual(first, second)
        self.assertEqual([item["reason"] for item in first[:4]], ["top_api-wrapper", "top_mcp", "top_agent-skill", "top_cli-scraper"])
        self.assertEqual(len(first), 5)

    def test_decision_validator_blocks_unstable_and_false_free_recommendations(self) -> None:
        payload = {
            "base_search_complete": False,
            "candidate_fingerprint": "blocked",
            "requires_long_term_free": True,
            "platform_access_required": True,
            "license_use": "use_only",
            "candidates": [
                {
                    "repository": "example/tool",
                    "gate": "pass",
                    "recommended": True,
                    "recommendation_rank": 1,
                    "cost_type": "one_time_trial",
                    "cost_evidence_url": "https://example.com/pricing",
                    "cost_reset_period": None,
                    "license_status": "known",
                    "access_route": "official_api",
                    "terms_status": "permitted",
                    "terms_evidence_url": "https://example.com/developer-terms",
                    "deprecated": False,
                }
            ],
        }
        search_payload = {
            "base_search_complete": False,
            "recommendation_eligible": False,
            "candidate_fingerprint": "blocked",
            "plan": {"route_priority": MODULE.DEFAULT_ROUTE_PRIORITY},
            "deep_review_candidates": [{"fullName": "example/tool", "roles": ["api-wrapper"]}],
        }
        errors = MODULE.validate_decision(payload, search_payload)
        self.assertTrue(any("base search" in item for item in errors))
        self.assertTrue(any("one_time_trial" in item for item in errors))

    def test_decision_validator_accepts_recurring_free_tier_with_evidence(self) -> None:
        payload = {
            "base_search_complete": True,
            "candidate_fingerprint": "stable",
            "requires_long_term_free": True,
            "platform_access_required": True,
            "license_use": "use_only",
            "candidates": [
                {
                    "repository": "example/tool",
                    "gate": "conditional",
                    "recommended": True,
                    "recommendation_rank": 1,
                    "cost_type": "recurring_free_tier",
                    "cost_evidence_url": "https://example.com/pricing",
                    "cost_reset_period": "monthly",
                    "license_status": "known",
                    "access_route": "official_api",
                    "terms_status": "permitted",
                    "terms_evidence_url": "https://example.com/developer-terms",
                    "deprecated": False,
                }
            ],
        }
        search_payload = {
            "base_search_complete": True,
            "recommendation_eligible": True,
            "candidate_fingerprint": "stable",
            "plan": {"route_priority": MODULE.DEFAULT_ROUTE_PRIORITY},
            "deep_review_candidates": [{"fullName": "example/tool", "roles": ["api-wrapper"]}],
        }
        self.assertEqual(MODULE.validate_decision(payload, search_payload), [])

    def test_decision_validator_rejects_candidate_pool_substitution(self) -> None:
        recurring_payload = {
            "base_search_complete": True,
            "candidate_fingerprint": "stable",
            "requires_long_term_free": True,
            "platform_access_required": True,
            "license_use": "use_only",
            "candidates": [{
                "repository": "example/tool",
                "gate": "pass",
                "recommended": True,
                "recommendation_rank": 1,
                "cost_type": "recurring_free_tier",
                "cost_evidence_url": "https://example.com/pricing",
                "cost_reset_period": "monthly",
                "license_status": "known",
                "access_route": "official_api",
                "terms_status": "permitted",
                "terms_evidence_url": "https://example.com/developer-terms",
                "deprecated": False,
            }],
        }
        recurring_search = {
            "base_search_complete": True,
            "recommendation_eligible": True,
            "candidate_fingerprint": "stable",
            "plan": {"route_priority": MODULE.DEFAULT_ROUTE_PRIORITY},
            "deep_review_candidates": [{"fullName": "example/tool", "roles": ["api-wrapper"]}],
        }
        recurring_errors = MODULE.validate_decision(recurring_payload, recurring_search)
        self.assertTrue(any("requires gate=conditional" in item for item in recurring_errors))

        payload = {
            "base_search_complete": True,
            "candidate_fingerprint": "stable",
            "requires_long_term_free": False,
            "platform_access_required": True,
            "license_use": "use_only",
            "candidates": [{
                "repository": "example/replacement",
                "gate": "pass",
                "recommended": True,
                "recommendation_rank": 1,
                "cost_type": "unknown",
                "cost_evidence_url": None,
                "cost_reset_period": None,
                "license_status": "known",
                "access_route": "official_api",
                "terms_status": "permitted",
                "terms_evidence_url": "https://example.com/developer-terms",
                "deprecated": False,
            }],
        }
        search_payload = {
            "base_search_complete": True,
            "recommendation_eligible": True,
            "candidate_fingerprint": "stable",
            "plan": {"route_priority": MODULE.DEFAULT_ROUTE_PRIORITY},
            "deep_review_candidates": [{"fullName": "example/original", "roles": ["api-wrapper"]}],
        }
        errors = MODULE.validate_decision(payload, search_payload)
        self.assertTrue(any("exactly match deep review set" in item for item in errors))

    def test_decision_validator_rejects_recommendation_order_drift(self) -> None:
        def candidate(repository: str, rank: int) -> dict:
            return {
                "repository": repository,
                "gate": "conditional",
                "recommended": True,
                "recommendation_rank": rank,
                "cost_type": "recurring_free_tier",
                "cost_evidence_url": "https://example.com/pricing",
                "cost_reset_period": "monthly",
                "license_status": "known",
                "access_route": "official_api",
                "terms_status": "permitted",
                "terms_evidence_url": "https://example.com/developer-terms",
                "deprecated": False,
            }

        payload = {
            "base_search_complete": True,
            "candidate_fingerprint": "stable",
            "requires_long_term_free": True,
            "platform_access_required": True,
            "license_use": "use_only",
            "candidates": [candidate("example/sdk", 2), candidate("example/mcp", 1)],
        }
        search_payload = {
            "base_search_complete": True,
            "recommendation_eligible": True,
            "candidate_fingerprint": "stable",
            "plan": {"route_priority": MODULE.DEFAULT_ROUTE_PRIORITY},
            "deep_review_candidates": [
                {"fullName": "example/sdk", "roles": ["api-wrapper"]},
                {"fullName": "example/mcp", "roles": ["mcp"]},
            ],
        }
        errors = MODULE.validate_decision(payload, search_payload)
        self.assertTrue(any("violates route priority" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
