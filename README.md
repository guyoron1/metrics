# fullsend-ai/metrics

SDLC metrics dashboard for the fullsend-ai organization. Tracks deployment frequency, PR lead time, PR volume, issue volume, community attribution, and **PR delivery mix** (conventional-commit type + fix-filer source) across repos. Published daily as a D3.js dashboard on GitHub Pages.

**Dashboard:** https://fullsend-ai.github.io/metrics/

## PR type / fix source metrics

Dedicated tab: **[Delivered PR Types](https://fullsend-ai.github.io/metrics/delivered-pr-types.html)** (`docs/delivered-pr-types.html`).

Daily collector for merged PRs on `fullsend` + `agents`:

| File | Purpose |
|------|---------|
| `docs/pr-type.csv` | Per day × repo counts by type (`feat`…`other`) and fix filer (`fix_core` / `fix_external` / `fix_bot` / `fix_unlinked`) |
| `docs/pr-type-details.csv` | Per-PR drill-down |
| `scripts/collect-pr-type.sh` | Daily (wired into `.github/workflows/collect.yml`) |
| `scripts/backfill-pr-type.sh` | Date-range backfill |

Core-team roster comes from `docs/community-config.json` (same list as community metrics).

```bash
# Daily
./scripts/collect-pr-type.sh            # yesterday UTC
./scripts/collect-pr-type.sh 2026-08-11

# Backfill (rewrites CSVs)
./scripts/backfill-pr-type.sh 2026-05-14 2026-08-11
```

See [docs/design.md](docs/design.md) for the full design spec.

## Quality Signals

The [Quality Signals](https://fullsend-ai.github.io/metrics/quality.html) tab
reports two GitHub PR-based proxies for `fullsend` and `agents`:

- **Defect-labeled rate:** `fix` PRs linked to an issue with a defect label divided by merged PRs.
- **Revert event rate:** PRs titled `Revert ...` or typed subjects such as `fix: revert ...`, plus Git commits containing `This reverts commit`; duplicate evidence and temporary changes reverted within the same merged PR are excluded before division by merged PRs.

The defect-labeled rate is an explicit-label proxy; it does not prove
preventability or distinguish a review-escaped bug from a missed requirement.
These are leading indicators, not DORA Change Failure Rate. DORA requires
production deployment and rollback/hotfix evidence. The daily workflow derives
`docs/quality.csv` from the PR-type datasets and GitHub issue/commit metadata.

### 30-day escaped-fix rate

Also on the Quality Signals tab: of the PRs merged in a week, the share that a
later PR had to fix within 30 days. Weeks appear once their 30-day window has
closed.

| File | Purpose |
|------|---------|
| `docs/fix-candidates.csv` | Later PR → earlier PR pairs: the later PR (or the issue it closes) names the earlier PR, uses causal wording ("broke", "regression", "caused by", "introduced in"), or reverts it |
| `docs/fix-judgments.csv` | Verdict per pair (`defect_from_original`, `later_change`, `not_related`, `unknown`, or `unjudged`); seeded from an offline replay, editable by hand |
| `docs/escaped-fixes.csv` | Per week × repo × PR type: merged PRs, PRs with a confirmed fix, severe, unjudged candidates, rate |
| `scripts/collect-escaped-fixes.sh` | Daily (wired into `.github/workflows/collect.yml`, after PR types) |
| `scripts/backfill-escaped-fixes.sh` | Date-range backfill; keeps existing judgments |

Only `defect_from_original` counts. Detection alone is noisy (a named reference
is a real fix about a third of the time), so each pair needs a verdict. To
judge new pairs automatically, set `FIX_JUDGE_CMD` to a command that reads the
pair and both diffs as JSON on stdin and prints
`{"verdict": ..., "severity": ..., "judged_by": ...}`; unset, nothing is judged
and pairs stay `unjudged`. After editing judgments by hand, recompute with
`python3 scripts/collect-escaped-fixes.py --rebuild`.

The rate is a lower bound: fixes that never mention the earlier PR are missed.
Verdicts are AI judgments unless `judged_by` names a person.
