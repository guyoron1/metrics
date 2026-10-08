#!/usr/bin/env python3
"""Find PRs that had to be fixed by a later PR and compute the 30-day escaped-fix rate.

1. Candidates: a later merged PR in fullsend/agents that names an earlier
   merged PR (#N, owner/repo#N or a PR link) in its title, body or the issue
   it closes, uses causal wording ("broke", "regression", "caused by",
   "introduced in", "fixes ... from #N"), or reverts it. Pairs more than 30 days apart are dropped. Written to
   docs/fix-candidates.csv.
2. Judgments: every candidate pair gets a row in docs/fix-judgments.csv,
   "unjudged" until a person or the optional FIX_JUDGE_CMD hook fills it in.
3. Rate: docs/escaped-fixes.csv, per week (by the earlier PR's merge date),
   repo and PR type, only for weeks whose 30-day window has closed. Merged
   PR counts and types come from docs/pr-type-details.csv.

FIX_JUDGE_CMD (optional): a shell command run once per unjudged pair. It gets
a JSON object on stdin (fix, original, fix_url, original_url, fix_diff,
original_diff) and must print {"verdict": ..., "severity": ...,
"judged_by": ...} where verdict is one of defect_from_original,
later_change, not_related, unknown and severity is low, medium, high or "".
Unset means no judging.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORG = "fullsend-ai"
REPOS = ("fullsend", "agents")
WINDOW_DAYS = 30
CANDIDATES = ROOT / "docs" / "fix-candidates.csv"
JUDGMENTS = ROOT / "docs" / "fix-judgments.csv"
OUTPUT = ROOT / "docs" / "escaped-fixes.csv"
DETAILS = ROOT / "docs" / "pr-type-details.csv"
AGGREGATE = ROOT / "docs" / "pr-type.csv"

CANDIDATE_HEADER = ["fix", "original", "detected_by", "days_between", "fix_merged_at", "original_merged_at"]
JUDGMENT_HEADER = ["fix", "original", "verdict", "severity", "judged_by", "judged_at"]
OUTPUT_HEADER = [
    "week", "repo", "pr_type", "merged_prs", "escaped_prs", "severe_prs",
    "unjudged_candidates", "escaped_fix_rate",
]
VERDICTS = {"defect_from_original", "later_change", "not_related", "unknown"}

URL_REF = re.compile(r"github\.com/fullsend-ai/([\w.-]+)/pull/(\d+)", re.I)
QUALIFIED_REF = re.compile(r"(?<![\w/.-])fullsend-ai/([\w.-]+)#(\d+)", re.I)
LOCAL_REF = re.compile(r"(?<![\w/&#])#(\d+)\b")
CAUSAL = re.compile(
    r"\b(?:broke|breaks|broken by|regression|regressed|caused by|introduced (?:in|by)"
    r"|fix(?:es|ed)?\b[^\n]{0,80}?\bfrom\s+(?:fullsend-ai/[\w.-]+)?#\d+)",
    re.I,
)
CAUSAL_SHA = re.compile(
    r"\b(?:broke|broken by|regressed (?:in|by)|regression (?:in|from)|caused by|introduced (?:in|by))"
    r"\s+(?:commit\s+)?`?([0-9a-f]{7,40})\b",
    re.I,
)
# Same revert subject rule as collect-quality.py.
REVERT_TITLE = re.compile(r"^\s*(?:(?:[a-z][\w-]*)(?:\([^)]*\))?!?:\s*)?revert(?:\b|!|:)", re.I)
REVERT_QUOTED = re.compile(r"revert\w*\s+\"(.+)\"", re.I)
REVERT_SHA = re.compile(r"\bThis reverts commit\s+([0-9a-f]{7,40})\b", re.I)
DEPS_TITLE = re.compile(r"^\s*\w+\(deps\)", re.I)  # dependency bumps quote upstream changelogs


def _load_pr_type():
    spec = importlib.util.spec_from_file_location("collect_pr_type", Path(__file__).parent / "collect-pr-type.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write(path: Path, header: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def merged_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def references(repo: str, text: str) -> set[str]:
    """Return repo#N keys for every fullsend/agents PR or issue number named in text."""
    refs = {f"{r.lower()}#{n}" for r, n in URL_REF.findall(text)}
    refs |= {f"{r.lower()}#{n}" for r, n in QUALIFIED_REF.findall(text)}
    refs |= {f"{repo}#{n}" for n in LOCAL_REF.findall(text)}
    return {ref for ref in refs if ref.split("#")[0] in REPOS}


