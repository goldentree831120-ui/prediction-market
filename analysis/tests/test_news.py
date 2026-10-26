"""M5 tests: synthetic news series and markets with known answers. No network."""

import io
import json
import sys
import urllib.error
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import news_fetch as nf  # noqa: E402
import news_study as ns  # noqa: E402

H = ns.HOUR_MS
D = ns.DAY_MS


# ---- fetcher ---------------------------------------------------------------------------------


def test_fetch_range_is_whole_days_last_90_days_plus_two_weeks():
    lo, hi = nf.fetch_range(5 * H, 200 * D + 7 * H)
    assert lo % D == 0 and hi % D == 0
    assert hi == 201 * D, "includes the day trading ended"
    assert lo == 110 * D - 14 * D
    lo, hi = nf.fetch_range(50 * D + 3 * H, 52 * D)
    assert (lo, hi) == (36 * D, 53 * D)


def test_fetch_range_is_always_long_enough_for_daily_steps():
    lo, hi = nf.fetch_range(10 * D, 10 * D + 2 * H)
    assert hi - lo >= nf.MIN_SPAN_DAYS * D


def _timeline(points):
    return json.dumps({"timeline": [{"series": "Article Count", "data": [{"date": d, "value": v, "norm": n} for d, v, n in points]}]})


def test_parse_timeline_daily_with_missing_days():
    t0 = int(pd.Timestamp("2025-06-12", tz="UTC").timestamp() * 1000)
    rows = nf.parse_timeline(_timeline([("20250612T000000Z", 30, 1000), ("20250615T000000Z", 5, 900)]))
    assert rows == [(t0, 30, 1000), (t0 + 3 * D, 5, 900)], "days GDELT leaves out are just absent"
    assert nf.parse_timeline(json.dumps({})) == []


def test_parse_timeline_adds_finer_steps_into_days():
    t0 = int(pd.Timestamp("2025-06-12", tz="UTC").timestamp() * 1000)
    rows = nf.parse_timeline(_timeline([("20250612T010000Z", 3, 10), ("20250612T230000Z", 4, 10), ("20250613T000000Z", 1, 10)]))
    assert rows == [(t0, 7, 20), (t0 + D, 1, 10)]


def test_parse_timeline_rejects_error_text():
    with pytest.raises(nf.FetchError, match="did not return JSON"):
        nf.parse_timeline("Please limit requests to one every 5 seconds")
    with pytest.raises(nf.FetchError, match="date format"):
        nf.parse_timeline(_timeline([("yesterday", 1, 1)]))


def test_url_asks_for_english_raw_counts_over_the_whole_span():
    url = nf.build_url('"Adam Bandt"', 0, 20 * D)
    assert "mode=timelinevolraw" in url and "sourcelang%3Aenglish" in url
    assert "startdatetime=19700101000000" in url and "enddatetime=19700120235959" in url


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_client_backs_off_slows_down_and_caches(tmp_path):
    calls, sleeps = [], []
    good = _timeline([("20250612T000000Z", 1, 1)]).encode()
    replies = [urllib.error.HTTPError("u", 429, "slow down", {}, None), _Resp(good)]

    def opener(req, timeout):
        calls.append(req.full_url)
        assert "prediction-market-research" in req.get_header("User-agent")
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    c = nf.Client(tmp_path, opener=opener, sleep=sleeps.append, gap_s=2)
    assert c.get("https://x/1") == good.decode()
    assert len(calls) == 2 and any(s >= 60 for s in sleeps), "429 -> waited, then retried"
    assert c.gap_s == 3, "and slowed down for the rest of the run"
    assert c.get("https://x/1") == good.decode() and len(calls) == 2, "second time: from the cache"


def test_client_error_text_is_not_cached(tmp_path):
    c = nf.Client(tmp_path, opener=lambda req, timeout: _Resp(b"Invalid query"), sleep=lambda s: None, gap_s=0)
    with pytest.raises(nf.FetchError):
        c.get("https://x/err")
    assert list(tmp_path.glob("*.json")) == []
    good = _timeline([("20250612T000000Z", 1, 1)]).encode()
    c2 = nf.Client(tmp_path, opener=lambda req, timeout: _Resp(good), sleep=lambda s: None, gap_s=0)
    assert c2.get("https://x/ok") == good.decode()
    off = nf.Client(tmp_path, offline=True)
    assert off.get("https://x/ok") == good.decode(), "served from cache"
    with pytest.raises(nf.FetchError, match="offline"):
        off.get("https://x/never")


