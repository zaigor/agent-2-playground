# Working rules for this repository

`pm_scanner/lp.py` is the only code that can move money. Read MEMO.md section 18
and section 20 before changing it or advising a live run.

- **Branch.** All work on `claude/prediction-market-monetization-kq8bj4`; never
  push elsewhere; no pull request unless asked. Commit and push before ending
  a turn.
- **Secrets.** Keys live only in the environment (`.env.example` names them),
  never in a file in the repo and never in a log. The signer is a dedicated
  wallet that holds only the test budget. `MAX_BUDGET_USD` is the hard cap.
- **Money numbers are readings, not estimates.** What a position is worth is
  what the bids pay: `python -m pm_scanner lp --positions`. The site's midpoint
  mark is not money, and neither is a figure from memory. Before advising a
  sale, an exit or a size, run that command or ask for the book, and if the
  reading is not available say so instead of estimating. (4 Oct 2026: "under a
  dollar" to exit was really $2.45 by the bids, and $4.63 an hour later.)
- **A live `lp` run** names its market with `--only` from a dry run made minutes
  before, and its plan's `exit $` column is under `--max-exit`. Raising the cap
  is the user's call, written in the command, never a default changed in code.
- **Public information only.** Nothing from inside a resolution source, no VPN,
  no terms-of-service circumvention.
- **Record outcomes in MEMO.md** the day they happen: fills, disposals, final
  P&L, and what was changed after the fact, marked as not pre-registered.
