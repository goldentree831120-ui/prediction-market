use anyhow::{bail, Result};
use clap::Parser;
use pm_fetch::select::Criteria;
use pm_fetch::source::HttpSource;
use pm_fetch::{run, Limits};
use std::path::PathBuf;
use std::time::Duration;

/// Read-only fetcher for public Manifold Markets data: resolved binary markets and their bet
/// histories, written as CSV with a manifest. GET requests only, no API key, rate-limited.
#[derive(Parser)]
#[command(version)]
struct Args {
    /// Markets created on or after this date (YYYY-MM-DD, UTC)
    #[arg(long, default_value = "2025-01-01")]
    since: String,
    /// ...and before this date (YYYY-MM-DD, UTC)
    #[arg(long, default_value = "2025-07-01")]
    until: String,
    /// Minimum unique bettors for a market to be included
    #[arg(long, default_value_t = 10)]
    min_bettors: u64,
    /// Also include CASH-token markets (default: play-money MANA only)
    #[arg(long)]
    include_cash: bool,
    /// Cap on markets whose bets are fetched (a deterministic sample if more qualify)
    #[arg(long, default_value_t = 1500)]
    max_markets: usize,
    /// Cap on market-list pages (1000 markets each)
    #[arg(long, default_value_t = 400)]
    max_market_pages: usize,
    /// Cap on bet pages per market (1000 bets each); truncation is recorded in the manifest
    #[arg(long, default_value_t = 20)]
    max_bet_pages: usize,
    /// Milliseconds between requests (250 = 240/min, half the documented 500/min limit)
    #[arg(long, default_value_t = 250)]
    min_interval_ms: u64,
    /// Output directory for CSV files and the manifest
    #[arg(long, default_value = "data/clean")]
    out: PathBuf,
    /// Raw response cache (reruns reuse it unless --refresh)
    #[arg(long, default_value = "data/raw")]
    cache: PathBuf,
    /// Ignore the cache and fetch everything again
    #[arg(long)]
    refresh: bool,
    /// Never touch the network; use only cached responses
    #[arg(long)]
    offline: bool,
    #[arg(long, default_value = "https://api.manifold.markets", hide = true)]
    base: String,
}

fn parse_date_ms(s: &str) -> Result<i64> {
    let p: Vec<&str> = s.split('-').collect();
    if p.len() != 3 {
        bail!("date must be YYYY-MM-DD: {s}");
    }
    let (y, m, d): (i64, i64, i64) = (p[0].parse()?, p[1].parse()?, p[2].parse()?);
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) {
        bail!("bad date {s}");
    }
    // Days since 1970-01-01 (proleptic Gregorian), from Howard Hinnant's algorithm.
    let y2 = if m <= 2 { y - 1 } else { y };
    let era = y2.div_euclid(400);
    let yoe = y2 - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    Ok(days * 86_400_000)
}

fn main() -> Result<()> {
    let a = Args::parse();
    let c = Criteria {
        since_ms: parse_date_ms(&a.since)?,
        until_ms: parse_date_ms(&a.until)?,
        min_bettors: a.min_bettors,
        include_cash: a.include_cash,
    };
    if c.since_ms >= c.until_ms {
        bail!("--since must be before --until");
    }
    let limits = Limits {
        page_size: pm_fetch::manifold::PAGE,
        max_market_pages: a.max_market_pages,
        max_markets: a.max_markets,
        max_bet_pages: a.max_bet_pages,
    };
    let mut src = HttpSource::new(
        &a.base,
        &a.cache,
        Duration::from_millis(a.min_interval_ms),
        a.refresh,
        a.offline,
    )?;
    eprintln!(
        "pm-fetch: markets created {} .. {}, >= {} bettors -> {}",
        a.since,
        a.until,
        a.min_bettors,
        a.out.display()
    );
    let m = run(&mut src, &a.out, &c, &limits, &a.base)?;
    eprintln!(
        "done: {} markets seen, {} selected, {} written ({} bets); {} network requests",
        m.markets_seen, m.markets_selected, m.markets_written, m.bets_written, m.network_requests
    );
    for (reason, n) in &m.excluded {
        eprintln!("  excluded {n:>7}  {reason}");
    }
    if m.capped_at_max_markets {
        eprintln!("  note: capped at --max-markets {}", a.max_markets);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::parse_date_ms;

    #[test]
    fn dates_convert_to_unix_ms() {
        assert_eq!(parse_date_ms("1970-01-01").unwrap(), 0);
        assert_eq!(parse_date_ms("2000-03-01").unwrap(), 951_868_800_000);
        assert_eq!(parse_date_ms("2025-01-01").unwrap(), 1_735_689_600_000);
        assert!(parse_date_ms("2025-13-01").is_err());
        assert!(parse_date_ms("2025/01/01").is_err());
    }
}