def test_fetch_main_fills_missing_days_with_zero(tmp_path, monkeypatch):
    picks = pd.DataFrame([{"id": "m1", "topic": "t", "question": "q", "gdelt_query": "x"}])
    markets = pd.DataFrame([{"id": "m1", "created_time": 100 * D, "close_time": 120 * D, "resolution_time": 121 * D}])
    picks.to_csv(tmp_path / "picks.csv", index=False)
    markets.to_csv(tmp_path / "markets.csv", index=False)
    day = lambda k: pd.Timestamp(k * D, unit="ms").strftime("%Y%m%dT000000Z")  # noqa: E731
    monkeypatch.setattr(nf.Client, "get", lambda self, url: _timeline([(day(110), 50, 1), (day(111), 7, 1)]))
    assert nf.main(["--picks", str(tmp_path / "picks.csv"), "--markets", str(tmp_path / "markets.csv"), "--out", str(tmp_path / "o")]) == 0
    out = pd.read_csv(tmp_path / "o" / "news_daily.csv")
    assert list(out["day_ms"]) == [k * D for k in range(86, 121)], "every day from 2 weeks before to the end"
    assert out.set_index("day_ms").loc[110 * D, "articles"] == 50
    assert out["articles"].sum() == 57


# ---- spikes ----------------------------------------------------------------------------------


def _series(n_days, base=5, spikes=()):
    days = np.arange(n_days, dtype=np.int64) * D
    counts = np.full(n_days, float(base))
    for i, v in spikes:
        counts[i] = v
    return days, counts