def detect(pr: dict, by_title: dict[tuple[str, str], str], sha_to_pr, linked_text: str = "") -> dict[str, str]:
    """Return {original_key: detected_by} for one later PR (unfiltered by date).

    linked_text is the title and body of the issue the PR closes; fix PRs
    often name the PR that broke things only there.
    """
    title = pr.get("title") or ""
    if DEPS_TITLE.match(title):
        return {}
    repo = pr["repo"]
    text = f"{title}\n{pr.get('body') or ''}\n{linked_text}"
    found = dict.fromkeys(references(repo, text), "named")
    if CAUSAL.search(text):
        found = dict.fromkeys(found, "causal+named")
        for sha in CAUSAL_SHA.findall(text):
            key = sha_to_pr(repo, sha)
            if key:
                found.setdefault(key, "causal")
    if REVERT_TITLE.match(title):
        reverted = set(found)
        quoted = REVERT_QUOTED.search(title)
        if quoted and (repo, quoted.group(1).strip()) in by_title:
            reverted.add(by_title[(repo, quoted.group(1).strip())])
        for sha in REVERT_SHA.findall(text):
            key = sha_to_pr(repo, sha)
            if key:
                reverted.add(key)
        found.update(dict.fromkeys(reverted, "revert"))
    found.pop(f"{repo}#{pr['number']}", None)
    return found


def pair_rows(fixes: list[dict], merged: dict[str, dict], sha_to_pr, linked_text=lambda pr: "") -> list[dict]:
    """Keep pairs whose original is a merged PR from the 30 days before the fix."""
    by_title = {(pr["repo"], (pr.get("title") or "").strip()): key for key, pr in merged.items()}
    rows = []
    for pr in fixes:
        fix_key = f"{pr['repo']}#{pr['number']}"
        for original, source in detect(pr, by_title, sha_to_pr, linked_text(pr)).items():
            if original not in merged:
                continue
            days = (merged_time(pr["closedAt"]) - merged_time(merged[original]["closedAt"])).total_seconds() / 86400
            if 0 < days <= WINDOW_DAYS:
                rows.append({
                    "fix": fix_key, "original": original, "detected_by": source,
                    "days_between": f"{days:.1f}", "fix_merged_at": pr["closedAt"],
                    "original_merged_at": merged[original]["closedAt"],
                })
    return rows


def gh_sha_lookup():
    cache: dict[tuple[str, str], str | None] = {}

    def lookup(repo: str, sha: str) -> str | None:
        if (repo, sha) not in cache:
            result = subprocess.run(
                ["gh", "api", f"repos/{ORG}/{repo}/commits/{sha}/pulls",
                 "--jq", "[.[] | select(.merged_at != null) | .number][0]"],
                capture_output=True, text=True,
            )
            number = result.stdout.strip() if result.returncode == 0 else ""
            cache[(repo, sha)] = f"{repo}#{number}" if number.isdigit() else None
        return cache[(repo, sha)]

    return lookup


def gh_linked_issue_text(pr_type):
    def linked_text(pr: dict) -> str:
        title = pr.get("title") or ""
        ref = None if DEPS_TITLE.match(title) else pr_type.extract_issue_ref(pr["repo"], title, pr.get("body"))
        if not ref or ref[0] not in REPOS:
            return ""
        result = subprocess.run(
            ["gh", "api", f"repos/{ORG}/{ref[0]}/issues/{ref[1]}", "--jq", '.title + "\\n" + (.body // "")'],
            capture_output=True, text=True,
        )
        # Bare #N in the issue refers to the issue's own repo.
        text = result.stdout if result.returncode == 0 else ""
        return text if ref[0] == pr["repo"] else LOCAL_REF.sub(f"{ORG}/{ref[0]}#\\1", text)

    return linked_text


def collect(start: date, end: date) -> None:
    """Replace candidate rows for fixes merged in [start, end]."""
    pr_type = _load_pr_type()
    merged: dict[str, dict] = {}
    for repo in REPOS:
        print(f"Fetching {ORG}/{repo} merged PRs {start - timedelta(days=WINDOW_DAYS)} → {end}...")
        for pr in pr_type.fetch_merged_range(repo, start - timedelta(days=WINDOW_DAYS), end):
            pr["repo"] = repo
            merged[f"{repo}#{pr['number']}"] = pr
    fixes = [pr for pr in merged.values() if start.isoformat() <= pr["closedAt"][:10] <= end.isoformat()]
    new_rows = pair_rows(fixes, merged, gh_sha_lookup(), gh_linked_issue_text(pr_type))
    kept = [row for row in read(CANDIDATES) if not start.isoformat() <= row["fix_merged_at"][:10] <= end.isoformat()]
    rows = sorted(kept + new_rows, key=lambda row: (row["fix_merged_at"], row["fix"], row["original"]))
    write(CANDIDATES, CANDIDATE_HEADER, rows)
    print(f"  {len(fixes)} merged PRs checked, {len(new_rows)} candidate pairs")


def sync_judgments(candidates: list[dict], judgments: list[dict]) -> list[dict]:
    """Add an "unjudged" row for each new candidate pair; drop unjudged rows that are no longer candidates."""
    pairs = {(row["fix"], row["original"]) for row in candidates}
    judgments = [row for row in judgments if row["verdict"] != "unjudged" or (row["fix"], row["original"]) in pairs]
    known = {(row["fix"], row["original"]) for row in judgments}
    for row in candidates:
        if (row["fix"], row["original"]) not in known:
            known.add((row["fix"], row["original"]))
            judgments.append({"fix": row["fix"], "original": row["original"], "verdict": "unjudged",
                              "severity": "", "judged_by": "", "judged_at": ""})
    return judgments


