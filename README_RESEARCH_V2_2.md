# Binance Insight Research Engine v2.2

This package adds a separate research layer to the existing Strategy v2.0 application. It does **not** alter the paper or live execution rules.

## Research workflow

`3-year history → 1h source → 1h/4h tests → pre-2026 history → 2026+ validation → score bands → 90-day stability windows`

### Deep research
Choose any core pair and run either **1h** or **4h** over **2 years** or **3 years**.

### Research matrix
Runs **30 pairs × 2 timeframes = 60 studies**. The browser processes one pair at a time to reduce the chance of a long single web request timing out.

### Score-band diagnostics
The engine compares trades in these bands:

- 72–74
- 75–79
- 80–84
- 85–89
- 90+

It reports trade count, win rate, profit factor, and expectancy R. It deliberately does not present a score-band portfolio return because account equity between selected trades can be affected by trades in other bands.

### Validation split
The default split is **January 1, 2026**. Parameters remain frozen; 2026+ is reported separately from the pre-2026 period.

### Walk-forward stability
The 2026+ segment is divided into sequential 90-day windows. A window is considered evaluable when it has at least 3 trades. A positive window requires positive return, positive expectancy R, and profit factor above 1.

## Important interpretation note

Because some 2026 results have already been inspected during development, the 2026 segment should be treated as a **holdout-style validation period rather than a pristine never-seen dataset**. The strongest confirmation will come from future unseen paper results and later rolling validation without changing the rules in response to the same data.
