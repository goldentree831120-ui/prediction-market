"""Tests on small hand-built data with known answers. No network, no real data needed."""

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import calibration as cal  # noqa: E402

DAY = cal.DAY_MS


# ---- probability at a time: no look-ahead -------------------------------------------------


def test_prob_at_uses_only_bets_at_or_before_t():
    t = np.array([100.0, 200.0, 300.0])
    before = np.array([0.5, 0.6, 0.7])
    after = np.array([0.6, 0.7, 0.9])
    assert cal.prob_at(t, before, after, 50) == 0.5, "before the first bet: the opening probability"
    assert cal.prob_at(t, before, after, 100) == 0.6, "a bet exactly at t counts"
    assert cal.prob_at(t, before, after, 299) == 0.7, "the bet at 300 is in the future"
    assert cal.prob_at(t, before, after, 10_000) == 0.9
    assert math.isnan(cal.prob_at(np.array([]), np.array([]), np.array([]), 5))


def _market(mid, created, end, resolution="YES", bettors=20, close=None):
    return {
        "id": mid,
        "question": "q",
        "url": "",
        "created_time": created,
        "close_time": end if close is None else close,
        "resolution_time": end,
        "resolution": resolution,
        "unique_bettors": bettors,
        "volume": 0,
        "total_liquidity": 0,
        "mechanism": "cpmm-1",
        "token": "MANA",
        "n_bets": 0,
        "bets_truncated": False,
    }


def _bet(mid, bid, t, pb, pa):
    return {"market_id": mid, "bet_id": bid, "created_time": t, "prob_before": pb, "prob_after": pa}


def test_snapshots_pick_the_right_moments_and_skip_inapplicable_horizons():
    markets = pd.DataFrame(
        [
            _market("long", 0, 40 * DAY),  # 40 days of trading
            _market("short", 0, 10 * DAY, resolution="NO"),  # 10 days: no 30d snapshot
        ]
    )
    bets = pd.DataFrame(
        [
            _bet("long", "a", 1 * DAY, 0.30, 0.40),  # before the 30d mark (day 10)
            _bet("long", "b", 15 * DAY, 0.40, 0.55),  # before mid (day 20)
            _bet("long", "c", 35 * DAY, 0.55, 0.80),  # before 1d (day 39), after 7d (day 33)
            _bet("long", "d", 39.5 * DAY, 0.80, 0.99),  # after the 1d mark: never used
            _bet("short", "e", 2 * DAY, 0.50, 0.20),
        ]
    )
    df = cal.snapshots(markets, bets).set_index("id")
    assert df.loc["long", "open"] == 0.30
    assert df.loc["long", "30d"] == 0.40
    assert df.loc["long", "mid"] == 0.55
    assert df.loc["long", "7d"] == 0.55
    assert df.loc["long", "1d"] == 0.80, "the bet 12 hours before the end is not used"
    assert df.loc["long", "y"] == 1
    assert math.isnan(df.loc["short", "30d"]), "a 10-day market has no 30-day snapshot"
    assert df.loc["short", "7d"] == 0.20
    assert df.loc["short", "y"] == 0


def test_trading_end_is_the_earlier_of_close_and_resolution():
    markets = pd.DataFrame(
        [_market("a", 0, 10 * DAY, close=5 * DAY), _market("b", 0, 10 * DAY, close=12 * DAY), _market("c", 0, 10 * DAY)]
    )
    markets.loc[2, "close_time"] = np.nan
    assert list(cal.trading_end(markets) / DAY) == [5, 10, 10]


def test_unresolved_or_cancelled_markets_are_ignored():
    markets = pd.DataFrame([_market("a", 0, 10 * DAY, resolution="CANCEL"), _market("b", 0, 10 * DAY)])
    bets = pd.DataFrame([_bet("b", "x", DAY, 0.5, 0.6)])
    assert list(cal.snapshots(markets, bets)["id"]) == ["b"]


# ---- scores with known answers ------------------------------------------------------------


def test_brier_known_values():
    assert cal.brier(np.array([1.0, 0.0]), np.array([1.0, 0.0])) == 0
    assert cal.brier(np.array([0.5, 0.5]), np.array([1.0, 0.0])) == 0.25
    assert cal.brier(np.array([0.0]), np.array([1.0])) == 1


def test_wilson_interval_matches_reference():
    lo, hi = cal.wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-4)
    assert hi == pytest.approx(0.7634, abs=1e-4)
    lo, hi = cal.wilson(0, 10)
    assert lo == 0 and hi == pytest.approx(0.2775, abs=1e-4)
    # At 100% the interval must still contain 1.0 exactly (a floating-point slip here broke the
    # chart on the first full-size run).
    for n in range(1, 500):
        lo, hi = cal.wilson(n, n)
        assert lo <= 1.0 <= hi == 1.0
        lo, hi = cal.wilson(0, n)
        assert lo == 0.0 <= hi


