#!/usr/bin/env bash
# run.sh — single-phase dispatcher for the folder-reorg pipeline.
# For an interactive end-to-end run with prompts and state, use ./run.py.
#
# Usage: ./run.sh <phase> [pipeline args...]
#        ./run.sh -h | --help

set -euo pipefail
cd "$(dirname "$0")"

usage() {
  cat <<'EOF'
run.sh — single-phase dispatcher for the folder-reorg pipeline

USAGE
  ./run.sh <phase> [pipeline args...]
  ./run.sh -h | --help

  Runs ONE phase of the pipeline by number. Pipeline args (like --source,
  --target, --limit) are passed straight through to the underlying Python
  module. No prompts, no state tracking, no subset discovery.

  For the interactive wizard (end-to-end run with prompts + state), use:
    ./run.py [--subset ...] [--nas-name ...] [--resume]

PHASES
  0        phase0_manifest       — SHA-256 baseline of the source
  1 | 1a   phase1_inventory      — walk source, write inventory.csv
  1b       phase1_extract        — extract first ~4000 chars per file
  1c       phase1_lang_detect    — language detection (lingua)
  2 | 2a   phase2_embed          — bge-m3 embeddings on GPU
  2b       phase2_cluster        — HDBSCAN → cluster_assignments.csv
  3        phase3_classify       — LLM cluster + per-file naming → rename_plan.csv
  5        phase5_execute        — copy source → target per approved plan
  6        phase6_verify         — counts + hash check + convention lint

GROUPED SHORTCUTS
  all-up-to-3   — runs 0, 1a, 1b, 1c, 2a, 2b, 3 in order. Stops for review.
  all           — same as all-up-to-3 plus reminders for Phase 5/6.

NOTES
  · Phase 4 (Streamlit review) is launched via the wizard (./run.py) or by
    hand:  streamlit run review_ui/review_ui.py -- --plan data/rename_plan.csv
  · Phase 7 (rsync target_local to NAS) is done by hand or via ./run.py —
    this dispatcher does not handle it.
  · The venv at .venv/bin/activate is sourced automatically if present.

EXAMPLES
  ./run.sh 0 --source ./source_local/F-Finance
  ./run.sh 3 --source ./source_local/F-Finance --limit 50
  ./run.sh 5 --plan data/rename_plan_approved.csv --target ./target_local/F-Finance
  ./run.sh all-up-to-3 --source ./source_local/F-Finance
EOF
}

# Handle help before anything else (before venv activation, before shift).
case "${1:-}" in
  -h|--help|help|"") usage; exit 0 ;;
esac

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

PHASE="$1"; shift

case "$PHASE" in
  0)       python -m src.phase0_manifest      "$@" ;;
  1|1a)    python -m src.phase1_inventory     "$@" ;;
  1b)      python -m src.phase1_extract       "$@" ;;
  1c)      python -m src.phase1_lang_detect   "$@" ;;
  2|2a)    python -m src.phase2_embed         "$@" ;;
  2b)      python -m src.phase2_cluster       "$@" ;;
  3)       python -m src.phase3_classify      "$@" ;;
  5)       python -m src.phase5_execute       "$@" ;;
  6)       python -m src.phase6_verify        "$@" ;;
  all-up-to-3)
    python -m src.phase0_manifest    "$@"
    python -m src.phase1_inventory   "$@"
    python -m src.phase1_extract
    python -m src.phase1_lang_detect
    python -m src.phase2_embed
    python -m src.phase2_cluster
    python -m src.phase3_classify
    ;;
  all)
    python -m src.phase0_manifest    "$@"
    python -m src.phase1_inventory   "$@"
    python -m src.phase1_extract
    python -m src.phase1_lang_detect
    python -m src.phase2_embed
    python -m src.phase2_cluster
    python -m src.phase3_classify
    echo
    echo "==> Phase 3 done. Review rename_plan.csv, then:"
    echo "    ./run.sh 5 --target ./target_local/<SUBSET>"
    echo "    ./run.sh 6 --source ./source_local/<SUBSET> --target ./target_local/<SUBSET>"
    ;;
  *)
    echo "Unknown phase: $PHASE" >&2
    echo
    usage
    exit 1
    ;;
esac
