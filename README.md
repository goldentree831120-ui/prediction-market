# Prediction Market & Market Intel Lab

Status: **M1** binary-outcome market, **M2** constant-product pricing, **M3** read-only data
pipeline (Rust), **M4** calibration study and **M5** news event study done. Next: M6 dashboard + note.

```bash
forge test                      # 44 unit/fuzz tests + 9 invariants (market and pool)
forge test -vv --match-test tradeoff   # prints the LP trade-off numbers quoted below
./script/mutation-check.sh      # plants 15 bugs (6 market, 9 pool); the suite must catch each
(cd pipeline && cargo test)     # data fetcher tests (recorded fixtures; no network)
(cd pipeline && ./run.sh)       # fetch the dataset from Manifold's public API (read-only, no key)
```
(OpenZeppelin v5.1.0 and forge-std are included in `lib/`. On Windows use WSL or Git Bash.)

## M1: `BinaryMarket`

One yes/no question on a test ERC-20 collateral.

- **Complete sets.** `mintSets(x)`: deposit x collateral, get x YES + x NO. `redeemSets(x)`:
  return x of each, get x back, in any phase. A set is always worth exactly 1, so the
  market is solvent without knowing any price.
- **Lifecycle.** Open until `closeTime`. Then the **resolver** proposes Yes, No or Invalid.
  During the **dispute window** anyone can dispute by posting a bond. An undisputed proposal
  is finalized by anyone after the window. A disputed one goes to the **arbiter**: if
  overturned the disputer gets the bond back, if upheld it goes to the resolver.
- **Payout.** After Final, `redeem()` burns all your YES and NO: Yes pays 1 per YES, No pays
  1 per NO, Invalid pays 0.5 per token (rounded down).
- **Liveness.** If the resolver never proposes (30 days after close) or the arbiter never
  rules (30 days after the window), anyone can finalize as Invalid and the bond is returned.
  Funds can't be locked forever by a silent role.
- **Fee-on-transfer collateral.** Sets are minted for what actually arrived, not what was asked.

### What the tests prove

