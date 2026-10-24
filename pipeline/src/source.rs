//! Where JSON comes from: the live API (rate-limited, retried, cached on disk) or, in tests,
//! a directory of recorded responses. Everything above this layer only sees `Source`.

use anyhow::{anyhow, bail, Context, Result};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::thread::sleep;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub trait Source {
    /// GET `path_and_query` (e.g. `/v0/markets?limit=1000`) and return the response body.
    fn get(&mut self, path_and_query: &str) -> Result<String>;
    /// Number of requests that actually went over the network (cache hits excluded).
    fn network_requests(&self) -> u64;
}

/// Cache entry: the raw body plus where and when it was fetched, so every number in the
/// analysis can be traced back to a response.
#[derive(serde::Serialize, serde::Deserialize)]
struct CacheEntry {
    url: String,
    fetched_at_unix: u64,
    body: String,
}

pub struct HttpSource {
    base: String,
    agent: ureq::Agent,
    min_interval: Duration,
    last_request: Option<Instant>,
    cache_dir: PathBuf,
    refresh: bool,
    offline: bool,
    requests: u64,
    max_retries: u32,
}

impl HttpSource {
    /// `min_interval` between network requests: 250 ms = 240/min, half of Manifold's documented
    /// 500/min per IP. `refresh` ignores the cache; `offline` never touches the network.
    pub fn new(
        base: &str,
        cache_dir: &Path,
        min_interval: Duration,
        refresh: bool,
        offline: bool,
    ) -> Result<Self> {
        fs::create_dir_all(cache_dir)
            .with_context(|| format!("creating {}", cache_dir.display()))?;
        let agent = ureq::AgentBuilder::new()
            .timeout(Duration::from_secs(60))
            .user_agent(concat!(
                "pm-fetch/",
                env!("CARGO_PKG_VERSION"),
                " (read-only student research; github.com/goldentree831120-ui/prediction-market)"
            ))
            .build();
        Ok(Self {
            base: base.trim_end_matches('/').to_string(),
            agent,
            min_interval,
            last_request: None,
            cache_dir: cache_dir.to_path_buf(),
            refresh,
            offline,
            requests: 0,
            max_retries: 5,
        })
    }

    fn cache_path(&self, url: &str) -> PathBuf {
        let digest = Sha256::digest(url.as_bytes());
        self.cache_dir
            .join(format!("{}.json", hex::encode(&digest[..16])))
    }

    fn throttle(&mut self) {
        if let Some(t) = self.last_request {
            let elapsed = t.elapsed();
            if elapsed < self.min_interval {
                sleep(self.min_interval - elapsed);
            }
        }
        self.last_request = Some(Instant::now());
    }
}

impl Source for HttpSource {
    fn get(&mut self, path_and_query: &str) -> Result<String> {
        let url = format!("{}{}", self.base, path_and_query);
        let cache = self.cache_path(&url);
        if !self.refresh && cache.exists() {
            let entry: CacheEntry = serde_json::from_str(&fs::read_to_string(&cache)?)
                .with_context(|| format!("corrupt cache file {}", cache.display()))?;
            return Ok(entry.body);
        }
        if self.offline {
            bail!("offline mode and no cached response for {url}");
        }

        let mut attempt = 0;
        loop {
            self.throttle();
            self.requests += 1;
            match self.agent.get(&url).call() {
                Ok(resp) => {
                    let body = resp.into_string().context("reading response body")?;
                    let entry = CacheEntry {
                        url: url.clone(),
                        fetched_at_unix: now_unix(),
                        body,
                    };
                    // Write to a temp file and rename, so an interrupted run never leaves half a file.
                    let tmp = cache.with_extension("tmp");
                    fs::write(&tmp, serde_json::to_vec(&entry)?)?;
                    fs::rename(&tmp, &cache)?;
                    return Ok(entry.body);
                }
                Err(ureq::Error::Status(code, resp))
                    if (code == 429 || code >= 500) && attempt < self.max_retries =>
                {
                    let wait = resp
                        .header("Retry-After")
                        .and_then(|v| v.parse::<u64>().ok())
                        .map(Duration::from_secs)
                        .unwrap_or_else(|| Duration::from_secs(2u64.pow(attempt + 1)));
                    eprintln!("HTTP {code} for {url}; retrying in {}s", wait.as_secs());
                    sleep(wait);
                    attempt += 1;
                }
                Err(ureq::Error::Status(code, _)) => bail!("HTTP {code} for {url}"),
                Err(e) if attempt < self.max_retries => {
                    eprintln!("network error for {url}: {e}; retrying");
                    sleep(Duration::from_secs(2u64.pow(attempt + 1)));
                    attempt += 1;
                }
                Err(e) => return Err(anyhow!("request failed for {url}: {e}")),
            }
        }
    }

    fn network_requests(&self) -> u64 {
        self.requests
    }
}

/// Recorded responses for tests: `path_and_query` -> file name via a simple index file
/// (`index.json`: {"<path_and_query>": "<file>"}). Unknown requests fail loudly.
pub struct FixtureSource {
    dir: PathBuf,
    index: std::collections::HashMap<String, String>,
    pub requested: Vec<String>,
}

impl FixtureSource {
    pub fn new(dir: &Path) -> Result<Self> {
        let index = serde_json::from_str(&fs::read_to_string(dir.join("index.json"))?)?;
        Ok(Self {
            dir: dir.to_path_buf(),
            index,
            requested: Vec::new(),
        })
    }
}

impl Source for FixtureSource {
    fn get(&mut self, path_and_query: &str) -> Result<String> {
        self.requested.push(path_and_query.to_string());
        let file = self
            .index
            .get(path_and_query)
            .ok_or_else(|| anyhow!("no fixture for {path_and_query}"))?;
        Ok(fs::read_to_string(self.dir.join(file))?)
    }

    fn network_requests(&self) -> u64 {
        0
    }
}

pub fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}
