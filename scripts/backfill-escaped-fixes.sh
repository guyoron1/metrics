#!/usr/bin/env bash
# Backfill escaped-fix candidates across a date range (keeps existing judgments).
# Usage: ./scripts/backfill-escaped-fixes.sh [START] [END]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Same start as the PR-type data, which supplies the merged-PR denominator.
START_DATE="${1:-2026-05-14}"
END_DATE="${2:-$(date -d yesterday +%Y-%m-%d)}"
echo "Backfilling escaped-fix candidates from ${START_DATE} to ${END_DATE}..."
python3 "${SCRIPT_DIR}/collect-escaped-fixes.py" --from "$START_DATE" --to "$END_DATE"
