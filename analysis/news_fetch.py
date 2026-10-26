"""P4 M5, step 1: daily news-coverage counts from the GDELT DOC 2.0 API.

For each hand-picked market in news/markets.csv, asks GDELT how many English-language articles
matching that market's search were published on each day (UTC), from PRE_DAYS before the study
window (a news baseline) to the end of trading. The study window is the market's last MAX_DAYS
days of trading. GDELT answers a span of more than a week with daily counts in one response, so
this is ONE request per market.

(A first version asked for hourly counts, which GDELT only gives a week at a time: ~400 requests,
and GDELT refused nearly all of them with HTTP 429 even at one request per 30 s. Daily counts
need 49.)

Polite by design: one request at a time, at least GAP_S seconds apart (more after a 429), every
response cached on disk (a re-run makes no requests), backoff on 429/5xx, and it stops after a
few failures in a row instead of retrying hard. No key, no login: GDELT's data is free to use
with a citation (https://www.gdeltproject.org/about.html).

    python news_fetch.py                  # all markets, writes news/data/news_daily.csv
    python news_fetch.py --limit 1        # one market: a quick check that the API answers
    python news_fetch.py --offline        # cache only, no network
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
API = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_AGENT = "prediction-market-research/0.5 (+https://github.com/goldentree831120-ui/prediction-market)"
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
MIN_SPAN_DAYS = 10  # GDELT gives daily steps only for spans over a week; keep a margin
MAX_DAYS = 90  # study window: the last 90 days of trading
PRE_DAYS = 14  # news before the window, for the spike baseline
GAP_S = 10.0  # starting gap; grows by itself when GDELT answers 429 (too many requests)
MAX_GAP_S = 60.0
ATTEMPTS = 6
MAX_FAILURES = 5


class FetchError(RuntimeError):
    pass


def fetch_range(created_ms: int, end_ms: int) -> tuple[int, int]:
    """Whole UTC days to fetch for one market: PRE_DAYS before the study window to the day trading
    ended (inclusive), at least MIN_SPAN_DAYS long so GDELT answers with daily steps."""
    window_start = max(created_ms, end_ms - MAX_DAYS * DAY_MS)
    lo = (window_start - window_start % DAY_MS) - PRE_DAYS * DAY_MS
    hi = end_ms - end_ms % DAY_MS + DAY_MS
    return min(lo, hi - MIN_SPAN_DAYS * DAY_MS), hi


def gdelt_time(ms: int) -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime(ms / 1000))


def build_url(query: str, start_ms: int, end_ms: int) -> str:
    params = {
        "query": f"{query} sourcelang:english",
        "mode": "timelinevolraw",
        "format": "json",
        "startdatetime": gdelt_time(start_ms),
        "enddatetime": gdelt_time(end_ms - 1000),  # last second of the last day
    }
    return API + "?" + urllib.parse.urlencode(params)


_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})T?(\d{2})(\d{2})(\d{2})")


def parse_timeline(text: str) -> list[tuple[int, int, int]]:
    """GDELT timelinevolraw JSON -> [(day_ms, articles, all_articles_monitored)], one row per UTC day
    present. Finer steps (if GDELT ever sends them) are added up into days; days GDELT leaves out
    simply had no matching articles. Raises FetchError on GDELT's plain-text error messages."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        raise FetchError("GDELT did not return JSON: " + text.strip()[:200]) from None
    timeline = doc.get("timeline") or []
    if not timeline:
        return []  # no matching articles at all in this span
    days: dict[int, list[int]] = {}
    for point in timeline[0].get("data", []):
        m = _DATE.match(str(point["date"]))
        if not m:
            raise FetchError(f"unexpected date format: {point['date']!r}")
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        ms = int(pd.Timestamp(year=y, month=mo, day=d, hour=h, minute=mi, second=s, tz="UTC").timestamp() * 1000)
        row = days.setdefault(ms - ms % DAY_MS, [0, 0])
        row[0] += int(point.get("value", 0))
        row[1] += int(point.get("norm", 0))
    return [(t, v, n) for t, (v, n) in sorted(days.items())]


