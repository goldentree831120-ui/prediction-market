"""P4 M6: a local dashboard for the prediction-market project.

    cd analysis && ./run_dashboard.sh        # opens http://localhost:8501

Reads only local files (see dashboard_data.py); makes no network calls of its own.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

import dashboard_data as dd
from pool import WAD, Pool, PoolError

st.set_page_config(page_title="Prediction market lab", layout="wide")
BLUE = "#4c9be8"  # readable on both the light and the dark theme
P = dd.paths()

st.title("Prediction market lab")
st.caption(
    "Play-money Manifold markets created January to June 2025. Descriptive research only: "
    "nothing here is a trading strategy."
)

tab_cal, tab_news, tab_pool, tab_about = st.tabs(["Calibration (M4)", "News study (M5)", "Pool calculator (M2)", "About"])


# ---------------------------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------------------------

with tab_cal:
    cal = dd.load_json(P["results"] / "calibration.json")
    if cal is None:
        st.info("No results/calibration.json yet: run `./run.sh` first (M4).")
    else:
        inp = cal["input"]
        st.markdown(f"**{inp['markets_used']:,} resolved yes/no markets**, {inp['bets_csv_rows']:,} bets.")
        snaps = list(cal["snapshots"])
        snap = st.radio("Probability taken at", snaps, index=snaps.index("mid") if "mid" in snaps else 0, horizontal=True,
                        help="open = creator's opening price; mid = halfway through trading; 30d/7d/1d = days before trading ended")
        s = cal["snapshots"][snap]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Markets", f"{s['n']:,}")
        c2.metric("Brier score", f"{s['brier']:.3f}", help=f"95% CI {s['brier_ci'][0]:.3f}–{s['brier_ci'][1]:.3f}; always-50% scores 0.25")
        c3.metric("Calibration error (ECE)", f"{s['ece']:.3f}")
        c4.metric("Skill vs base rate (later half)", f"{s['test_skill_vs_base_rate']:.0%}",
                  help="1 − market Brier / base-rate Brier, on markets that ended later than those the base rate was learned from")

        t = dd.reliability_frame(cal, snap)
        diag = alt.Chart(pd.DataFrame({"x": [0, 1], "y": [0, 1]})).mark_line(strokeDash=[4, 4], color="gray").encode(x="x", y="y")
        pts = alt.Chart(t).encode(
            x=alt.X("mean_forecast:Q", title="market probability (bin mean)", scale=alt.Scale(domain=[0, 1])),
            y=alt.Y("observed:Q", title="fraction resolved YES", scale=alt.Scale(domain=[0, 1])),
            tooltip=["bin", "n", alt.Tooltip("mean_forecast", format=".3f"), alt.Tooltip("observed", format=".3f"),
                     alt.Tooltip("ci_low", format=".3f"), alt.Tooltip("ci_high", format=".3f")],
        )
        bars = pts.mark_rule(color=BLUE, strokeWidth=2).encode(y="ci_low:Q", y2="ci_high:Q")
        st.altair_chart((diag + bars + pts.mark_circle(size=80, color=BLUE, opacity=1)).properties(height=420, title=f"Reliability at '{snap}' (95% Wilson CIs)"),
                        use_container_width=True)
        st.caption("Points below the dashed line: YES happened less often than the price said (YES overpriced).")

        st.subheader("All snapshots")
        st.dataframe(dd.snapshot_frame(cal).style.format(precision=3), hide_index=True, width="stretch")
        st.caption("The 30d/7d/1d snapshots cover different subsets of markets (only those that traded that long), "
                   "so they are not a like-for-like comparison.")
        for title, rows in cal.get("breakdowns", {}).items():
            st.markdown(f"**Midpoint Brier by {title}** (confounded with question difficulty and YES rate; not causal)")
            st.dataframe(dd.breakdown_frame(rows), hide_index=True, width="stretch")


# ---------------------------------------------------------------------------------------------
# News study
# ---------------------------------------------------------------------------------------------

with tab_news:
    news = dd.load_json(P["results"] / "news_study.json")
    if news is None:
        st.info("No results/news_study.json yet: run `./run_news.sh` first (M5).")
    else:
        df = dd.news_frame(news)
        r, lead = news["reaction"], news["lead"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Markets tested", f"{news['markets_tested']} of {news['markets_picked']}")
        if r.get("markets"):
            c2.metric("Extra move from a news day", f"{100 * r['mean_over_topics']:.1f} pts",
                      help=f"mean over {r['topics']} topics, 95% CI {100 * r['ci'][0]:.1f} to {100 * r['ci'][1]:.1f}; "
                           f"significant after BH in {r['bh_significant']} markets")
        if lead.get("markets"):
            c3.metric("Extra move BEFORE a news day", f"{100 * lead['mean_over_topics']:.1f} pts",
                      help=f"95% CI {100 * lead['ci'][0]:.1f} to {100 * lead['ci'][1]:.1f}; significant after BH in {lead['bh_significant']} markets")
        if r.get("markets") and lead.get("markets") and lead["mean_over_topics"] >= 0.5 * r["mean_over_topics"]:
            st.caption("Prices move about as much before a news-coverage spike as after it: daily coverage lags the "
                       "events traders react to, so this shows co-movement, not prediction.")
        else:
            st.caption("Daily data can't order events within a day: read these as co-movement, not cause and effect.")

        tested = df[df["tested"]].sort_values("reaction_p")
        show = tested[["question", "topic", "spikes", "reaction_after_news", "reaction_normal", "reaction_p", "reaction_bh",
                       "lead_before_news", "lead_p", "lead_bh"]].copy()
        for c in ["reaction_after_news", "reaction_normal", "lead_before_news"]:
            show[c] = (100 * show[c]).round(1)
        for c in ["reaction_p", "lead_p"]:
            show[c] = show[c].map(lambda x: "<0.001" if x < 0.001 else f"{x:.3f}")
        show["spikes"] = show["spikes"].astype(int)
        show.columns = ["Market", "Topic", "Spike days", "Move from news day (pts)", "Ordinary 2-day move (pts)", "p",
                        "BH", "Move before news (pts)", "p (before)", "BH (before)"]
        st.dataframe(show, hide_index=True, width="stretch")
        with st.expander(f"Not tested ({int((~df['tested']).sum())} markets) and why"):
            st.dataframe(df[~df["tested"]][["question", "topic", "note"]], hide_index=True, width="stretch")

        st.subheader("One market: probability and news coverage")
        labels = {f"{q} [{i}]": i for q, i in zip(df["question"], df["id"])}
        choice = st.selectbox("Market", list(labels))
        mid = labels[choice]
        series = dd.market_series(mid, P["data"], P["news"])
        if series is None:
            st.info("Per-market charts need the local data from M3 and M5 (pipeline/data/clean and news/data), "
                    "which is not in the repo. Run the fetchers on this machine to see them.")
        else:
            prob, counts = series
            row = df.set_index("id").loc[mid]
            spikes = pd.DataFrame({"date": pd.to_datetime(row.get("spike_days") or [])})
            base = alt.Chart(prob).mark_line().encode(
                x=alt.X("date:T", title=None), y=alt.Y("probability:Q", scale=alt.Scale(domain=[0, 1]), title="P(YES)"))
            rules = alt.Chart(spikes).mark_rule(color="#d62728", strokeDash=[3, 3]).encode(x="date:T") if len(spikes) else None
            top = base + rules if rules is not None else base
            bottom = alt.Chart(counts).mark_bar(color="#888").encode(x=alt.X("date:T", title=None), y=alt.Y("articles:Q", title="articles / day"))
            # one shared time axis, so a news bar sits exactly under the price on the same day
            st.altair_chart(alt.vconcat(top.properties(height=220), bottom.properties(height=120)).resolve_scale(x="shared"),
                            use_container_width=True)
            st.caption(f"Red dashed lines: news-spike days used in the study ({len(spikes)}). Resolved: {row['resolution']}.")


# ---------------------------------------------------------------------------------------------
# Pool calculator
# ---------------------------------------------------------------------------------------------

with tab_pool:
    st.markdown("The same constant-product maths as `src/FixedProductMarketMaker.sol`, computed with the "
                "same integer rounding (a test checks both give identical numbers).")
    c1, c2, c3 = st.columns(3)
    liquidity = c1.number_input("Pool liquidity (tokens)", min_value=1.0, value=1000.0, step=100.0)
    opening = c2.slider("Opening probability of YES (%)", 1, 99, 50)
    fee = c3.slider("Fee (%)", 0.0, 10.0, 2.0, step=0.5)
    c4, c5 = st.columns(2)
    side = c4.radio("Buy", ["YES", "NO"], horizontal=True)
    spend = c5.number_input("Spend (tokens)", min_value=0.0, value=100.0, step=10.0)
    try:
        pool = Pool.seed(int(liquidity * WAD), opening * 100, int(round(fee * 100)))
        is_yes = side == "YES"
        if spend > 0:
            out, fee_paid = pool.quote_buy(is_yes, int(spend * WAD))
            after = pool.after_buy(is_yes, int(spend * WAD))
            before_p = pool.price_yes() if is_yes else 1 - pool.price_yes()
            after_p = after.price_yes() if is_yes else 1 - after.price_yes()
            k1, k2, k3, k4 = st.columns(4)
            k1.metric(f"{side} tokens received", f"{out / WAD:,.2f}")
            k2.metric("Average price paid", f"{spend / (out / WAD):.3f}")
            k3.metric(f"{side} price", f"{after_p:.3f}", delta=f"{after_p - before_p:+.3f}")
            k4.metric("Fee (stays in the pool)", f"{fee_paid / WAD:,.2f}")
        sizes = [liquidity * f for f in (0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0)]
        curve = pd.DataFrame({
            "spend": sizes,
            "price after": [(lambda a: a.price_yes() if is_yes else 1 - a.price_yes())(pool.after_buy(is_yes, int(x * WAD))) for x in sizes],
        })
        st.altair_chart(alt.Chart(curve).mark_line(point=True).encode(
            x=alt.X("spend:Q", title=f"tokens spent buying {side}"), y=alt.Y("price after:Q", title=f"{side} price after", scale=alt.Scale(domain=[0, 1]))
        ).properties(height=300, title="Price impact: bigger trades move the price more"), use_container_width=True)
        st.caption("Each full set (1 YES + 1 NO) is backed by 1 collateral token, so the YES and NO prices always sum to 1.")
    except PoolError as e:
        st.warning(str(e))


# ---------------------------------------------------------------------------------------------
# About
# ---------------------------------------------------------------------------------------------

with tab_about:
    st.markdown(
        """
- **Data:** Manifold's public API (terms allow academic, personal, non-commercial use) and the
  [GDELT Project](https://www.gdeltproject.org) news API (free with citation). Polymarket, Kraken,
  Coinbase and Kalshi data were not used: their terms forbid it for this purpose.
- **Limits:** play money; markets with 10+ bettors created in one six-month window; a hand-picked
  news sample of 49 markets, 24 testable; daily news resolution.
- **No strategy is claimed.** A small YES overpricing and price moves around news are descriptive
  findings, not trading signals.
- Write-ups: `analysis/FINDINGS.md` (calibration), `analysis/NEWS_FINDINGS.md` (news), `NOTE.md` (whole project).
"""
    )
