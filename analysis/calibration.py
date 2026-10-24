"""P4 M4: are Manifold's probabilities calibrated?

Reads the CSVs written by the Rust fetcher (pipeline/data/clean) and, for every resolved YES/NO
market, takes the market probability at moments fixed IN ADVANCE (never "just before
resolution", when the answer is often already known):

    open       the first probability (set by the market's creator, before any trading)
    mid        halfway through the market's trading life
    30d/7d/1d  30, 7 and 1 day(s) before trading ended (only markets that lasted that long)

For each snapshot it reports the Brier score with a bootstrap 95% CI, two naive baselines,
a reliability table (10 bins, Wilson 95% CIs), expected calibration error and the Murphy
decomposition. Everything is deterministic (fixed bootstrap seed).

    python analysis/calibration.py                 # reads pipeline/data/clean, writes analysis/results
    python analysis/calibration.py --data DIR --out DIR
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

DAY_MS = 86_400_000
HORIZONS = {"30d": 30 * DAY_MS, "7d": 7 * DAY_MS, "1d": 1 * DAY_MS}
SNAPSHOTS = ["open", "mid", "30d", "7d", "1d"]
N_BINS = 10
BOOTSTRAP = 2000
SEED = 20261024


# --------------------------------------------------------------------------------------------
# Loading and probability snapshots
# --------------------------------------------------------------------------------------------


def load(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    markets = pd.read_csv(data_dir / "markets.csv")
    bets = pd.read_csv(data_dir / "bets.csv", usecols=["market_id", "bet_id", "created_time", "prob_before", "prob_after"])
    return markets, bets


def trading_end(markets: pd.DataFrame) -> pd.Series:
    """When trading stopped: the earlier of close and resolution time (whichever exist)."""
    close = markets["close_time"].astype("float").to_numpy()
    res = markets["resolution_time"].astype("float").to_numpy()
    return pd.Series(np.fmin(close, res), index=markets.index)


def prob_at(times: np.ndarray, before: np.ndarray, after: np.ndarray, t: float) -> float:
    """Market probability at time t from one market's bets (sorted by time):
    the probability after the last bet at or before t; before the first bet, the opening
    probability (the first bet's prob_before). NaN if the market has no bets.
    Never uses a bet made after t."""
    if len(times) == 0:
        return float("nan")
    i = np.searchsorted(times, t, side="right") - 1
    return float(after[i]) if i >= 0 else float(before[0])


def snapshots(markets: pd.DataFrame, bets: pd.DataFrame) -> pd.DataFrame:
    """One row per market: outcome, metadata, and the probability at each snapshot (NaN when the
    snapshot doesn't apply, e.g. a 30d snapshot for a market that traded for only 10 days)."""
    m = markets.copy()
    m["end"] = trading_end(m)
    m["y"] = (m["resolution"] == "YES").astype(int)
    m = m[m["resolution"].isin(["YES", "NO"]) & m["end"].notna() & (m["end"] > m["created_time"])]
    groups = {k: g.sort_values(["created_time", "bet_id"]) for k, g in bets.groupby("market_id")}
    rows = []
    empty = np.array([])
    for r in m.itertuples(index=False):
        g = groups.get(r.id)
        t = g["created_time"].to_numpy(dtype=float) if g is not None else empty
        pb = g["prob_before"].to_numpy(dtype=float) if g is not None else empty
        pa = g["prob_after"].to_numpy(dtype=float) if g is not None else empty
        start, end = float(r.created_time), float(r.end)
        row = {
            "id": r.id,
            "y": r.y,
            "start": start,
            "end": end,
            "lifetime_days": (end - start) / DAY_MS,
            "bettors": r.unique_bettors,
            "open": float(pb[0]) if len(pb) else float("nan"),
            "mid": prob_at(t, pb, pa, start + (end - start) / 2),
        }
        for name, h in HORIZONS.items():
            row[name] = prob_at(t, pb, pa, end - h) if end - h >= start else float("nan")
        rows.append(row)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------------
# Scores
# --------------------------------------------------------------------------------------------


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def log_loss(p: np.ndarray, y: np.ndarray, eps: float = 1e-3) -> float:
    q = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)))