class Client:
    def __init__(self, cache: Path, offline: bool = False, gap_s: float = GAP_S, opener=None, sleep=time.sleep):
        self.cache, self.offline, self.gap_s = cache, offline, gap_s
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self.last = 0.0
        self.requests = 0
        cache.mkdir(parents=True, exist_ok=True)

    def get(self, url: str) -> str:
        path = self.cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if path.exists():
            return path.read_text()
        if self.offline:
            raise FetchError("not in cache (offline): " + url)
        delay = 60.0
        for attempt in range(ATTEMPTS):
            wait = self.last + self.gap_s - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self.last = time.monotonic()
            self.requests += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with self.opener(req, timeout=120) as r:
                    text = r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    if e.code == 429:  # slow down for the rest of the run, not just this request
                        self.gap_s = min(MAX_GAP_S, max(self.gap_s, 1.0) * 1.5)
                    wait_s = max(delay, _retry_after(e))
                    print(f"  HTTP {e.code}, waiting {wait_s:.0f} s (attempt {attempt + 1}/{ATTEMPTS}; "
                          f"gap between requests now {self.gap_s:.0f} s)", flush=True)
                    self.sleep(wait_s)
                    delay = min(delay * 2, 300.0)
                    continue
                raise FetchError(f"HTTP {e.code} for {url}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                print(f"  network error ({e}), waiting {delay:.0f} s", flush=True)
                self.sleep(delay)
                delay = min(delay * 2, 300.0)
                continue
            parse_timeline(text)  # raises on an error message, which is then NOT cached
            path.write_text(text)
            return text
        raise FetchError(f"gave up after {ATTEMPTS} attempts: " + url)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--picks", type=Path, default=HERE / "news" / "markets.csv")
    ap.add_argument("--markets", type=Path, default=HERE.parent / "pipeline" / "data" / "clean" / "markets.csv")
    ap.add_argument("--out", type=Path, default=HERE / "news" / "data")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args(argv)

    picks = pd.read_csv(a.picks)
    if a.limit:
        picks = picks.head(a.limit)
    m = pd.read_csv(a.markets).set_index("id")
    missing = [i for i in picks["id"] if i not in m.index]
    if missing:
        print("not in markets.csv (re-run the M3 fetch?):", missing)
        return 1
    client = Client(a.out / "cache", offline=a.offline)
    total = len(picks)
    print(f"{total} markets, one GDELT request each (cached ones are free), at least {GAP_S:.0f} s apart")

    rows, failures, failed = [], 0, []
    for k, p in enumerate(picks.itertuples(index=False), 1):
        lo, hi = fetch_range(int(m.loc[p.id, "created_time"]), _end(m.loc[p.id]))
        try:
            pts = parse_timeline(client.get(build_url(p.gdelt_query, lo, hi)))
            failures = 0
        except FetchError as e:
            failures += 1
            failed.append(p.id)
            print(f"  [{k}/{total}] {p.id}: {e}", flush=True)
            if failures >= MAX_FAILURES:
                print(f"stopping: {MAX_FAILURES} failures in a row. Wait a while and re-run; cached results are kept.")
                return 1
            continue
        have = {t: (v, n) for t, v, n in pts}
        for t in range(lo, hi, DAY_MS):  # days GDELT leaves out had zero matching articles
            v, n = have.get(t, (0, 0))
            rows.append((p.id, t, v, n))
        peak = max((v for _, v, _ in pts), default=0)
        print(f"[{k}/{total}] {p.id} {p.topic}: {len(range(lo, hi, DAY_MS))} days, busiest day {peak} articles", flush=True)

    out = pd.DataFrame(rows, columns=["market_id", "day_ms", "articles", "all_articles"])
    a.out.mkdir(parents=True, exist_ok=True)
    out.to_csv(a.out / "news_daily.csv", index=False)
    manifest = {
        "source": "GDELT DOC 2.0 API, mode=timelinevolraw, sourcelang:english, daily (https://www.gdeltproject.org)",
        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "markets": int(out["market_id"].nunique()) if len(out) else 0,
        "markets_failed": failed,
        "days": int(len(out)),
        "network_requests": client.requests,
        "max_days": MAX_DAYS,
        "pre_days": PRE_DAYS,
        "sha256_news_daily": hashlib.sha256((a.out / "news_daily.csv").read_bytes()).hexdigest(),
    }
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return 1 if failed else 0


def _retry_after(e: urllib.error.HTTPError) -> float:
    try:
        return float(e.headers.get("Retry-After", 0)) if e.headers else 0.0
    except (TypeError, ValueError):
        return 0.0


def _end(row) -> int:
    ends = [row[c] for c in ("close_time", "resolution_time") if pd.notna(row[c])]
    return int(min(ends))


if __name__ == "__main__":
    sys.exit(main())
