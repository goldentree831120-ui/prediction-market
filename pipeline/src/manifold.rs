//! Manifold's public read API (https://docs.manifold.markets/api): types and paging.
//! Only GET endpoints that need no key. Field names follow the docs; anything the docs mark
//! optional, or that we don't strictly need, is optional here so a schema change degrades
//! to a missing column instead of a crash.

use crate::source::Source;
use anyhow::{Context, Result};
use serde::Deserialize;

/// Documented maximum page size for /v0/markets and /v0/bets.
pub const PAGE: usize = 1000;

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Market {
    pub id: String,
    pub question: String,
    #[serde(default)]
    pub url: Option<String>,
    pub outcome_type: String,
    #[serde(default)]
    pub mechanism: Option<String>,
    pub created_time: i64,
    #[serde(default)]
    pub close_time: Option<i64>,
    #[serde(default)]
    pub is_resolved: bool,
    #[serde(default)]
    pub resolution: Option<String>,
    #[serde(default)]
    pub resolution_time: Option<i64>,
    #[serde(default)]
    pub resolution_probability: Option<f64>,
    #[serde(default)]
    pub probability: Option<f64>,
    #[serde(default)]
    pub unique_bettor_count: Option<u64>,
    #[serde(default)]
    pub volume: Option<f64>,
    #[serde(default)]
    pub total_liquidity: Option<f64>,
    /// 'MANA' (play money) or 'CASH'. Missing means MANA on older markets.
    #[serde(default)]
    pub token: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Bet {
    pub id: String,
    pub contract_id: String,
    pub created_time: i64,
    #[serde(default)]
    pub amount: Option<f64>,
    #[serde(default)]
    pub shares: Option<f64>,
    #[serde(default)]
    pub outcome: Option<String>,
    #[serde(default)]
    pub prob_before: Option<f64>,
    #[serde(default)]
    pub prob_after: Option<f64>,
    #[serde(default)]
    pub is_redemption: Option<bool>,
    #[serde(default)]
    pub is_cancelled: Option<bool>,
    #[serde(default)]
    pub is_filled: Option<bool>,
    #[serde(default)]
    pub limit_prob: Option<f64>,
    #[serde(default)]
    pub answer_id: Option<String>,
}

/// Pages through GET /v0/markets (newest first) until a market created before `since_ms`,
/// or `max_pages`. Returns every market seen (filtering happens later, so it can be counted).
pub fn fetch_markets(
    src: &mut dyn Source,
    since_ms: i64,
    max_pages: usize,
    page_size: usize,
) -> Result<Vec<Market>> {
    let mut all = Vec::new();
    let mut before: Option<String> = None;
    for _ in 0..max_pages {
        let mut q = format!("/v0/markets?limit={page_size}&sort=created-time&order=desc");
        if let Some(b) = &before {
            q.push_str(&format!("&before={b}"));
        }
        let page: Vec<Market> = serde_json::from_str(&src.get(&q)?)
            .with_context(|| format!("parsing markets page {q}"))?;
        let n = page.len();
        let oldest = page.last().map(|m| m.created_time);
        before = page.last().map(|m| m.id.clone());
        all.extend(page);
        if n < page_size || oldest.is_some_and(|t| t < since_ms) {
            break;
        }
    }
    Ok(all)
}

/// All bets on one market, oldest first. Pages with `before=<bet id>` (newest first, as the API
/// returns them) and reverses at the end. Stops at `max_pages` (and reports truncation).
pub fn fetch_bets(
    src: &mut dyn Source,
    market_id: &str,
    max_pages: usize,
    page_size: usize,
) -> Result<(Vec<Bet>, bool)> {
    let mut all: Vec<Bet> = Vec::new();
    let mut before: Option<String> = None;
    let mut truncated = false;
    for page_no in 0..max_pages {
        let mut q = format!("/v0/bets?contractId={market_id}&limit={page_size}");
        if let Some(b) = &before {
            q.push_str(&format!("&before={b}"));
        }
        let page: Vec<Bet> = serde_json::from_str(&src.get(&q)?)
            .with_context(|| format!("parsing bets page {q}"))?;
        let n = page.len();
        before = page.last().map(|b| b.id.clone());
        all.extend(page);
        if n < page_size {
            break;
        }
        if page_no + 1 == max_pages {
            truncated = true;
        }
    }
    all.sort_by(|a, b| {
        a.created_time
            .cmp(&b.created_time)
            .then_with(|| a.id.cmp(&b.id))
    });
    all.dedup_by(|a, b| a.id == b.id);
    Ok((all, truncated))
}