def bootstrap_ci(stat, n: int, rng: np.random.Generator, reps: int = BOOTSTRAP) -> tuple[float, float]:
    """Percentile 95% CI of stat(idx) over resamples of n items with replacement."""
    vals = np.array([stat(rng.integers(0, n, n)) for _ in range(reps)])
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson score 95% interval for a proportion k/n."""
    if n == 0:
        return (float("nan"), float("nan"))
    ph = k / n
    den = 1 + z * z / n
    centre = (ph + z * z / (2 * n)) / den
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    # Clamp floating-point error at 0% and 100% (k = 0 or k = n): the interval must contain k/n.
    return (max(0.0, min(centre - half, ph)), min(1.0, max(centre + half, ph)))


def reliability(p: np.ndarray, y: np.ndarray, bins: int = N_BINS) -> pd.DataFrame:
    """Equal-width bins; the last bin includes 1.0."""
    idx = np.minimum((p * bins).astype(int), bins - 1)
    out = []
    for b in range(bins):
        mask = idx == b
        n = int(mask.sum())
        k = int(y[mask].sum())
        lo, hi = wilson(k, n)
        out.append(
            {
                "bin": f"{b / bins:.1f}-{(b + 1) / bins:.1f}",
                "n": n,
                "mean_forecast": float(p[mask].mean()) if n else float("nan"),
                "observed": k / n if n else float("nan"),
                "ci_low": lo,
                "ci_high": hi,
            }
        )
    return pd.DataFrame(out)


def ece(table: pd.DataFrame) -> float:
    t = table[table["n"] > 0]
    return float((t["n"] * (t["observed"] - t["mean_forecast"]).abs()).sum() / t["n"].sum())


def murphy(p: np.ndarray, y: np.ndarray, bins: int = N_BINS) -> dict:
    """Binned Murphy decomposition: Brier ~= reliability - resolution + uncertainty
    (exact up to the within-bin spread of forecasts)."""
    base = y.mean()
    idx = np.minimum((p * bins).astype(int), bins - 1)
    rel = res = 0.0
    for b in range(bins):
        mask = idx == b
        n = mask.sum()
        if n == 0:
            continue
        rel += n * (p[mask].mean() - y[mask].mean()) ** 2
        res += n * (y[mask].mean() - base) ** 2
    n = len(y)
    return {"reliability": rel / n, "resolution": res / n, "uncertainty": float(base * (1 - base))}


@dataclass
class SnapshotResult:
    name: str
    n: int
    base_rate: float
    brier: float
    brier_ci: tuple[float, float]
    log_loss: float
    brier_half: float  # always 0.5
    test_n: int
    test_brier: float
    test_brier_base_rate: float  # base rate learned from the EARLIER half, applied to the later half
    test_skill_vs_base_rate: float  # 1 - test_brier / test_brier_base_rate
    test_diff_ci: tuple[float, float]  # CI of (market - base-rate) Brier on the later half
    ece: float
    murphy: dict
    table: pd.DataFrame

    def to_json(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "table"}
        d["reliability_table"] = self.table.to_dict(orient="records")
        return d


def evaluate(df: pd.DataFrame, col: str, rng: np.random.Generator) -> SnapshotResult | None:
    d = df[df[col].notna()].sort_values("end")
    if len(d) < 20:
        return None
    p = d[col].to_numpy(float)
    y = d["y"].to_numpy(float)
    n = len(d)
    ci = bootstrap_ci(lambda i: brier(p[i], y[i]), n, rng)

    # Baseline that can't see the future: the YES rate of the markets that ENDED earlier.
    half = n // 2
    train_y, test_p, test_y = y[:half], p[half:], y[half:]
    base = float(train_y.mean())
    tb = brier(test_p, test_y)
    tbb = brier(np.full_like(test_y, base), test_y)
    m = len(test_y)
    diff_ci = bootstrap_ci(lambda i: brier(test_p[i], test_y[i]) - brier(np.full(len(i), base), test_y[i]), m, rng)
    table = reliability(p, y)
    return SnapshotResult(
        name=col,
        n=n,
        base_rate=float(y.mean()),
        brier=brier(p, y),
        brier_ci=ci,
        log_loss=log_loss(p, y),
        brier_half=0.25,
        test_n=m,
        test_brier=tb,
        test_brier_base_rate=tbb,
        test_skill_vs_base_rate=1 - tb / tbb if tbb > 0 else float("nan"),
        test_diff_ci=diff_ci,
        ece=ece(table),
        murphy=murphy(p, y),
        table=table,
    )


def breakdown(df: pd.DataFrame, col: str, by: str, cuts: list[float], labels: list[str], rng) -> list[dict]:
    d = df[df[col].notna()].copy()
    d["group"] = pd.cut(d[by], bins=cuts, labels=labels, right=False)
    out = []
    for g, part in d.groupby("group", observed=True):
        p, y = part[col].to_numpy(float), part["y"].to_numpy(float)
        if len(part) < 20:
            out.append({"group": str(g), "n": len(part), "note": "fewer than 20 markets: not reported"})
            continue
        lo, hi = bootstrap_ci(lambda i: brier(p[i], y[i]), len(part), rng)
        out.append({"group": str(g), "n": len(part), "base_rate": float(y.mean()), "brier": brier(p, y), "brier_ci": [lo, hi]})
    return out


# --------------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------------


def plot(results: dict[str, SnapshotResult], out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.plot([0, 1], [0, 1], color="#999", lw=1, ls="--", label="perfect calibration")
    for name, color in [("mid", "#1f77b4"), ("1d", "#d62728")]:
        r = results.get(name)
        if r is None:
            continue
        t = r.table[r.table["n"] > 0]
        ax.errorbar(
            t["mean_forecast"], t["observed"],
            yerr=[(t["observed"] - t["ci_low"]).clip(lower=0), (t["ci_high"] - t["observed"]).clip(lower=0)],
            fmt="o-", color=color, capsize=3, label=f"{name} (n={r.n}, ECE={r.ece:.3f})",
        )
    ax.set_xlabel("market probability (bin mean)")
    ax.set_ylabel("fraction resolved YES (95% Wilson CI)")
    ax.set_title("Manifold calibration (play-money markets)")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "reliability.png", dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 4))
    names = [s for s in SNAPSHOTS if s in results]
    vals = [results[s].brier for s in names]
    errs = [[results[s].brier - results[s].brier_ci[0] for s in names], [results[s].brier_ci[1] - results[s].brier for s in names]]
    ax.bar(names, vals, yerr=errs, capsize=4, color="#1f77b4")
    ax.axhline(0.25, color="#999", ls="--", lw=1, label="always 50% (0.25)")
    for i, s in enumerate(names):
        ax.text(i, vals[i] + 0.005, f"n={results[s].n}", ha="center", fontsize=8)
    ax.set_ylabel("Brier score (lower is better), 95% CI")
    ax.set_title("Accuracy by snapshot")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "brier_by_snapshot.png", dpi=150)
    plt.close(fig)


def markdown(results: dict[str, SnapshotResult], extra: dict) -> str:
    lines = [
        "| Snapshot | Markets | YES rate | Brier (95% CI) | Log loss | ECE | Later half: market vs base-rate Brier | Difference (95% CI) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in SNAPSHOTS:
        r = results.get(s)
        if r is None:
            continue
        lines.append(
            f"| {s} | {r.n} | {r.base_rate:.1%} | {r.brier:.4f} ({r.brier_ci[0]:.4f}–{r.brier_ci[1]:.4f}) | "
            f"{r.log_loss:.3f} | {r.ece:.3f} | {r.test_brier:.4f} vs {r.test_brier_base_rate:.4f} (n={r.test_n}) | "
            f"{r.test_brier - r.test_brier_base_rate:+.4f} ({r.test_diff_ci[0]:+.4f} to {r.test_diff_ci[1]:+.4f}) |"
        )
    mid = results.get("mid")
    if mid is not None:
        lines += ["", "Reliability at the midpoint:", "", "| Bin | Markets | Mean forecast | Resolved YES | 95% CI |", "|---|---|---|---|---|"]
        for t in mid.table.itertuples():
            if t.n:
                lines.append(f"| {t.bin} | {t.n} | {t.mean_forecast:.3f} | {t.observed:.3f} | {t.ci_low:.3f}–{t.ci_high:.3f} |")
            else:
                lines.append(f"| {t.bin} | 0 | – | – | – |")
    for title, rows in extra.items():
        lines += ["", f"Midpoint Brier by {title}:", "", "| Group | Markets | YES rate | Brier (95% CI) |", "|---|---|---|---|"]
        for r in rows:
            if "brier" in r:
                lines.append(f"| {r['group']} | {r['n']} | {r['base_rate']:.1%} | {r['brier']:.4f} ({r['brier_ci'][0]:.4f}–{r['brier_ci'][1]:.4f}) |")
            else:
                lines.append(f"| {r['group']} | {r['n']} | – | {r['note']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> dict:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", type=Path, default=here.parent / "pipeline" / "data" / "clean")
    ap.add_argument("--out", type=Path, default=here / "results")
    ap.add_argument("--no-plots", action="store_true")
    a = ap.parse_args(argv)

    markets, bets = load(a.data)
    df = snapshots(markets, bets)
    rng = np.random.default_rng(SEED)
    results = {s: r for s in SNAPSHOTS if (r := evaluate(df, s, rng)) is not None}
    extra = {
        "unique bettors": breakdown(df, "mid", "bettors", [0, 20, 50, math.inf], ["10–19", "20–49", "50+"], rng),
        "trading lifetime": breakdown(df, "mid", "lifetime_days", [0, 7, 30, math.inf], ["< 7 days", "7–30 days", "> 30 days"], rng),
    }
    a.out.mkdir(parents=True, exist_ok=True)
    manifest_path = a.data / "manifest.json"
    summary = {
        "input": {
            "markets_csv_rows": int(len(markets)),
            "bets_csv_rows": int(len(bets)),
            "markets_used": int(len(df)),
            "markets_without_bets": int(df["open"].isna().sum()),
            "data_manifest_sha256": json.loads(manifest_path.read_text())["sha256"] if manifest_path.exists() else None,
        },
        "seed": SEED,
        "bootstrap_reps": BOOTSTRAP,
        "snapshots": {k: v.to_json() for k, v in results.items()},
        "breakdowns": extra,
    }
    (a.out / "calibration.json").write_text(json.dumps(summary, indent=2, default=float))
    (a.out / "calibration.md").write_text(markdown(results, extra))
    if not a.no_plots:
        plot(results, a.out)
    print(markdown(results, extra))
    return summary


if __name__ == "__main__":
    main()
