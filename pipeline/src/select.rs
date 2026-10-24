//! Which markets go into the dataset, and a count of everything left out and why, so the
//! sample can be described honestly in M4.

use crate::manifold::Market;
use std::collections::BTreeMap;

#[derive(Debug, Clone)]
pub struct Criteria {
    pub since_ms: i64,
    pub until_ms: i64,
    pub min_bettors: u64,
    pub include_cash: bool,
}

#[derive(Debug, Default, serde::Serialize)]
pub struct Selection {
    pub selected: Vec<String>,
    /// reason -> count
    pub excluded: BTreeMap<String, u64>,
}

/// Why a market is excluded, or None if it's in. Order matters: the first reason is counted.
pub fn exclusion(m: &Market, c: &Criteria) -> Option<&'static str> {
    if m.created_time < c.since_ms || m.created_time >= c.until_ms {
        return Some("created outside the window");
    }
    if m.outcome_type != "BINARY" {
        return Some("not a binary market");
    }
    if !m.is_resolved {
        return Some("not resolved yet");
    }
    match m.resolution.as_deref() {
        Some("YES") | Some("NO") => {}
        Some("MKT") => return Some("resolved to a probability (MKT)"),
        Some("CANCEL") => return Some("cancelled (N/A)"),
        _ => return Some("unknown resolution"),
    }
    if !c.include_cash && m.token.as_deref() == Some("CASH") {
        return Some("CASH-token market (only MANA included)");
    }
    if m.unique_bettor_count.unwrap_or(0) < c.min_bettors {
        return Some("too few unique bettors");
    }
    None
}

pub fn select(markets: &[Market], c: &Criteria) -> Selection {
    let mut s = Selection::default();
    for m in markets {
        match exclusion(m, c) {
            None => s.selected.push(m.id.clone()),
            Some(reason) => *s.excluded.entry(reason.to_string()).or_insert(0) += 1,
        }
    }
    s
}
