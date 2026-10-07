#!/usr/bin/env bash
# Memo 21g: the paper test's nightly reading. Run once a night at 00:05 UTC from the repo root, e.g.
#   5 0 * * *  cd /path/to/agent-2-playground && bash scripts/paper_nightly.sh >> data/paper/nightly.log 2>&1
# It appends tonight's lines to data/paper/snapshots.jsonl, scores what has resolved so far, and
# commits the record. No key, no money: the paper command only reads public data.
set -u
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
date -u +"%Y-%m-%dT%H:%M:%SZ paper nightly"
python -m pm_scanner paper --snapshot --dir data/paper
python -m pm_scanner paper --score --dir data/paper
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git add data/paper >/dev/null 2>&1
  git commit -q -m "paper: $(date -u +%Y-%m-%d) snapshot and score" && git push -q || echo "paper: nothing to commit or push failed"
fi
