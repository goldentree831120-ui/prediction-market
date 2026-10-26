"""Data loading for the M6 dashboard, kept apart from the Streamlit code so it can be tested.

The dashboard reads only files on this machine:
  results/calibration.json, results/news_study.json      (committed to the repo)
  ../pipeline/data/clean/{markets,bets}.csv               (local only, from M3; optional)
  news/data/news_daily.csv                                (local only, from M5; optional)
It never calls an API. Without the local-only files, the per-market charts are skipped and the
rest still works.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DAY_MS = 86_400_000


def paths() -> dict[str, Path]:
    """Default locations, overridable with environment variables (used by the tests)."""
    return {
        "results": Path(os.environ.get("PM_RESULTS", HERE / "results")),
        "data": Path(os.environ.get("PM_DATA", HERE.parent / "pipeline" / "data" / "clean")),
        "news": Path(os.environ.get("PM_NEWS", HERE / "news" / "data" / "news_daily.csv")),
    }


def load_json(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def reliability_frame(calibration: dict, snapshot: str) -> pd.DataFrame:
    """Reliability table of one snapshot, bins with no markets dropped."""
    t = pd.DataFrame(calibration["snapshots"][snapshot]["reliability_table"])
    return t[t["n"] > 0].reset_index(drop=True)


def snapshot_frame(calibration: dict) -> pd.DataFrame:
    rows = []
    for name, s in calibration["snapshots"].items():
        rows.append({
            "snapshot": name,
            "markets": s["n"],
            "YES rate": s["base_rate"],
            "Brier": s["brier"],
            "Brier CI low": s["brier_ci"][0],
            "Brier CI high": s["brier_ci"][1],
            "ECE": s["ece"],
            "later half: market Brier": s["test_brier"],
            "later half: base-rate Brier": s["test_brier_base_rate"],
        })
    return pd.DataFrame(rows)


def breakdown_frame(rows: list[dict]) -> pd.DataFrame:
    """A breakdown table from calibration.json, rounded, with the CI as one readable column."""
    out = []
    for r in rows:
        if "brier" in r:
            out.append({"group": r["group"], "markets": r["n"], "YES rate": f"{r['base_rate']:.1%}",
                        "Brier": f"{r['brier']:.3f}", "95% CI": f"{r['brier_ci'][0]:.3f}–{r['brier_ci'][1]:.3f}"})
        else:
            out.append({"group": r["group"], "markets": r["n"], "YES rate": "–", "Brier": "–", "95% CI": r.get("note", "")})
    return pd.DataFrame(out)


def news_frame(news: dict) -> pd.DataFrame:
    """One row per picked market, tested or not."""
    df = pd.DataFrame(news["markets"])
    for c in ["reaction_p", "lead_p", "reaction_after_news", "reaction_normal", "lead_before_news", "lead_normal"]:
        if c not in df:
            df[c] = np.nan
    if "note" not in df:  # every market was tested
        df["note"] = ""
    df["tested"] = df["reaction_p"].notna()
    return df


def market_series(market_id: str, data_dir: Path, news_csv: Path) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Daily probability (at 00:00 UTC, from bets at or before that moment) and daily news counts for
    one market. None if the local-only data isn't on this machine."""
    if not (data_dir / "markets.csv").exists() or not (data_dir / "bets.csv").exists() or not news_csv.exists():
        return None
    markets = pd.read_csv(data_dir / "markets.csv").set_index("id")
    if market_id not in markets.index:
        return None
    m = markets.loc[market_id]
    ends = [m[c] for c in ("close_time", "resolution_time") if pd.notna(m[c])]
    start, end = float(m["created_time"]), float(min(ends))
    bets = _bets_for(data_dir / "bets.csv", market_id)
    news = pd.read_csv(news_csv)
    news = news[news["market_id"] == market_id].sort_values("day_ms")
    if bets.empty:
        return None
    t = bets["created_time"].to_numpy(float)
    before, after = bets["prob_before"].to_numpy(float), bets["prob_after"].to_numpy(float)
    first = int(np.ceil(start / DAY_MS) * DAY_MS)
    days = np.arange(first, int(end) + 1, DAY_MS, dtype=np.int64)
    i = np.searchsorted(t, days, side="right") - 1
    prob = np.where(i >= 0, after[np.maximum(i, 0)], before[0])
    prob_df = pd.DataFrame({"date": pd.to_datetime(days, unit="ms"), "probability": prob})
    news_df = pd.DataFrame({"date": pd.to_datetime(news["day_ms"], unit="ms"), "articles": news["articles"].to_numpy()})
    return prob_df, news_df


def _bets_for(path: Path, market_id: str) -> pd.DataFrame:
    cols = ["market_id", "bet_id", "created_time", "prob_before", "prob_after"]
    parts = [c[c["market_id"] == market_id] for c in pd.read_csv(path, usecols=cols, chunksize=200_000)]
    b = pd.concat(parts) if parts else pd.DataFrame(columns=cols)
    return b.sort_values(["created_time", "bet_id"])
