"""P4 M5, step 2: do Manifold probabilities move when the news does?

An event study on the hand-picked markets in news/markets.csv, on daily news counts (UTC days;
see news_fetch.py for why daily). All rules are fixed here, before looking at results:

  news spike   a day with at least MIN_ARTICLES matching articles AND at least RATIO times the
               median daily count of the previous BASELINE_D days (the baseline never includes the
               day itself or anything later). Spike days less than WINDOW_D days after a kept spike
               are the same story and are dropped.
  reaction     |p(D + 2 days) - p(D)|, D = 00:00 UTC on the spike day: how far the probability moved
               over the spike day and the day after. p(t) uses only bets at or before t (as in M4).
  lead         |p(D) - p(D - 2 days)|: did the market move BEFORE the news spiked?
  normal days  days in the same market more than WINDOW_D days from every spike (spikes before the
               de-duplication included), with the same windows inside the market's life.

Per market: the mean reaction on spike days minus the mean over normal days, with a one-sided
permutation p-value (random normal days in place of the spikes, PERMS draws). Across markets:
Benjamini-Hochberg at Q. Pooled: markets on the same topic share news, so the overall effect is the
mean over topics of each topic's mean, with a bootstrap over topics.

    python news_study.py      # reads news/data + pipeline/data/clean, writes results/news_*.{json,md,png}
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import calibration as cal

HERE = Path(__file__).resolve().parent
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
STEP = DAY_MS  # the news series is daily
MAX_DAYS = 90
BASELINE_D = 14
MIN_ARTICLES = 20
RATIO = 3.0
WINDOW_D = 2
MIN_NORMAL_DAYS = 10
PERMS = 2000
Q = 0.10
SEED = 20261025
PROFILE_H = 96


# --------------------------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------------------------


def price_fn(times: np.ndarray, before: np.ndarray, after: np.ndarray):
    """Vectorised version of calibration.prob_at: probability at each time t, never using a later bet."""

    def p(t: np.ndarray) -> np.ndarray:
        i = np.searchsorted(times, t, side="right") - 1
        return np.where(i >= 0, after[np.maximum(i, 0)], before[0])

    return p


def find_spikes(days: np.ndarray, counts: np.ndarray, candidates: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """days/counts: the daily news series (sorted, day starts in ms).
    candidates: the days allowed to be spikes. Returns (all spike days, de-duplicated spike days)."""
    idx = {int(h): i for i, h in enumerate(days)}
    raw = []
    for t in candidates:
        i = idx.get(int(t))
        if i is None:
            continue
        lo = int(np.searchsorted(days, int(t) - BASELINE_D * STEP))
        if i - lo != BASELINE_D:  # the full baseline before t must be in the data (no gaps)
            continue
        base = float(np.median(counts[lo:i]))
        if counts[i] >= max(MIN_ARTICLES, RATIO * base):
            raw.append(int(t))
    kept = []
    for t in raw:
        if not kept or t - kept[-1] >= WINDOW_D * STEP:
            kept.append(t)
    return np.array(raw, dtype=np.int64), np.array(kept, dtype=np.int64)


def normal_days(grid: np.ndarray, spikes: np.ndarray) -> np.ndarray:
    if len(spikes) == 0:
        return grid
    d = np.abs(grid[:, None] - spikes[None, :]).min(axis=1)
    return grid[d > WINDOW_D * STEP]


def perm_pvalue(event_vals: np.ndarray, pool: np.ndarray, rng: np.random.Generator, reps: int = PERMS) -> float:
    """One-sided: P(mean of len(event_vals) random pool values >= observed mean), with the +1 correction."""
    k = len(event_vals)
    obs = event_vals.mean()
    draws = np.array([pool[rng.choice(len(pool), k, replace=False)].mean() for _ in range(reps)])
    return float((1 + np.sum(draws >= obs)) / (1 + reps))


def benjamini_hochberg(p: np.ndarray, q: float = Q) -> np.ndarray:
    """True where the hypothesis is rejected at false-discovery rate q."""
    p = np.asarray(p, dtype=float)
    m = len(p)
    if m == 0:
        return np.zeros(0, dtype=bool)
    order = np.argsort(p)
    passed = p[order] <= q * np.arange(1, m + 1) / m
    k = np.max(np.nonzero(passed)[0]) + 1 if passed.any() else 0
    out = np.zeros(m, dtype=bool)
    out[order[:k]] = True
    return out


# --------------------------------------------------------------------------------------------
# Per market
# --------------------------------------------------------------------------------------------


def study_market(pick, mrow, bets: pd.DataFrame, news: pd.DataFrame, rng: np.random.Generator) -> dict:
    created = float(mrow["created_time"])
    end = float(np.fmin(float(mrow["close_time"]) if pd.notna(mrow["close_time"]) else np.inf,
                        float(mrow["resolution_time"]) if pd.notna(mrow["resolution_time"]) else np.inf))
    out = {"id": pick.id, "topic": pick.topic, "question": pick.question, "resolution": mrow["resolution"]}
    b = bets.sort_values(["created_time", "bet_id"])
    if len(b) == 0 or len(news) == 0:
        return out | {"note": "no bets or no news data"}
    p = price_fn(b["created_time"].to_numpy(float), b["prob_before"].to_numpy(float), b["prob_after"].to_numpy(float))

    n = news.sort_values("day_ms")
    days, counts = n["day_ms"].to_numpy(np.int64), n["articles"].to_numpy(float)
    start = max(created, end - MAX_DAYS * DAY_MS)
    first = int(np.ceil(start / STEP) * STEP)
    grid = np.arange(first, int(end) - WINDOW_D * STEP + 1, STEP, dtype=np.int64)
    grid = grid[grid - WINDOW_D * STEP >= created]  # the lead window must be inside the market's life
    if len(grid) == 0:
        return out | {"note": "market too short"}

    raw, spikes = find_spikes(days, counts, grid)
    normal = normal_days(grid, raw)
    react = lambda t: np.abs(p(t + WINDOW_D * STEP) - p(t))  # noqa: E731
    lead = lambda t: np.abs(p(t) - p(t - WINDOW_D * STEP))  # noqa: E731
    out |= {"days": int(len(grid)), "spikes": int(len(spikes)), "normal_days": int(len(normal)),
            "spike_days": [pd.Timestamp(int(s), unit="ms").strftime("%Y-%m-%d") for s in spikes]}
    if len(spikes) == 0:
        return out | {"note": "no news spikes"}
    if len(normal) < max(MIN_NORMAL_DAYS, len(spikes)):
        return out | {"note": "too few normal days"}
    r_e, r_n, l_e, l_n = react(spikes), react(normal), lead(spikes), lead(normal)
    return out | {
        "reaction_after_news": float(r_e.mean()),
        "reaction_normal": float(r_n.mean()),
        "reaction_diff": float(r_e.mean() - r_n.mean()),
        "reaction_p": perm_pvalue(r_e, r_n, rng),
        "lead_before_news": float(l_e.mean()),
        "lead_normal": float(l_n.mean()),
        "lead_diff": float(l_e.mean() - l_n.mean()),
        "lead_p": perm_pvalue(l_e, l_n, rng),
        "profile_news": _profile(p, spikes, created, end),
        "profile_normal": _profile(p, normal, created, end),
    }


def _profile(p, ts: np.ndarray, created: float, end: float) -> list[float]:
    """Mean |p(t + tau) - p(t)| for tau = -48 .. +48 h (only where t + tau is inside the market's life)."""
    out = []
    for tau in range(-PROFILE_H, PROFILE_H + 1):
        u = ts + tau * HOUR_MS
        ok = (u >= created) & (u <= end)
        out.append(float(np.abs(p(u[ok]) - p(ts[ok])).mean()) if ok.any() else float("nan"))
    return out


# --------------------------------------------------------------------------------------------
# Across markets
# --------------------------------------------------------------------------------------------


def pooled(rows: pd.DataFrame, col: str, rng: np.random.Generator, reps: int = 2000) -> dict:
    d = rows.dropna(subset=[col])
    if d.empty:
        return {"markets": 0}
    topic_means = d.groupby("topic")[col].mean()
    vals = topic_means.to_numpy()
    boot = [rng.choice(vals, len(vals), replace=True).mean() for _ in range(reps)]
    return {
        "markets": int(len(d)),
        "topics": int(len(vals)),
        "mean_over_topics": float(vals.mean()),
        "ci": [float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))],
        "markets_positive": int((d[col] > 0).sum()),
        "topics_positive": int((vals > 0).sum()),
    }


