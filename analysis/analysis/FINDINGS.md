# Are Manifold's probabilities calibrated? (P4 M4)

**Data:** 3,202 resolved yes/no Manifold markets created 1 January – 30 June 2025, play money
(mana), each with at least 10 unique bettors, and their 417,387 bets. Fetched with `pipeline/`
(fetch time and SHA-256 hashes in `pipeline/data/clean/manifest.json`, not committed). Analysis
run on an Intel Core i5-10210U laptop with 8 GB RAM (WSL Ubuntu, Python 3.12). Full tables: [`results/calibration.md`](results/calibration.md); charts:
[`results/reliability.png`](results/reliability.png), [`results/brier_by_snapshot.png`](results/brier_by_snapshot.png).
Method: see the README's M4 section. Every number below comes from that run.

## What the numbers say

**1. Halfway through their lives, these markets are far better than naive forecasts.**
At the midpoint the Brier score is **0.152** (95% CI 0.145–0.159), against 0.25 for "always 50%".
On the later half of markets, against a base rate learned only from the earlier half, the market
scores **0.133 vs 0.231**: a difference of −0.098 (95% CI −0.107 to −0.089), a Brier skill of
about **42%** over the base rate.

**2. They are fairly well calibrated, but YES is consistently overpriced.**
The expected calibration error at the midpoint is **0.037**. But in **all 10** probability bins,
fewer markets resolved YES than the price implied:

| Price (bin mean) | 5% | 15% | 25% | 35% | 45% | 54% | 65% | 75% | 85% | 95% |
|---|---|---|---|---|---|---|---|---|---|---|
| Resolved YES | 2.3% | 10.4% | 22.0% | 33.7% | 37.6% | 51.6% | 61.3% | 70.2% | 78.9% | 89.6% |

The gap is 1–7 percentage points. In 5 bins the 95% interval excludes the price; in the other 5
it doesn't, but the direction is the same. If prices were unbiased, all 10 independent bins landing
on the same side would happen with probability about 0.002 (sign test, two-sided). For example,
markets priced around 95% resolved YES 89.6% of the time (95% CI 84.7–93.0%).

**3. Accuracy improves as the end approaches, as it should.**
Brier 0.112 at 30 days before the end of trading, 0.097 at 7 days, 0.078 at 1 day. These groups
are **not the same markets** (only markets that traded at least 30 days have a 30-day snapshot,
and their YES rate is lower: 29% vs 36%), so the trend is indicative, not a like-for-like comparison.

**4. The opening price carries no information here.** Every market in the sample opened at
exactly 50% (Brier 0.2500 with a zero-width interval), so the "open" snapshot is just the
"always 50%" baseline.

## What the breakdowns do *not* show

- **More bettors, worse Brier** (10–19 bettors: 0.142; 50+: 0.176, non-overlapping intervals).
  This does **not** mean crowds make worse forecasts. The 50+ markets have a higher YES rate
  (41% vs 36%) and are plausibly the harder, more contested questions; popularity and difficulty
  are confounded, and this study can't separate them.
- **Longer-lived markets score better** (< 7 days: 0.185; > 30 days: 0.137). Their YES rates
  differ a lot (53% vs 29%), which by itself changes the achievable Brier score. Also confounded.

## Limits

- **Play money.** Mana has no cash value, so traders have less at stake than on real-money
  markets. These results describe Manifold's play-money markets, not prediction markets in general.
- **One sample.** Markets created in one six-month window, with at least 10 bettors. Smaller,
  quieter markets are excluded and may behave differently.
- **Who resolves.** Manifold markets are usually resolved by their creator. Resolution errors or
  bias would show up here as miscalibration.
- **Snapshots.** "Midpoint" and "N days before the end of trading" are choices made in advance;
  other choices (time-weighted averages, fixed calendar dates) can give different numbers.
- **No strategy claim.** A YES bias of a few points does not imply a profitable strategy: fees,
  liquidity, price impact and the fact that this is one historical sample all stand in the way,
  and none of that was tested.

## What worked, what didn't

- **Worked:** measuring at pre-chosen snapshots instead of just before resolution; a baseline
  that can't see the future; tests and a mutation check aimed at exactly those mistakes. The
  full-size dry run on synthetic data also caught a real chart bug (an error bar at 100% that
  was negative by a rounding error) before the real run.
- **Didn't:** the "open" snapshot turned out to be uninformative (every market opens at 50%), and
  the breakdowns can't be read causally. A stronger study would compare against real-money
  markets, which the terms check ruled out for now.