def test_a_calibrated_forecaster_scores_as_calibrated():
    rng = np.random.default_rng(1)
    p = rng.uniform(0, 1, 50_000)
    y = (rng.uniform(0, 1, 50_000) < p).astype(float)
    t = cal.reliability(p, y)
    assert cal.ece(t) < 0.01
    assert ((t["observed"] - t["mean_forecast"]).abs() < 0.02).all()
    m = cal.murphy(p, y)
    assert m["reliability"] < 0.001
    assert abs(m["reliability"] - m["resolution"] + m["uncertainty"] - cal.brier(p, y)) < 0.005


def test_an_overconfident_forecaster_is_detected():
    rng = np.random.default_rng(2)
    true = rng.uniform(0.3, 0.7, 50_000)
    y = (rng.uniform(0, 1, 50_000) < true).astype(float)
    overconfident = np.clip(0.5 + (true - 0.5) * 3, 0, 1)  # pushes 0.6 to 0.8, 0.4 to 0.2
    assert cal.ece(cal.reliability(overconfident, y)) > 0.1
    assert cal.brier(overconfident, y) > cal.brier(true, y)


def test_reliability_bins_include_the_edges():
    t = cal.reliability(np.array([0.0, 0.05, 0.1, 0.95, 1.0]), np.array([0, 0, 1, 1, 1.0]))
    assert list(t["n"]) == [2, 1, 0, 0, 0, 0, 0, 0, 0, 2]


def test_base_rate_baseline_only_learns_from_earlier_markets():
    # 40 markets: the first 20 to end all resolve NO, the last 20 all YES. A baseline that peeked
    # at the later half would not score a perfect 1.0 Brier on it; one that can't, does.
    n = 40
    df = pd.DataFrame({"id": range(n), "y": [0] * 20 + [1] * 20, "end": range(n), "mid": [0.5] * n})
    r = cal.evaluate(df, "mid", np.random.default_rng(0))
    assert r.test_n == 20
    assert r.test_brier_base_rate == 1.0
    assert r.test_brier == 0.25


def test_too_few_markets_are_not_reported():
    df = pd.DataFrame({"id": range(5), "y": [1, 0, 1, 0, 1], "end": range(5), "mid": [0.5] * 5})
    assert cal.evaluate(df, "mid", np.random.default_rng(0)) is None


def test_bootstrap_is_deterministic_and_covers_the_estimate():
    p = np.linspace(0.05, 0.95, 200)
    y = (np.arange(200) % 3 == 0).astype(float)
    a = cal.bootstrap_ci(lambda i: cal.brier(p[i], y[i]), 200, np.random.default_rng(7), reps=500)
    b = cal.bootstrap_ci(lambda i: cal.brier(p[i], y[i]), 200, np.random.default_rng(7), reps=500)
    assert a == b
    assert a[0] < cal.brier(p, y) < a[1]


# ---- end to end -----------------------------------------------------------------------------


def test_end_to_end_on_synthetic_csvs(tmp_path):
    rng = np.random.default_rng(3)
    markets, bets = [], []
    for i in range(300):
        life = rng.uniform(2, 60) * DAY
        truth = rng.uniform(0.05, 0.95)
        res = "YES" if rng.uniform() < truth else "NO"
        markets.append(_market(f"m{i}", 0, life, resolution=res, bettors=int(rng.integers(10, 80))))
        prob = 0.5
        for j, t in enumerate(np.sort(rng.uniform(0, life, 6))):
            new = float(np.clip(truth + rng.normal(0, 0.05), 0.01, 0.99))
            bets.append(_bet(f"m{i}", f"b{i}_{j}", t, prob, new))
            prob = new
    data = tmp_path / "data"
    data.mkdir()
    pd.DataFrame(markets).to_csv(data / "markets.csv", index=False)
    pd.DataFrame(bets).to_csv(data / "bets.csv", index=False)
    out = tmp_path / "out"
    summary = cal.main(["--data", str(data), "--out", str(out)])

    assert summary["input"]["markets_used"] == 300
    s = summary["snapshots"]
    assert set(s) >= {"open", "mid", "7d", "1d"}
    assert s["mid"]["n"] == 300
    assert s["mid"]["brier"] < 0.25, "informative synthetic forecasts beat a coin flip"
    assert s["open"]["brier"] == pytest.approx(0.25, abs=0.03), "every market opens at 50%"
    for f in ["calibration.json", "calibration.md", "reliability.png", "brier_by_snapshot.png"]:
        assert (out / f).exists()
    json.loads((out / "calibration.json").read_text())