def test_spike_needs_both_the_floor_and_the_ratio():
    days, counts = _series(100, base=2, spikes=[(30, 19), (40, 20), (60, 90)])
    raw, kept = ns.find_spikes(days, counts, days)
    assert list(kept // D) == [40, 60], "19 is under the 20-article floor"
    days, counts = _series(100, base=10, spikes=[(30, 29), (40, 30)])
    assert list(ns.find_spikes(days, counts, days)[1] // D) == [40], "29 < 3 x median 10"


def test_spike_baseline_uses_only_the_past_two_weeks():
    # A long burst right AFTER day 50 must not raise the baseline for day 50 itself.
    days, counts = _series(120, base=1, spikes=[(50, 40)] + [(i, 500) for i in range(51, 120)])
    raw, _ = ns.find_spikes(days, counts, days)
    assert 50 in set(raw // D)
    # The first two weeks have no full baseline: no spikes there.
    days, counts = _series(100, base=1, spikes=[(10, 500)])
    assert len(ns.find_spikes(days, counts, days)[0]) == 0
    # ...and a gap in the data inside the baseline also means no spike.
    days, counts = _series(100, base=1, spikes=[(50, 500)])
    keep = days != 45 * D
    assert len(ns.find_spikes(days[keep], counts[keep], days[keep])[0]) == 0


def test_spike_days_close_together_are_one_story():
    days, counts = _series(100, base=1, spikes=[(30, 99), (31, 99), (32, 99), (40, 99)])
    raw, kept = ns.find_spikes(days, counts, days)
    assert list(raw // D) == [30, 31, 32, 40]
    assert list(kept // D) == [30, 32, 40]


def test_normal_days_stay_clear_of_every_spike():
    grid = np.arange(0, 40) * D
    n = set(ns.normal_days(grid, np.array([20 * D])) // D)
    assert not n & {18, 19, 20, 21, 22}
    assert {17, 23} <= n


# ---- statistics ------------------------------------------------------------------------------


def test_benjamini_hochberg_known_example():
    p = np.array([0.01, 0.04, 0.03, 0.005, 0.2])
    # sorted: .005 .01 .03 .04 .2 vs q*i/m = .02 .04 .06 .08 .10 -> first four pass
    assert list(ns.benjamini_hochberg(p, 0.10)) == [True, True, True, True, False]
    assert list(ns.benjamini_hochberg(np.array([0.5, 0.6]), 0.10)) == [False, False]
    # step-up: a p-value above its own threshold still passes if a larger one passes
    assert list(ns.benjamini_hochberg(np.array([0.03, 0.04]), 0.05)) == [True, True]


def test_perm_pvalue_extremes():
    rng = np.random.default_rng(0)
    pool = np.zeros(500)
    assert ns.perm_pvalue(np.array([1.0, 1.0]), pool, rng, reps=999) == pytest.approx(1 / 1000)
    assert ns.perm_pvalue(np.array([0.0]), pool, rng, reps=99) == 1.0, "ties count against the event"


def test_price_fn_never_looks_ahead():
    p = ns.price_fn(np.array([10.0, 20.0]), np.array([0.5, 0.6]), np.array([0.6, 0.9]))
    assert list(p(np.array([5.0, 10.0, 19.0, 20.0]))) == [0.5, 0.6, 0.6, 0.9]


# ---- end to end ------------------------------------------------------------------------------


def _world(react: bool, seed=1):
    """Two markets, 80 days each (news from 14 days before). News spikes on 5 days; if react, the
    price jumps 15 points within a few hours after the start of each spike day, otherwise it only
    drifts."""
    rng = np.random.default_rng(seed)
    picks, markets, bets, news = [], [], [], []
    spike_days = [20, 32, 44, 56, 68]
    for k, topic in enumerate(["a", "b"]):
        mid = f"m{k}"
        created, end = 0, 80 * D
        picks.append({"id": mid, "topic": topic, "question": f"q{k}", "gdelt_query": "x"})
        markets.append({"id": mid, "created_time": created, "close_time": end, "resolution_time": end, "resolution": "YES",
                        "unique_bettors": 20})
        for d in range(-14, 80):
            v = int(rng.poisson(8)) + (200 if d in spike_days else 0)
            news.append({"market_id": mid, "day_ms": d * D, "articles": v, "all_articles": 100000})
        prob, j = 0.5, 0
        for h in range(1, 80 * 24, 3):
            new = prob + rng.normal(0, 0.003)
            if react and any(0 < h - 24 * s <= 9 for s in spike_days):
                new = prob + 0.05
            new = float(np.clip(new, 0.02, 0.98))
            bets.append({"market_id": mid, "bet_id": f"b{k}_{j}", "created_time": h * H, "prob_before": prob, "prob_after": new})
            prob, j = new, j + 1
    return pd.DataFrame(picks), pd.DataFrame(markets), pd.DataFrame(bets), pd.DataFrame(news)


def test_end_to_end_detects_a_real_reaction():
    picks, markets, bets, news = _world(react=True)
    res = ns.analyse(picks, markets, bets, news)
    df = res["rows"]
    assert (df["spikes"] == 5).all()
    assert (df["reaction_p"] < 0.01).all() and df["reaction_bh"].all()
    assert res["summary"]["reaction"]["mean_over_topics"] > 0.05
    assert not df["lead_bh"].any(), "the price moved only AFTER the news"


def test_end_to_end_finds_nothing_when_there_is_nothing():
    # 10 worlds x 2 markets with no reaction: about 5% of tests may pass by chance, not many more.
    hits, effects = 0, []
    for seed in range(1, 11):
        res = ns.analyse(*_world(react=False, seed=seed))
        hits += int(res["rows"]["reaction_bh"].sum())
        effects.append(res["summary"]["reaction"]["mean_over_topics"])
    assert hits <= 2, f"{hits}/20 false positives"
    assert abs(np.mean(effects)) < 0.003


def test_short_markets_are_reported_not_tested():
    picks, markets, bets, news = _world(react=True)
    markets.loc[0, ["close_time", "resolution_time"]] = 3 * D
    res = ns.analyse(picks, markets, bets, news)
    row = res["rows"].set_index("id").loc["m0"]
    assert np.isnan(row["reaction_p"]) and isinstance(row["note"], str)
    assert res["summary"]["markets_tested"] == 1


def test_main_writes_outputs(tmp_path):
    picks, markets, bets, news = _world(react=True)
    data = tmp_path / "clean"
    data.mkdir()
    markets.to_csv(data / "markets.csv", index=False)
    bets.to_csv(data / "bets.csv", index=False)
    picks.to_csv(tmp_path / "picks.csv", index=False)
    news.to_csv(tmp_path / "news.csv", index=False)
    out = tmp_path / "out"
    s = ns.main(["--picks", str(tmp_path / "picks.csv"), "--data", str(data), "--news", str(tmp_path / "news.csv"), "--out", str(out)])
    assert s["markets_tested"] == 2
    for f in ["news_study.json", "news_study.md", "news_event_moves.png"]:
        assert (out / f).exists()