def pr_diff(key: str) -> str:
    repo, number = key.split("#")
    result = subprocess.run(["gh", "pr", "diff", number, "--repo", f"{ORG}/{repo}"], capture_output=True, text=True)
    return result.stdout


def judge(candidates: list[dict], judgments: list[dict]) -> None:
    """Fill unjudged candidate pairs from FIX_JUDGE_CMD; no-op when it is unset."""
    command = os.environ.get("FIX_JUDGE_CMD")
    if not command:
        return
    pairs = {(row["fix"], row["original"]) for row in candidates}
    for row in judgments:
        if row["verdict"] != "unjudged" or (row["fix"], row["original"]) not in pairs:
            continue
        payload = {"fix": row["fix"], "original": row["original"]}
        for side in ("fix", "original"):
            repo, number = row[side].split("#")
            payload[f"{side}_url"] = f"https://github.com/{ORG}/{repo}/pull/{number}"
            payload[f"{side}_diff"] = pr_diff(row[side])
        result = subprocess.run(command, shell=True, input=json.dumps(payload), capture_output=True, text=True)
        try:
            answer = json.loads(result.stdout) if result.returncode == 0 else {}
        except json.JSONDecodeError:
            answer = {}
        if answer.get("verdict") not in VERDICTS:
            print(f"  judge skipped {row['fix']} -> {row['original']}: {result.stderr.strip()[:200]}", file=sys.stderr)
            continue
        row.update(
            verdict=answer["verdict"],
            severity=answer.get("severity") or "",
            judged_by=answer.get("judged_by") or "ai (FIX_JUDGE_CMD)",
            judged_at=date.today().isoformat(),
        )


def rates(candidates: list[dict], judgments: list[dict], details: list[dict], as_of: date) -> list[dict]:
    """Weekly escaped-fix rate by original merge week, repo and PR type ("all" = every type)."""
    verdicts = {(row["fix"], row["original"]): row for row in judgments}
    escaped: dict[str, bool] = {}  # original -> severe
    unjudged: dict[str, int] = defaultdict(int)
    for row in candidates:
        if float(row["days_between"]) > WINDOW_DAYS:
            continue
        judgment = verdicts.get((row["fix"], row["original"]), {"verdict": "unjudged", "severity": ""})
        if judgment["verdict"] == "defect_from_original":
            escaped[row["original"]] = escaped.get(row["original"], False) or judgment["severity"] == "high"
        elif judgment["verdict"] == "unjudged":
            unjudged[row["original"]] += 1

    totals: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for pr in details:
        if pr["repo"] not in REPOS:
            continue
        merged_on = date.fromisoformat(pr["date"])
        week = merged_on - timedelta(days=merged_on.weekday())
        if week + timedelta(days=6 + WINDOW_DAYS) > as_of:
            continue  # a fix for this week's PRs could still land
        key = f"{pr['repo']}#{pr['number']}"
        for pr_type in ("all", pr["pr_type"]):
            bucket = totals[(week.isoformat(), pr["repo"], pr_type)]
            bucket[0] += 1
            bucket[1] += key in escaped
            bucket[2] += escaped.get(key, False)
            bucket[3] += unjudged.get(key, 0)
    return [
        {"week": week, "repo": repo, "pr_type": pr_type, "merged_prs": merged, "escaped_prs": hit,
         "severe_prs": severe, "unjudged_candidates": open_pairs,
         "escaped_fix_rate": f"{hit / merged:.4f}"}
        for (week, repo, pr_type), (merged, hit, severe, open_pairs) in sorted(totals.items())
    ]


def rebuild() -> None:
    candidates = read(CANDIDATES)
    judgments = sync_judgments(candidates, read(JUDGMENTS))
    judge(candidates, judgments)
    write(JUDGMENTS, JUDGMENT_HEADER, judgments)
    dates = [row["date"] for row in read(AGGREGATE)]
    if not dates:
        raise SystemExit("pr-type.csv is empty; run collect-pr-type.sh first")
    rows = rates(candidates, judgments, read(DETAILS), date.fromisoformat(max(dates)))
    write(OUTPUT, OUTPUT_HEADER, rows)
    print(f"  {len(rows)} weekly rows in {OUTPUT.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="Collect fixes merged on one YYYY-MM-DD date.")
    parser.add_argument("--from", dest="start", help="Start date for an inclusive YYYY-MM-DD range.")
    parser.add_argument("--to", dest="end", help="End date for an inclusive YYYY-MM-DD range.")
    parser.add_argument("--rebuild", action="store_true", help="Only re-judge and recompute rates (no GitHub search).")
    args = parser.parse_args()
    if args.date:
        collect(date.fromisoformat(args.date), date.fromisoformat(args.date))
    elif args.start:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end or args.start)
        if start > end:
            parser.error("--from must not be later than --to")
        collect(start, end)
    elif not args.rebuild:
        parser.error("one of --date, --from or --rebuild is required")
    rebuild()


if __name__ == "__main__":
    main()
