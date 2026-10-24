use pm_fetch::manifold::{fetch_bets, fetch_markets};
use pm_fetch::select::{select, Criteria};
use pm_fetch::source::{FixtureSource, HttpSource, Source};
use pm_fetch::{run, Limits};
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::thread;
use std::time::{Duration, Instant};

const JAN_2025: i64 = 1_735_689_600_000;
const JUL_2025: i64 = 1_751_328_000_000;

fn fixtures() -> FixtureSource {
    FixtureSource::new(&Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")).unwrap()
}

fn criteria() -> Criteria {
    Criteria {
        since_ms: JAN_2025,
        until_ms: JUL_2025,
        min_bettors: 10,
        include_cash: false,
    }
}

fn limits() -> Limits {
    Limits {
        page_size: 2,
        max_market_pages: 100,
        max_markets: 100,
        max_bet_pages: 100,
    }
}

#[test]
fn market_paging_stops_at_the_window_start() {
    let mut src = fixtures();
    let markets = fetch_markets(&mut src, JAN_2025, 100, 2).unwrap();
    assert_eq!(markets.len(), 8);
    // the 4th page contains a market created before the window, so there's no 5th request
    assert_eq!(src.requested.len(), 4);
    assert!(src.requested[3].ends_with("&before=m3"));
}

#[test]
fn market_paging_respects_max_pages() {
    let mut src = fixtures();
    let markets = fetch_markets(&mut src, JAN_2025, 2, 2).unwrap();
    assert_eq!(markets.len(), 4);
    assert_eq!(src.requested.len(), 2);
}

#[test]
fn selection_keeps_resolved_binary_yes_no_and_counts_every_exclusion() {
    let mut src = fixtures();
    let markets = fetch_markets(&mut src, JAN_2025, 100, 2).unwrap();
    let s = select(&markets, &criteria());
    assert_eq!(s.selected, vec!["m8", "m6"]);
    let ex: Vec<(&str, u64)> = s.excluded.iter().map(|(k, v)| (k.as_str(), *v)).collect();
    assert_eq!(
        ex,
        vec![
            ("CASH-token market (only MANA included)", 1),
            ("cancelled (N/A)", 1),
            ("created outside the window", 1),
            ("not a binary market", 1),
            ("resolved to a probability (MKT)", 1),
            ("too few unique bettors", 1),
        ]
    );
    assert_eq!(
        s.selected.len() as u64 + s.excluded.values().sum::<u64>(),
        8,
        "every market accounted for"
    );
}

#[test]
fn cash_markets_can_be_included_on_request() {
    let mut src = fixtures();
    let markets = fetch_markets(&mut src, JAN_2025, 100, 2).unwrap();
    let s = select(
        &markets,
        &Criteria {
            include_cash: true,
            ..criteria()
        },
    );
    assert!(s.selected.contains(&"m2".to_string()));
}

#[test]
fn bets_are_paged_and_returned_oldest_first() {
    let mut src = fixtures();
    let (bets, truncated) = fetch_bets(&mut src, "m6", 100, 2).unwrap();
    let ids: Vec<&str> = bets.iter().map(|b| b.id.as_str()).collect();
    assert_eq!(ids, vec!["b1", "b2", "b3"]);
    assert!(!truncated);
    assert_eq!(bets[0].is_redemption, Some(true));
    assert_eq!(bets[0].limit_prob, Some(0.4));
}

#[test]
fn bet_truncation_is_reported() {
    let mut src = fixtures();
    let (bets, truncated) = fetch_bets(&mut src, "m6", 1, 2).unwrap();
    assert_eq!(bets.len(), 2);
    assert!(truncated);
}

#[test]
fn full_run_writes_csvs_and_an_honest_manifest() {
    let dir = tempfile::tempdir().unwrap();
    let mut src = fixtures();
    let m = run(&mut src, dir.path(), &criteria(), &limits(), "fixture").unwrap();
    assert_eq!(m.markets_seen, 8);
    assert_eq!(m.markets_selected, 2);
    assert_eq!(m.markets_written, 2);
    assert_eq!(
        m.bets_written, 3,
        "4 bets fetched, 1 with an impossible probability dropped"
    );
    assert_eq!(m.bets_dropped_invalid_prob, 1);
    assert_eq!(m.resolution_counts.get("YES"), Some(&1));
    assert_eq!(m.resolution_counts.get("NO"), Some(&1));
    assert!(m.markets_with_truncated_bets.is_empty());

    let markets = std::fs::read_to_string(dir.path().join("markets.csv")).unwrap();
    let lines: Vec<&str> = markets.lines().collect();
    assert_eq!(lines.len(), 3);
    assert!(lines[0].starts_with("id,question,url,created_time"));
    assert!(lines[1].starts_with("m6,"), "oldest market first");
    assert!(lines[2].starts_with("m8,"));

    let bets = std::fs::read_to_string(dir.path().join("bets.csv")).unwrap();
    let rows: Vec<&str> = bets.lines().skip(1).collect();
    assert_eq!(rows.len(), 3);
    assert!(rows[0].starts_with("m6,b1,"));
    assert!(
        rows[0].contains(",true,0.4"),
        "redemption flag and limit prob kept: {}",
        rows[0]
    );
    assert!(
        !bets.contains(",1.7,"),
        "the impossible probability is not written"
    );

    let manifest: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(dir.path().join("manifest.json")).unwrap())
            .unwrap();
    assert_eq!(manifest["sha256"]["bets.csv"].as_str().unwrap().len(), 64);
    assert!(manifest["terms_note"]
        .as_str()
        .unwrap()
        .contains("non-commercial"));
}

