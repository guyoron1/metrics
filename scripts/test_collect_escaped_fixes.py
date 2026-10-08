#!/usr/bin/env python3
"""Unit tests for collect-escaped-fixes helpers (no GitHub calls)."""
from __future__ import annotations

import importlib.util
import unittest
from datetime import date
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "collect_escaped_fixes", Path(__file__).parent / "collect-escaped-fixes.py"
)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


def pr(repo, number, closed, title="fix: x", body=""):
    return {"repo": repo, "number": number, "closedAt": closed, "title": title, "body": body}


def no_sha(repo, sha):
    return None


class TestDetect(unittest.TestCase):
    def test_named_local_qualified_and_url(self):
        body = "See #10, fullsend-ai/agents#20 and https://github.com/fullsend-ai/fullsend/pull/30"
        found = mod.detect(pr("fullsend", 99, "", body=body), {}, no_sha)
        self.assertEqual(found, {"fullsend#10": "named", "agents#20": "named", "fullsend#30": "named"})

    def test_ignores_other_orgs_markdown_and_self(self):
        body = "## Notes\nother/repo#5, color &#35;12, this PR #99"
        self.assertEqual(mod.detect(pr("fullsend", 99, "", body=body), {}, no_sha), {})

    def test_causal_with_named(self):
        found = mod.detect(pr("agents", 50, "", body="This regression was introduced in #40."), {}, no_sha)
        self.assertEqual(found, {"agents#40": "causal+named"})

    def test_causal_sha_resolves_to_pr(self):
        found = mod.detect(
            pr("agents", 50, "", body="Broken by commit abc1234 last week."), {},
            lambda repo, sha: "agents#41" if sha == "abc1234" else None,
        )
        self.assertEqual(found, {"agents#41": "causal"})

    def test_named_in_linked_issue(self):
        found = mod.detect(pr("agents", 50, "", title="fix(#49): x"), {}, no_sha, "Caused by #44")
        # #49 is the issue itself; pair_rows drops it because it is not a merged PR.
        self.assertEqual(found, {"agents#49": "causal+named", "agents#44": "causal+named"})

    def test_revert_by_title_and_sha(self):
        by_title = {("fullsend", "feat: add thing"): "fullsend#7"}
        found = mod.detect(pr("fullsend", 9, "", title='Revert "feat: add thing"'), by_title, no_sha)
        self.assertEqual(found, {"fullsend#7": "revert"})
        found = mod.detect(
            pr("fullsend", 9, "", title="fix: revert cache", body="This reverts commit deadbeef1."),
            {}, lambda repo, sha: "fullsend#8",
        )
        self.assertEqual(found, {"fullsend#8": "revert"})

    def test_dependency_bumps_skipped(self):
        self.assertEqual(mod.detect(pr("fullsend", 9, "", title="chore(deps): bump x", body="#3"), {}, no_sha), {})


class TestPairWindow(unittest.TestCase):
    def test_only_earlier_merged_prs_within_30_days(self):
        merged = {
            "fullsend#1": pr("fullsend", 1, "2026-06-01T00:00:00Z"),
            "fullsend#2": pr("fullsend", 2, "2026-05-01T00:00:00Z"),
            "fullsend#4": pr("fullsend", 4, "2026-07-01T00:00:00Z"),
        }
        fix = pr("fullsend", 3, "2026-06-30T12:00:00Z", body="fixes #1, #2, #4 and issue #5")
        rows = mod.pair_rows([fix], merged, no_sha)
        self.assertEqual([(r["original"], r["days_between"]) for r in rows], [("fullsend#1", "29.5")])


class TestRates(unittest.TestCase):
    def setUp(self):
        # Week of 2026-06-01 (Mon): three PRs; week of 2026-06-08 not closed by 2026-07-10.
        self.details = [
            {"date": "2026-06-01", "repo": "fullsend", "number": "1", "pr_type": "feat"},
            {"date": "2026-06-03", "repo": "fullsend", "number": "2", "pr_type": "fix"},
            {"date": "2026-06-07", "repo": "fullsend", "number": "3", "pr_type": "feat"},
            {"date": "2026-06-08", "repo": "fullsend", "number": "4", "pr_type": "feat"},
        ]
        self.candidates = [
            {"fix": "fullsend#10", "original": "fullsend#1", "days_between": "5.0"},
            {"fix": "fullsend#11", "original": "fullsend#1", "days_between": "6.0"},
            {"fix": "fullsend#12", "original": "fullsend#2", "days_between": "2.0"},
            {"fix": "fullsend#13", "original": "fullsend#3", "days_between": "1.0"},
            {"fix": "fullsend#14", "original": "fullsend#4", "days_between": "1.0"},
        ]
        self.judgments = [
            {"fix": "fullsend#10", "original": "fullsend#1", "verdict": "defect_from_original", "severity": "high"},
            {"fix": "fullsend#11", "original": "fullsend#1", "verdict": "defect_from_original", "severity": "low"},
            {"fix": "fullsend#12", "original": "fullsend#2", "verdict": "later_change", "severity": ""},
            {"fix": "fullsend#14", "original": "fullsend#4", "verdict": "defect_from_original", "severity": ""},
        ]

    def test_rate_counts_distinct_originals_and_closed_weeks_only(self):
        rows = mod.rates(self.candidates, self.judgments, self.details, date(2026, 7, 10))
        by_key = {(r["week"], r["pr_type"]): r for r in rows}
        self.assertNotIn(("2026-06-08", "all"), by_key)
        total = by_key[("2026-06-01", "all")]
        self.assertEqual(
            (total["merged_prs"], total["escaped_prs"], total["severe_prs"], total["unjudged_candidates"]),
            (3, 1, 1, 1),
        )
        self.assertEqual(total["escaped_fix_rate"], "0.3333")
        self.assertEqual(by_key[("2026-06-01", "feat")]["escaped_fix_rate"], "0.5000")
        self.assertEqual(by_key[("2026-06-01", "fix")]["escaped_fix_rate"], "0.0000")

    def test_window_closes_36_days_after_week_start(self):
        self.assertFalse(mod.rates(self.candidates, self.judgments, self.details, date(2026, 7, 6)))
        self.assertTrue(mod.rates(self.candidates, self.judgments, self.details, date(2026, 7, 7)))

    def test_sync_adds_unjudged_and_drops_stale(self):
        judgments = mod.sync_judgments(
            self.candidates[:1],
            [{"fix": "a#1", "original": "a#0", "verdict": "unjudged"},
             {"fix": "a#2", "original": "a#0", "verdict": "not_related"}],
        )
        self.assertEqual(
            [(j["fix"], j["verdict"]) for j in judgments],
            [("a#2", "not_related"), ("fullsend#10", "unjudged")],
        )


if __name__ == "__main__":
    unittest.main()
