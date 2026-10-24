//! pm-fetch: a read-only, rate-limited fetcher for public Manifold Markets data.
//!
//! Pipeline: page through all markets created in a time window -> keep resolved binary
//! YES/NO markets with enough bettors (counting every exclusion by reason) -> fetch each
//! one's full bet history -> write `markets.csv`, `bets.csv` and `manifest.json`.

pub mod manifold;
pub mod select;
pub mod source;

use anyhow::Result;
use manifold::{fetch_bets, fetch_markets, Market};
use select::{select, Criteria};
use serde::Serialize;
use sha2::{Digest, Sha256};
use source::Source;
use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::path::Path;

pub struct Limits {
    /// Page size for list endpoints (the API maximum, 1000, except in tests).
    pub page_size: usize,
    pub max_market_pages: usize,
    pub max_markets: usize,
    pub max_bet_pages: usize,
}

#[derive(Debug, Serialize)]
pub struct Manifest {
    pub tool: String,
    pub source: String,
    pub terms_note: String,
    pub generated_at_unix: u64,
    pub window_created_ms: [i64; 2],
    pub min_bettors: u64,
    pub include_cash: bool,
    pub markets_seen: usize,
    pub markets_selected: usize,
    pub markets_written: usize,
    pub capped_at_max_markets: bool,
    pub excluded: BTreeMap<String, u64>,
    pub resolution_counts: BTreeMap<String, u64>,
    pub bets_written: usize,
    pub bets_dropped_invalid_prob: usize,
    pub markets_with_truncated_bets: Vec<String>,
    pub network_requests: u64,
    pub sha256: BTreeMap<String, String>,
}

#[derive(Serialize)]
struct MarketRow<'a> {
    id: &'a str,
    question: &'a str,
    url: &'a str,
    created_time: i64,
    close_time: Option<i64>,
    resolution_time: Option<i64>,
    resolution: &'a str,
    unique_bettors: Option<u64>,
    volume: Option<f64>,
    total_liquidity: Option<f64>,
    mechanism: &'a str,
    token: &'a str,
    n_bets: usize,
    bets_truncated: bool,
}

#[derive(Serialize)]
struct BetRow<'a> {
    market_id: &'a str,
    bet_id: &'a str,
    created_time: i64,
    outcome: &'a str,
    amount: Option<f64>,
    shares: Option<f64>,
    prob_before: f64,
    prob_after: f64,
    is_redemption: bool,
    limit_prob: Option<f64>,
}

/// A bet is usable for a probability history if it has both probabilities in [0, 1].
fn valid_prob(p: Option<f64>) -> Option<f64> {
    p.filter(|x| x.is_finite() && (0.0..=1.0).contains(x))
}

pub fn run(
    src: &mut dyn Source,
    out_dir: &Path,
    c: &Criteria,
    limits: &Limits,
    source_base: &str,
) -> Result<Manifest> {
    fs::create_dir_all(out_dir)?;
    let markets = fetch_markets(src, c.since_ms, limits.max_market_pages, limits.page_size)?;
    let sel = select(&markets, c);
    let by_id: HashMap<&str, &Market> = markets.iter().map(|m| (m.id.as_str(), m)).collect();

    // If there are more markets than the cap, take a deterministic pseudo-random sample (ordered
    // by a hash of the market id) rather than the oldest or newest, so the sample spreads over the
    // whole window and the same inputs always give the same sample. Output is oldest first.
    let mut chosen: Vec<&Market> = sel
        .selected
        .iter()
        .filter_map(|id| by_id.get(id.as_str()).copied())
        .collect();
    let capped = chosen.len() > limits.max_markets;
    chosen.sort_by_key(|m| Sha256::digest(m.id.as_bytes()));
    chosen.truncate(limits.max_markets);
    chosen.sort_by(|a, b| {
        a.created_time
            .cmp(&b.created_time)
            .then_with(|| a.id.cmp(&b.id))
    });

    let markets_path = out_dir.join("markets.csv");
    let bets_path = out_dir.join("bets.csv");
    let mut mw = csv::Writer::from_path(&markets_path)?;
    let mut bw = csv::Writer::from_path(&bets_path)?;
    let mut bets_written = 0usize;
    let mut dropped = 0usize;
    let mut truncated_ids = Vec::new();
    let mut resolution_counts: BTreeMap<String, u64> = BTreeMap::new();

    for (i, m) in chosen.iter().enumerate() {
        if (i + 1) % 100 == 0 {
            eprintln!("  bets: {}/{} markets", i + 1, chosen.len());
        }
        let (bets, truncated) = fetch_bets(src, &m.id, limits.max_bet_pages, limits.page_size)?;
        if truncated {
            truncated_ids.push(m.id.clone());
        }
        let mut n = 0usize;
        for b in &bets {
            if b.contract_id != m.id {
                continue; // defensive: the API should only return this market's bets
            }
            let (Some(pb), Some(pa)) = (valid_prob(b.prob_before), valid_prob(b.prob_after)) else {
                dropped += 1;
                continue;
            };
            bw.serialize(BetRow {
                market_id: &m.id,
                bet_id: &b.id,
                created_time: b.created_time,
                outcome: b.outcome.as_deref().unwrap_or(""),
                amount: b.amount,
                shares: b.shares,
                prob_before: pb,
                prob_after: pa,
                is_redemption: b.is_redemption.unwrap_or(false),
                limit_prob: b.limit_prob,
            })?;
            n += 1;
        }
        bets_written += n;
        let res = m.resolution.as_deref().unwrap_or("");
        *resolution_counts.entry(res.to_string()).or_insert(0) += 1;
        mw.serialize(MarketRow {
            id: &m.id,
            question: &m.question,
            url: m.url.as_deref().unwrap_or(""),
            created_time: m.created_time,
            close_time: m.close_time,
            resolution_time: m.resolution_time,
            resolution: res,
            unique_bettors: m.unique_bettor_count,
            volume: m.volume,
            total_liquidity: m.total_liquidity,
            mechanism: m.mechanism.as_deref().unwrap_or(""),
            token: m.token.as_deref().unwrap_or("MANA"),
            n_bets: n,
            bets_truncated: truncated,
        })?;
    }
    mw.flush()?;
    bw.flush()?;

    let mut sha = BTreeMap::new();
    for p in [&markets_path, &bets_path] {
        let digest = Sha256::digest(fs::read(p)?);
        sha.insert(
            p.file_name().unwrap().to_string_lossy().to_string(),
            hex::encode(digest),
        );
    }

    let manifest = Manifest {
        tool: concat!("pm-fetch ", env!("CARGO_PKG_VERSION")).to_string(),
        source: source_base.to_string(),
        terms_note: "Manifold public API, read-only, no key. Manifold's API docs allow academic, personal and \
                     non-commercial use. Data are not committed to the repository; rerun to rebuild."
            .to_string(),
        generated_at_unix: source::now_unix(),
        window_created_ms: [c.since_ms, c.until_ms],
        min_bettors: c.min_bettors,
        include_cash: c.include_cash,
        markets_seen: markets.len(),
        markets_selected: sel.selected.len(),
        markets_written: chosen.len(),
        capped_at_max_markets: capped,
        excluded: sel.excluded,
        resolution_counts,
        bets_written,
        bets_dropped_invalid_prob: dropped,
        markets_with_truncated_bets: truncated_ids,
        network_requests: src.network_requests(),
        sha256: sha,
    };
    fs::write(
        out_dir.join("manifest.json"),
        serde_json::to_string_pretty(&manifest)?,
    )?;
    Ok(manifest)
}
