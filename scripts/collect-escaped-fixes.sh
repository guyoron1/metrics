#!/usr/bin/env bash
# Daily escaped-fix candidates + 30-day rate, after collect-pr-type.sh has run.
# Usage: ./scripts/collect-escaped-fixes.sh [YYYY-MM-DD]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DATE="${1:-$(date -d yesterday +%Y-%m-%d)}"
echo "Collecting escaped-fix candidates for ${TARGET_DATE}..."
python3 "${SCRIPT_DIR}/collect-escaped-fixes.py" --date "$TARGET_DATE"