| Invariant | Meaning |
|---|---|
| `collateralCoversOutstandingSets` | balance >= what holders could claim right now + any dispute bond (the brief's invariant) |
| `setsStayPairedUntilFinal` | YES supply == NO supply until resolution |
| `collateralAccounting` | balance == deposits + bonds in − redemptions − payouts − bonds out, exactly |
| `noOverpayment` | no redeem ever paid more than the tokens were worth (checked by an independent formula) |
| `roundingDustIsBounded` | rounding strands at most 1 wei per Invalid redemption, always in the market's favour |

The handler mints, trades YES and NO between four users (so balances become uneven),
redeems sets, and drives proposals, disputes, arbitration, finalization and payouts in
random order. `docs/mutation-report.txt`: all six planted bugs are caught.

### Trust and limitations (honest list)

- The **resolver and arbiter are trusted.** The dispute window and bond make a wrong report
  cost something and give time to react; they do not make the oracle trustless. A real system
  would use an optimistic oracle (e.g. UMA) or a decentralized arbiter.
- One market per contract, no factory, no fees, no trading mechanism yet (M2).
- The dispute bond is fixed and the first disputer wins the slot; a richer design would let
  bonds escalate.
- Minting after close is blocked (people would mint once the answer is known); trading the
  outcome tokens after close is not, because they are ordinary ERC-20s.

## M2: `FixedProductMarketMaker` (constant-product pricing)

A pool of YES and NO tokens for one market, the design Gnosis introduced and Polymarket used for
its first markets. The pool holds `y` YES and `n` NO and never lets `y * n` fall on a trade.

- **Price = probability.** Price of YES = `n / (y + n)`, price of NO = `y / (y + n)`; they always
  sum to 1. The first liquidity provider can set the opening odds (1%–99%).
- **Buying YES with c:** the pool mints c complete sets through the market (c YES + c NO), keeps
  the NO, and pays out YES until `y * n` is back where it was. **Selling** is the reverse: the pool
  takes YES and redeems complete sets for the collateral paid out.
- **Fee** (2% in the tests, max 10%): it stays in the pool as extra YES and NO, so `y * n` grows and
  liquidity providers own the growth. No separate fee accounting.
- **Liquidity** is an ERC-20 share. Joining keeps the current odds (the side the pool doesn't need is
  returned); leaving pays out the same fraction of YES and NO, at any time, including after
  resolution, when the tokens redeem through the market. The first 1000 wei of shares are locked
  forever, as in Uniswap v2.
- **Trading stops when the market closes.** Every rounding goes in the pool's favour, and every
  trade has a slippage limit.

### Why a constant-product pool, and what it costs

| | Constant-product pool (chosen) | Order matching (on-chain order book) |
|---|---|---|
| Can someone trade right now? | always, at some price, as long as there is any liquidity | only if someone has posted an order on the other side |
| Price for a large trade | moves along the curve (slippage), set by pool size | exact price levels, if the depth is there |
| Who takes the risk | liquidity providers, passively | market makers who choose their quotes |
| Gas / complexity | one formula, constant cost | order storage, matching loops, cancellation |
| Fit with P1 | new skill (AMM design) | repeats the Rust order book from P1 |

The cost is real, and the tests measure it (deterministic, same numbers on any machine):

- **Informed flow hurts the LP.** With 1000 of liquidity at 50%, a trader who *knows* YES will win
  buys 3300 worth: the price moves to 93.4%, the trader receives 3997 YES and redeems 3997. The LP
  gets back **302 of its 1000**. A pool can't tell an informed trader from anyone else; this is
  adverse selection, and it's why prediction-market liquidity is often subsidised.
- **Uninformed two-way flow pays the LP.** 20 buy-then-sell round trips of 50 leave the LP's
  position worth **1039** (from 1000) through fees.
- An LMSR market maker (Hanson) would bound the LP's worst-case loss in advance (`b · ln 2`) and is
  the classic alternative. It needs exp/log in fixed point; the pool here needs only multiplication
  and division, which keeps the code small and the rounding provable.

### What the tests prove (pool)

| Invariant | Meaning |
|---|---|
| `shareValueNeverDecreases` | after every trade, join, leave or resolution, `sqrt(y·n)` per share never goes down |
| `poolHoldsNoCollateral` | every unit the pool receives becomes complete sets; nothing sits loose |
| `marketStaysSolvent` | the M1 guarantee still holds with the pool trading: the market can pay every holder |
| `poolNeverRunsDry` | while shares exist both sides are non-empty (price strictly between 0 and 1); YES and NO stay paired until resolution |

Unit and fuzz tests check the exact constant-product amounts against a hand calculation for both
buy and sell, that buying then selling straight back never returns more than was paid (1000 random
cases, any opening odds), that quotes equal execution, slippage limits, a front-run, no trading
after close, the full lifecycle through redemption, and fee-on-transfer collateral being refused.

**What testing found:**
- The invariant `shareValueNeverDecreases` caught a rounding bug in the first version:
  joining liquidity rounded the amounts the pool keeps *down*, so a new provider could dilute
  existing ones by a tiny amount. They now round up.
- The mutation check found that the first test suite didn't notice if `sell` skipped its fee.
  `test_sellChargesTheFee_exactAmount` now pins the exact amount.
- Slither flagged strict equalities on reserves (`y == 0`), which can be influenced by donations.
  That led to checking the **first-depositor (inflation) attack**: hold 1 share, donate to inflate
  it, make the next provider's shares round down. With the locked 1000 shares,
  `test_inflationAttack_costsTheAttackerFarMoreThanTheVictim` measures it: the victim loses 4 bps
  (0.04%) of a 10,000 deposit, and the attacker loses about 2000 times as much. The victim's
  `minShares` argument can refuse even that.

### Limitations

- One pool per market, no router, no oracle. Liquidity providers should expect to lose to informed
  traders near resolution (above).
- Trading needs standard ERC-20 collateral; fee-on-transfer tokens can provide liquidity but not trade.
- The pool's price is only as good as the trading in it: M4 measures how well real prediction
  markets' prices match outcomes.

## M3: data pipeline (`pipeline/`, Rust)

`pm-fetch` collects resolved yes/no prediction markets and the full trading history of each,
for the calibration study (M4) and the news study (M5).

```bash
cd pipeline
./run.sh                                  # default: markets created 2025-01-01 .. 2025-07-01
./run.sh --since 2024-07-01 --until 2025-07-01 --min-bettors 20
./run.sh --clean                          # delete cached and derived data, rebuild from scratch
```

Output in `pipeline/data/clean/`:
- `markets.csv`: one row per market: question, created / close / resolution times, resolution
  (YES or NO), unique bettors, volume, liquidity, number of bets.
- `bets.csv`: every bet, oldest first: time, outcome, amount, and the market probability
  **before and after** it. That gives each market's probability over time.
- `manifest.json`: the selection rules, how many markets were seen, kept, and excluded **for each
  reason** (not binary, unresolved, resolved to a probability, cancelled, too few bettors, ...),
  bets dropped for impossible probabilities, any market whose history was cut short, the number of
  requests made, and SHA-256 hashes of both CSV files.

**How it behaves:**
- GET requests only, no API key, no account. A descriptive User-Agent with the repository link.
- At most 4 requests a second, half of Manifold's documented 500 per minute, with backoff on
  HTTP 429 and 5xx (honouring `Retry-After`).
- Every raw response is cached on disk with its URL and fetch time, so reruns are fast and give
  identical results, and every number can be traced back to a response. `--refresh` refetches;
  `--offline` never touches the network.
- If more markets qualify than `--max-markets` (default 1500), a deterministic pseudo-random sample
  is taken (by a hash of the market id), so it spreads over the whole window instead of favouring
  the oldest or newest markets. The manifest records that the cap was hit.
- The data are **not committed** to this repository (`data/` is git-ignored). Anyone can rebuild
  them with `./run.sh`.

**Tests** (`cargo test`, also in CI): paging stops at the window and at the page cap; every
excluded market is counted under exactly one reason; bets come back oldest first across pages;
truncated histories are reported; invalid probabilities are dropped and counted; the CSV and
manifest contents; and the HTTP layer against a local test server: GET only, caching, offline
mode, retry after a 429, and request spacing. The tests use recorded responses shaped like the
documented API types, so CI never calls Manifold.

### Data sources and terms

Checked before writing any code (October 2026). Not legal advice.

| Source | Decision | Reason (from the source's own terms) |
|---|---|---|
| **Manifold Markets** API | ✅ used | read endpoints need no key; the API docs allow academic, personal and non-commercial use; limit 500 requests/min per IP |
| Polymarket | ❌ not used | Terms of Use prohibit "data mining tools, robots, crawlers, or similar data gathering and extraction tools" and copying material without prior written consent |
| Kraken | ❌ not used | Terms of Service allow use "only for your own benefit", prohibit scraping, bots and scripts, and making content available to third parties; other uses need permission (marketdata@kraken.com) |
| Coinbase Exchange | ❌ not used | Market Data Terms prohibit sharing derived works (charts, analytics, research) outside your organisation without consent |
| Kalshi | ❌ not used | Data Terms allow personal non-commercial use only and prohibit publishing, derivative works and systematic retrieval |

The consequence: this project has no exchange price data, and the brief's ccxt step was dropped.
The news study (M5) measures how **Manifold probabilities** move around public news instead.

**Main limitation: Manifold uses play money (mana).** Traders have less at stake than on
real-money markets, so results describe play-money markets and may not carry over to real-money
ones.

## M4: calibration study (`analysis/`, Python)

When these markets said 70%, did the event happen about 70% of the time?

```bash
cd analysis && ./run.sh          # sets up analysis/.venv on first use, runs the tests, then the study
```

**Method.** The price just before resolution is nearly useless to score: by then the answer is often
known (a "will X happen by date D" market resolves YES the moment X happens). So each market's
probability is taken at moments fixed in advance, using only bets made at or before that moment:

| Snapshot | When |
|---|---|
| `open` | the opening probability, set by the market's creator before any trading |
| `mid` (main result) | halfway between creation and the end of trading |
| `30d`, `7d`, `1d` | 30, 7 and 1 day(s) before trading ended; only markets that traded that long |

For each snapshot: the Brier score with a bootstrap 95% CI (2000 resamples, fixed seed); log loss;
two naive baselines: "always 50%" (Brier 0.25) and "always the base rate", where the base rate is
learned only from the markets that ended **earlier** and scored on the later half, so the baseline
can't see the future; a reliability table (10 bins, Wilson 95% CIs, number of markets per bin);
expected calibration error; and the Murphy decomposition (reliability, resolution, uncertainty).
Breakdowns by number of bettors and by trading lifetime.

**Checks.** 13 tests on hand-built data with known answers (a bet made after a snapshot is never
used; a perfectly calibrated synthetic forecaster scores as calibrated; an overconfident one is
caught; the baseline can't learn from later markets), and `mutation_check.py` plants 8 classic
mistakes, including look-ahead, a baseline that sees the whole sample, and a flipped outcome. The
tests must catch every one; CI runs both.

**Results** (3,202 play-money markets created Jan–Jun 2025, 417,387 bets; full write-up in
[`analysis/FINDINGS.md`](analysis/FINDINGS.md), tables and charts in [`analysis/results/`](analysis/results/)):

- At the midpoint of their lives the markets score a Brier of **0.152** (95% CI 0.145–0.159), against
  0.25 for "always 50%" and about **42% better** than a base rate learned from earlier markets.
- Calibration error is small (ECE **0.037**), but **YES is overpriced in all 10 probability bins**, by
  1–7 percentage points (sign test p ≈ 0.002). Markets priced near 95% resolved YES 89.6% of the time.
- Accuracy improves toward the end of trading (Brier 0.112 at 30 days, 0.078 at 1 day; different
  subsets of markets, so indicative only).
- Play money, one six-month window, markets with 10+ bettors only; no strategy is claimed.

## M5: news event study (`analysis/`, Python)

Do Manifold probabilities move when the news does, and do they move **before** it?

```bash
cd analysis && ./run_news.sh --limit 1   # quick check: one market's news from GDELT
cd analysis && ./run_news.sh             # all 49 markets (one request each, cached), then the study
```

**Markets.** 49 hand-picked markets from the M3 sample ([`analysis/news/markets.csv`](analysis/news/markets.csv)),
each clearly about a news event (wars and ceasefires, tariffs, court cases, elections, launches),
across 15 topics, each with a hand-written news search. Price-driven (Bitcoin, stocks), poll-driven
and personal or joke markets were left out.

**News.** Daily counts of English-language articles matching each search, from the
[GDELT Project](https://www.gdeltproject.org) DOC 2.0 API (free to use with citation), for each
market's last 90 days of trading plus two weeks before for the baseline: one request per market,
at least 10 s apart, cached, with backoff; no key. (Hourly counts were tried first; they need a
request per week of data, about 400 in all, and GDELT refused nearly all of them with HTTP 429.)

**Method (rules fixed before the run, in `news_study.py`).** A *news-spike day* has at least 20
matching articles and at least 3× the median of the previous 14 days; spike days within 2 days of
a kept one are the same story. The *reaction* is how far the probability moved from 00:00 UTC on
the spike day to two days later; the *lead* is how far it moved in the two days before. Both are
compared with *normal days* in the same market (more than 2 days from any spike) using a one-sided
permutation test, then Benjamini–Hochberg across markets (q = 0.10). Markets on the same topic
share news, so the pooled effect is averaged over topics, with a bootstrap over topics.

**Checks.** 20 more tests (spike rule, no look-ahead in the baseline, gaps in the data, de-duplication,
BH on a known example, a synthetic market that reacts to news is detected, and markets that don't
are flagged at about the 5% chance rate, the fetcher's caching, backoff, zero-filling and refusal
to cache GDELT error messages), and 11 more planted mistakes in the mutation check (19 in total).

**Results** (24 of 49 markets testable, 142 news-spike days; full write-up in
[`analysis/NEWS_FINDINGS.md`](analysis/NEWS_FINDINGS.md), table and chart in [`analysis/results/`](analysis/results/)):

- Over a news-spike day and the day after, probabilities moved **3.1 points** more than over ordinary
  days (mean over 13 topics, 95% CI −0.1 to 8.1); significant in **9 of 24** markets after BH.
- They moved about as much in the **two days before** a spike (**4.0 points**, CI 0.5 to 8.5; 7 of 24
  significant). Daily coverage peaks seem to lag the events traders react to, so this shows prices
  and coverage moving together, not which comes first.

**Limits, stated up front.** 49 markets is a small, hand-picked sample, and markets that traded only
a few days can't be tested; news searches are imperfect; article counts measure coverage, not
importance; daily counts can't say whether the news came before or after a move within the same
day; and this describes how play-money prices behave around news. It is not a trading strategy.
