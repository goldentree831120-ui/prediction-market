# Do Manifold probabilities move with the news? (P4 M5)

**Data:** 49 hand-picked news-driven markets from the M3 sample ([`news/markets.csv`](news/markets.csv)),
their bets, and daily counts of English-language articles matching a hand-written search for each,
from the [GDELT Project](https://www.gdeltproject.org) DOC 2.0 API (fetched 25–26 October 2026;
2,602 market-days of news). Run on an Intel Core i5-10210U laptop with 8 GB RAM (WSL Ubuntu,
Python 3.12). Full table: [`results/news_study.md`](results/news_study.md); chart:
[`results/news_event_moves.png`](results/news_event_moves.png). Method: see the README's M5 section.
Every number below comes from that run.

## Who was tested

24 of the 49 markets could be tested. The other 25 were set aside by rules fixed before the run:

| Reason | Markets |
|---|---|
| No day passed the spike rule (≥ 20 articles and ≥ 3× the previous two weeks' median) | 9 |
| Too few ordinary days left to compare with (needs 10) | 11 |
| Traded too briefly for a 2-day window before and after | 5 |

Most of the excluded markets are short ones about one big event (the tariff pause, the conclave,
the Canadian election): coverage was high for their whole life, so there is no quiet baseline to
compare against. The tested markets are therefore the longer-running ones. In those 24 markets
there were 142 news-spike days (172 across all 49).

## What the numbers say

**1. In many markets, the probability moves more on news days than on ordinary days.**
Over the spike day and the day after, the probability moved on average **3.1 percentage points more**
than over an ordinary two-day stretch (mean over 13 topics; 95% CI −0.1 to 8.1). It was larger in
14 of 24 markets, and **9** stay significant after correcting for testing 24 markets
(Benjamini–Hochberg, q = 0.10). The clearest cases:

| Market | Move from news day | Ordinary 2-day move |
|---|---|---|
| Apple exempt from US tariffs on China by end of April? | 32.7 | 6.1 |
| US government shutdown in February or March 2025? | 19.6 | 2.8 |
| Musk stops advising Trump and is criticised by him before 2026? | 10.5 | 1.6 |
| Trump meets Putin in person in 2025? | 10.3 | 2.2 |
| AfD gets ≥ 20% in the German election? | 10.3 | 2.5 |
| Pope Leo XIV openly criticises Trump in 2025? | 4.1 | 1.7 |

(Moves are in percentage points of the YES probability.) The topic-level interval just touches
zero, so across topics this is suggestive rather than conclusive; the per-market results are
clearer than the pooled one.

**2. But the markets move about as much in the two days *before* a news spike.**
The move in the two days before the spike day was on average **4.0 points** above ordinary days
(95% CI 0.5 to 8.5), larger in 15 of 24 markets and significant in **7**. In the Apple-tariff
market the move before (30.6) is almost as big as the move after (32.7).

This does **not** mean the markets predicted the news. A likelier reading: news coverage, counted
per day, lags the event itself. A story breaks, traders react within hours, and the coverage
peak that our rule detects comes a day later. With daily counts this study cannot order events
within a day, so it shows that prices and coverage move *together*, not which comes first.

**3. Some markets barely move at all.** In "Will courts overturn the birthright-citizenship
order?" the news-day move is 0.6 points against 0.0 on ordinary days: statistically detectable,
practically tiny, because the price sat near certainty. "Will Trump fire Powell?" didn't move
on news days or otherwise. A significant p-value here means "not chance", not "large".

## Limits

- **Small, hand-picked sample.** 24 tested markets in 13 topics, chosen by me as news-driven.
  Different searches or markets could give different results.
- **Coverage, not importance.** Article counts measure how much was written, not how surprising
  it was. Searches are imperfect and can pick up unrelated stories.
- **Daily resolution.** Hourly counts would separate "before" from "after", but GDELT refused
  nearly all of the ~400 requests they needed (HTTP 429), so the study uses daily counts: one
  request per market.
- **Overlapping days.** Two-day windows on consecutive days overlap and price moves cluster, so
  the permutation p-values (which treat days as exchangeable) are probably somewhat optimistic.
- **Play money.** Manifold uses mana, so this is about play-money markets.
- **No strategy claim.** Nothing here is a trading signal: the moves happen around the coverage,
  not reliably after it.

## What worked, what didn't

- **Worked:** fixing the rules before the run; comparing each market with its own ordinary days;
  checking the move *before* the news as well as after, which is what stopped this from looking
  like "markets react to news" when the timing can't support it; multiple-testing correction;
  a fetcher that cached every answer and survived hours of rate limiting without losing data.
- **Didn't:** hourly data was out of reach under GDELT's rate limits; four hand-written searches
  were rejected at first (GDELT refuses words under three letters and unquoted dashes) and had to
  be rewritten; and half the markets couldn't be tested because their whole life was news.
