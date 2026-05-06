#!/usr/bin/env bash
# Phase 1.4 — one-shot helper to register the bulky directories with DVC and
# push them to the TrueNAS remote.
#
# Run from repo root. Idempotent on the directories DVC already tracks
# (dvc add re-hashes only changed files); safe to re-run after new campaigns
# land. The .dvc tracking files (references.dvc, campaigns.dvc) are committed
# to git; the data itself stays gitignored and lives on TrueNAS.

set -euo pipefail

# Resolve repo root from the script location so we don't depend on cwd
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v dvc >/dev/null 2>&1; then
    echo "[dvc_track] dvc not found — activate venv: source venv/bin/activate" >&2
    exit 1
fi

if [ ! -d .dvc ]; then
    echo "[dvc_track] .dvc/ missing — run 'dvc init' first" >&2
    exit 1
fi

# Defaults match Phase 1.4 ROADMAP scope: references/ (RAG docs) and
# campaigns/ (per-campaign evidence crates). Override via PATHS env if a
# downstream caller wants to track something else (e.g. datasets/).
TRACK_PATHS="${PATHS:-references campaigns}"

echo "[dvc_track] hashing + adding: $TRACK_PATHS"
for p in $TRACK_PATHS; do
    if [ ! -e "$p" ]; then
        echo "[dvc_track] skipping $p (not present)"
        continue
    fi
    dvc add "$p"
done

echo "[dvc_track] pushing to remote: $(dvc remote default)"
dvc push

echo
echo "[dvc_track] done. Suggested commit:"
echo "    git add ${TRACK_PATHS//$'\n'/ }*.dvc .gitignore"
echo "    git commit -m 'dvc: track $(echo "$TRACK_PATHS" | tr ' ' ',') @ $(date -u +%Y-%m-%dT%H:%M:%SZ)'"
