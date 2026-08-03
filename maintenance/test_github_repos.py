#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("github_repos", ROOT / "scripts" / "github_repos.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


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

    def test_quality_merge_excludes_missing_license_and_prefers_relevant_base(self) -> None:
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
        self.assertEqual(
            result["excluded"],
            [{"fullName": "example/unlicensed", "reason": "missing_license"}],
        )

    def test_parser_exposes_adaptive_search(self) -> None:
        parser = MODULE.build_parser()
        args = parser.parse_args(["adaptive-search", "--plan", "plan.json"])
        self.assertEqual(args.command, "adaptive-search")
        self.assertEqual(args.plan, "plan.json")


if __name__ == "__main__":
    unittest.main()
