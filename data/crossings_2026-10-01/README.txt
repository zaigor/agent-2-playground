Bracket-crossing test of the count-window model against outcomes (memo section 19e), 1 Oct 2026.
Seven posts series: trump-truth-social, whitehouse-daily-tweets, khamenei-daily-tweets, zelenskyy-tweets,
ted-cruz-daily-tweets, nycmayor-tweets, cz-tweets. Events from Gamma (closed series events), prices from the
full trade tapes (data-api /trades).

  A_*  crossings dated by the bracket's UMA close (pre-registered); B_*  dated by its price collapse (post hoc)
  *_lower   count = the lower bound (largest crossed ceiling + 1); *_interval  spread up to the next live bracket
  *_stale2h / *_stale72h  the price must be a print within 2 h / 72 h of the decision time (default 24 h)
  *_signal.csv  the (market, time, p, note) rows: re-score with  python -m pm_scanner signal --csv FILE

musk_*  the same two tests on elon-tweets and elon-tweets-48h (their signal rows, 31,000 each, are not kept: regenerate with the command below)

Command:  python -m pm_scanner crossings --series <slugs> --timing {close,collapse} --count {lower,interval} --max-stale H --fill-wait 6
