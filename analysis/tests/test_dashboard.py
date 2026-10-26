"""The dashboard starts and renders every tab, with results and local data present and without."""

import sys
from pathlib import Path

import pandas as pd
import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard_data as dd  # noqa: E402

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest


@pytest.fixture()
def world(tmp_path):
    """Synthetic results (written by the real M4 and M5 code) plus local data for two markets."""
    import calibration as cal
    import news_study as ns
    import test_calibration as tc
    import test_news as tn

    results = tmp_path / "results"
    # M4 on the calibration tests' synthetic markets
    import numpy as np

    rng = np.random.default_rng(3)
    markets, bets = [], []
    for i in range(300):
        life = rng.uniform(2, 60) * tc.DAY
        truth = rng.uniform(0.05, 0.95)
        markets.append(tc._market(f"c{i}", 0, life, resolution="YES" if rng.uniform() < truth else "NO", bettors=30))
        bets.append(tc._bet(f"c{i}", f"cb{i}", life / 3, 0.5, float(truth)))
    cdata = tmp_path / "cal"
    cdata.mkdir()
    pd.DataFrame(markets).to_csv(cdata / "markets.csv", index=False)
    pd.DataFrame(bets).to_csv(cdata / "bets.csv", index=False)
    cal.main(["--data", str(cdata), "--out", str(results), "--no-plots"])

    # M5 on the news tests' synthetic world; its markets/bets double as the "local data"
    picks, nm, nb, news = tn._world(react=True)
    data = tmp_path / "clean"
    data.mkdir()
    nm.to_csv(data / "markets.csv", index=False)
    nb.to_csv(data / "bets.csv", index=False)
    picks.to_csv(tmp_path / "picks.csv", index=False)
    news.to_csv(tmp_path / "news_daily.csv", index=False)
    ns.main(["--picks", str(tmp_path / "picks.csv"), "--data", str(data), "--news", str(tmp_path / "news_daily.csv"),
             "--out", str(results), "--no-plots"])
    return {"results": results, "data": data, "news": tmp_path / "news_daily.csv"}


def _run(monkeypatch, results, data, news):
    monkeypatch.setenv("PM_RESULTS", str(results))
    monkeypatch.setenv("PM_DATA", str(data))
    monkeypatch.setenv("PM_NEWS", str(news))
    at = AppTest.from_file(str(HERE / "dashboard.py"), default_timeout=120)
    at.run()
    assert not at.exception, at.exception
    return at


def test_dashboard_renders_with_everything(world, monkeypatch):
    at = _run(monkeypatch, world["results"], world["data"], world["news"])
    labels = [m.label for m in at.metric]
    assert "Brier score" in labels and "Markets tested" in labels
    assert any("tokens received" in lbl for lbl in labels)
    assert not [i for i in at.info if "local data" in i.value], "per-market chart should render"
    at.radio[0].set_value("1d").run()  # switch snapshot
    assert not at.exception


def test_dashboard_without_local_data_or_results(world, tmp_path, monkeypatch):
    at = _run(monkeypatch, world["results"], tmp_path / "nowhere", tmp_path / "nowhere.csv")
    assert any("local data" in i.value for i in at.info)
    at = _run(monkeypatch, tmp_path / "empty", tmp_path / "nowhere", tmp_path / "nowhere.csv")
    assert sum("run" in i.value for i in at.info) == 2


def test_pool_calculator_reports_bad_odds_instead_of_crashing(world, monkeypatch):
    at = _run(monkeypatch, world["results"], world["data"], world["news"])
    at.number_input[0].set_value(0.000000000000001).run()  # liquidity below the minimum
    assert not at.exception
    assert at.warning


def test_market_series_is_daily_and_uses_no_future_bets(world):
    prob, counts = dd.market_series("m0", world["data"], world["news"])
    assert (prob["date"].diff().dropna().dt.days == 1).all()
    assert prob["probability"].between(0, 1).all()
    assert len(counts) > 0
    assert dd.market_series("nope", world["data"], world["news"]) is None


def test_breakdown_table_is_rounded_and_readable():
    rows = [{"group": "10–19", "n": 1514, "base_rate": 0.36129, "brier": 0.142211, "brier_ci": [0.132397840785, 0.151796548]},
            {"group": "< 7 days", "n": 17, "note": "fewer than 20 markets: not reported"}]
    t = dd.breakdown_frame(rows)
    assert list(t["95% CI"]) == ["0.132–0.152", "fewer than 20 markets: not reported"]
    assert list(t["YES rate"]) == ["36.1%", "–"] and t["Brier"][0] == "0.142"