def analyse(picks: pd.DataFrame, markets: pd.DataFrame, bets: pd.DataFrame, news: pd.DataFrame) -> dict:
    rng = np.random.default_rng(SEED)
    m = markets.set_index("id")
    bg = dict(tuple(bets.groupby("market_id")))
    ng = dict(tuple(news.groupby("market_id")))
    empty_b = bets.iloc[0:0]
    empty_n = news.iloc[0:0]
    rows = [study_market(pk, m.loc[pk.id], bg.get(pk.id, empty_b), ng.get(pk.id, empty_n), rng)
            for pk in picks.itertuples(index=False) if pk.id in m.index]
    df = pd.DataFrame(rows)
    for c in ["reaction_p", "lead_p", "reaction_diff", "lead_diff"]:
        if c not in df:
            df[c] = np.nan
    tested = df["reaction_p"].notna()
    df["reaction_bh"] = False
    df.loc[tested, "reaction_bh"] = benjamini_hochberg(df.loc[tested, "reaction_p"].to_numpy())
    df["lead_bh"] = False
    df.loc[df["lead_p"].notna(), "lead_bh"] = benjamini_hochberg(df.loc[df["lead_p"].notna(), "lead_p"].to_numpy())
    return {
        "rows": df,
        "summary": {
            "markets_picked": int(len(picks)),
            "markets_studied": int(len(df)),
            "markets_tested": int(tested.sum()),
            "spikes_total": int(df.get("spikes", pd.Series(dtype=float)).fillna(0).sum()),
            "reaction": pooled(df, "reaction_diff", rng) | {"bh_significant": int(df["reaction_bh"].sum())},
            "lead": pooled(df, "lead_diff", rng) | {"bh_significant": int(df["lead_bh"].sum())},
            "not_tested": df.loc[~tested, ["id", "note"]].to_dict(orient="records") if "note" in df else [],
        },
    }


