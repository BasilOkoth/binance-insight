# Binance Insight Candidate Validation Lab v2.3

## Purpose
Keep Strategy v2.0 as an untouched control while testing whether the research candidates and the `score >= 80` hypothesis survive stricter validation.

## Candidate validation set
- INJUSDT — 1h
- ATOMUSDT — 1h
- ATOMUSDT — 4h
- TRXUSDT — 4h

Each candidate is replayed at thresholds `72, 75, 78, 80, 82, 85`. The simulation is rerun independently for every threshold, so removing a low-score trade can change later position availability and account equity. This is more valid than filtering an already-completed trade list.

`80+` is deliberately labelled the **forward hypothesis** because it was proposed before this grid was built. The grid is exploratory and must not be treated as fresh out-of-sample proof.

## Parallel paper experiment
A new paper account named `candidate-v23` is separate from the existing `primary-v2` account.

Candidate paper rules:
- INJUSDT and ATOMUSDT only
- 1h completed candles
- score >= 80
- same Strategy v2 quality guards
- same configured fees/slippage/risk sizing
- stop and target protection in paper simulation
- maximum hold: 24 hours
- separate trade metadata/version

The existing Strategy v2.0 live gate ignores Candidate v2.3 trades.

## Why no migration
The new experiment reuses `PaperAccount`, `Trade`, and `BacktestRun`. Experiment-specific fields are stored in the existing JSON metadata/results fields.
