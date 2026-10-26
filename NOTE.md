# Prediction Market & Market Intel Lab: what worked, what didn't

A binary prediction market on a test ERC-20, a constant-product pool to price it, a read-only
pipeline for public Manifold data, two studies on that data and a local dashboard. Everything
runs locally or on a test chain; no real money, no trading, no authenticated endpoints.

## What was built

| Part | Result |
|---|---|
| `BinaryMarket` + `FixedProductMarketMaker` (Solidity) | 46 unit/fuzz tests + 9 invariants; 15 of 15 planted bugs caught |
| Rust fetcher (`pipeline/`) | 3,202 resolved markets and 417,387 bets from Manifold's API, rate-limited, cached, reproducible |
| Calibration study (M4) | Brier 0.152 at mid-life, ~42% better than an out-of-sample base rate; YES overpriced in all 10 bins |
| News study (M5) | on 24 testable markets, larger moves around news-coverage spikes, but as large *before* them |
| Dashboard (M6) | Streamlit, local only; pool calculator verified against the contract to the wei |
| Analysis checks | 42 Python tests, 22 of 22 planted analysis mistakes caught; CI runs all three parts on every push |

## What worked

- **Terms first.** Checking each data source's terms before writing code ruled out Polymarket,
  Kraken, Coinbase and Kalshi early. Manifold (allowed) and GDELT (free with citation) were enough.
- **Invariant tests and planted bugs.** The invariant suite found a real rounding bug in the pool
  (joining liquidity could dilute existing shares) and Slither led to the inflation-attack fix.
  Planting bugs on purpose showed which tests actually guard what, in the contracts and in the
  analysis code (look-ahead, a baseline that sees the future, the wrong multiple-testing rule).
- **Rules fixed before the data.** Snapshots, the spike rule and the comparison windows were
  written down and tested on synthetic data before the real run, so the results couldn't steer them.
- **Checking the "before" window.** Measuring price moves before the news as well as after is what
  stopped the news study from claiming "markets react to news" when the timing can't support it.
- **One source of truth for maths shared across languages.** The dashboard's pool calculator and
  the contract are tested against the same numbers.

## What didn't

- **Real-money data.** No exchange or real-money market data could be used under their terms, so
  every result is about play money.
- **Hourly news.** GDELT refused almost all of the ~400 hourly requests (HTTP 429), so the news
  study fell back to daily counts. That loses the order of events within a day, which is exactly
  what the "before vs after" question needs.
- **Half the news sample.** 25 of 49 hand-picked markets couldn't be tested: their whole short life
  was news, leaving no quiet days to compare against.
- **Opening prices.** Every market in the sample opened at 50%, so that snapshot carried no
  information.
- **Causal questions.** Breakdowns by bettors and lifetime are confounded with question difficulty
  and base rate; the study reports them but can't explain them.

## If I did it again

Fetch news at hourly resolution over many days with a slower, scheduled job (or use GDELT's raw
files), pick news markets before seeing which ones had coverage, and compare against a real-money
market where terms allow.

**No strategy is claimed.** A few points of YES overpricing and price moves around news are
descriptive findings about one play-money sample, not trading signals.