# --------------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------------


def markdown(res: dict) -> str:
    s, df = res["summary"], res["rows"]
    pp = lambda x: f"{100 * x:.1f}"  # noqa: E731  probability points
    lines = [f"Markets picked: {s['markets_picked']}; tested: {s['markets_tested']}; news spikes: {s['spikes_total']}.", ""]
    for name, label in [("reaction", "2 days FROM the start of a news-spike day"), ("lead", "2 days BEFORE a news-spike day")]:
        r = s[name]
        if r.get("markets"):
            lines.append(
                f"- Move in the {label}, minus normal days: **{pp(r['mean_over_topics'])} points** "
                f"(mean over {r['topics']} topics, 95% CI {pp(r['ci'][0])} to {pp(r['ci'][1])}); larger in "
                f"{r['markets_positive']}/{r['markets']} markets; significant after Benjamini-Hochberg (q={Q}): {r['bh_significant']}."
            )
    lines += ["", "| Market | Topic | Spike days | Move from news day | Normal 2-day move | p | BH | Move before news | p |", "|---|---|---|---|---|---|---|---|---|"]
    t = df[df["reaction_p"].notna()].sort_values("reaction_p")
    for r in t.itertuples():
        q = r.question if len(r.question) <= 60 else r.question[:57] + "..."
        lines.append(
            f"| {q} | {r.topic} | {r.spikes} | {pp(r.reaction_after_news)} | {pp(r.reaction_normal)} | {r.reaction_p:.3f} | "
            f"{'yes' if r.reaction_bh else ''} | {pp(r.lead_before_news)} | {r.lead_p:.3f} |"
        )
    if s["not_tested"]:
        lines += ["", "Not tested: " + "; ".join(f"{x['id']} ({x['note']})" for x in s["not_tested"])]
    lines += ["", "Moves are absolute changes in the YES probability, in percentage points."]
    return "\n".join(lines) + "\n"


def plot(res: dict, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    df = res["rows"].dropna(subset=["reaction_p"])
    if df.empty:
        return
    taus = np.arange(-PROFILE_H, PROFILE_H + 1)
    news = np.nanmean(np.vstack(df["profile_news"].to_list()), axis=0) * 100
    normal = np.nanmean(np.vstack(df["profile_normal"].to_list()), axis=0) * 100
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(taus, news, color="#d62728", label=f"around news spikes ({int(df['spikes'].sum())} in {len(df)} markets)")
    ax.plot(taus, normal, color="#1f77b4", label="around normal days (same markets)")
    ax.axvline(0, color="#999", lw=1, ls="--")
    ax.set_xlabel("hours from 00:00 UTC on the news-spike day")
    ax.set_ylabel("mean |p(t+τ) − p(t)|, points")
    ax.set_title("How far Manifold probabilities move around news spikes")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "news_event_moves.png", dpi=150)
    plt.close(fig)


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--picks", type=Path, default=HERE / "news" / "markets.csv")
    ap.add_argument("--data", type=Path, default=HERE.parent / "pipeline" / "data" / "clean")
    ap.add_argument("--news", type=Path, default=HERE / "news" / "data" / "news_daily.csv")
    ap.add_argument("--out", type=Path, default=HERE / "results")
    ap.add_argument("--no-plots", action="store_true")
    a = ap.parse_args(argv)

    picks = pd.read_csv(a.picks)
    markets, bets = cal.load(a.data)
    bets = bets[bets["market_id"].isin(picks["id"])]
    news = pd.read_csv(a.news)
    res = analyse(picks, markets, bets, news)
    a.out.mkdir(parents=True, exist_ok=True)
    rows = res["rows"].drop(columns=[c for c in ["profile_news", "profile_normal"] if c in res["rows"]])
    summary = {
        "rules": {"max_days": MAX_DAYS, "baseline_days": BASELINE_D, "min_articles_per_day": MIN_ARTICLES, "ratio": RATIO,
                  "window_days": WINDOW_D, "permutations": PERMS, "fdr_q": Q, "seed": SEED},
        **res["summary"],
        "markets": json.loads(rows.to_json(orient="records")),
    }
    (a.out / "news_study.json").write_text(json.dumps(summary, indent=2))
    md = markdown(res)
    (a.out / "news_study.md").write_text(md)
    if not a.no_plots:
        plot(res, a.out)
    print(md)
    return summary


if __name__ == "__main__":
    main()