#[test]
fn max_markets_cap_takes_a_deterministic_sample_and_is_recorded() {
    let pick = || {
        let dir = tempfile::tempdir().unwrap();
        let mut src = fixtures();
        let m = run(
            &mut src,
            dir.path(),
            &criteria(),
            &Limits {
                max_markets: 1,
                ..limits()
            },
            "fixture",
        )
        .unwrap();
        assert!(m.capped_at_max_markets);
        assert_eq!(m.markets_written, 1);
        std::fs::read_to_string(dir.path().join("markets.csv")).unwrap()
    };
    assert_eq!(pick(), pick(), "same inputs, same sample");
}

// ---- HttpSource against a tiny local server -------------------------------------------------

/// Serves the given raw HTTP responses, one per connection, and records the request lines.
fn serve(responses: Vec<&'static str>) -> (String, thread::JoinHandle<Vec<String>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let h = thread::spawn(move || {
        let mut seen = Vec::new();
        for r in responses {
            let (mut s, _) = listener.accept().unwrap();
            let mut buf = [0u8; 4096];
            let n = s.read(&mut buf).unwrap();
            let req = String::from_utf8_lossy(&buf[..n]).to_string();
            seen.push(req.lines().next().unwrap_or("").to_string());
            s.write_all(r.as_bytes()).unwrap();
        }
        seen
    });
    (base, h)
}

const OK: &str = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]";
const TOO_MANY: &str = "HTTP/1.1 429 Too Many Requests\r\nRetry-After: 0\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";

#[test]
fn http_source_uses_get_caches_and_works_offline_afterwards() {
    let (base, server) = serve(vec![OK]);
    let cache = tempfile::tempdir().unwrap();
    let mut src = HttpSource::new(&base, cache.path(), Duration::ZERO, false, false).unwrap();
    assert_eq!(src.get("/v0/markets?limit=1").unwrap(), "[]");
    assert_eq!(src.network_requests(), 1);
    let seen = server.join().unwrap();
    assert!(
        seen[0].starts_with("GET /v0/markets?limit=1 "),
        "read-only: {}",
        seen[0]
    );

    // second call: served from the cache, no network (the server is gone)
    assert_eq!(src.get("/v0/markets?limit=1").unwrap(), "[]");
    assert_eq!(src.network_requests(), 1);

    let mut offline = HttpSource::new(&base, cache.path(), Duration::ZERO, false, true).unwrap();
    assert_eq!(offline.get("/v0/markets?limit=1").unwrap(), "[]");
    assert!(
        offline.get("/v0/markets?limit=2").is_err(),
        "offline and not cached"
    );
}

#[test]
fn http_source_backs_off_on_429_and_retries() {
    let (base, server) = serve(vec![TOO_MANY, OK]);
    let cache = tempfile::tempdir().unwrap();
    let mut src = HttpSource::new(&base, cache.path(), Duration::ZERO, false, false).unwrap();
    assert_eq!(src.get("/v0/bets?contractId=x&limit=1").unwrap(), "[]");
    assert_eq!(src.network_requests(), 2);
    assert_eq!(server.join().unwrap().len(), 2);
}

#[test]
fn http_source_spaces_out_requests() {
    let (base, server) = serve(vec![OK, OK, OK]);
    let cache = tempfile::tempdir().unwrap();
    let mut src =
        HttpSource::new(&base, cache.path(), Duration::from_millis(150), true, false).unwrap();
    let t = Instant::now();
    for i in 0..3 {
        src.get(&format!("/v0/markets?limit={i}")).unwrap();
    }
    assert!(
        t.elapsed() >= Duration::from_millis(300),
        "3 requests need at least 2 gaps of 150 ms"
    );
    server.join().unwrap();
}
