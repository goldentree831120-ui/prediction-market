# Prediction Market & Market Intel Lab

Status: **M1** binary-outcome market and **M2** constant-product pricing done. Next: M3
read-only data pipeline, M4 calibration study, M5 news event study, M6 dashboard + note.

```bash
forge test                      # 44 unit/fuzz tests + 9 invariants (market and pool)
forge test -vv --match-test tradeoff   # prints the LP trade-off numbers quoted below
./script/mutation-check.sh      # plants 15 bugs (6 market, 9 pool); the suite must catch each
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
