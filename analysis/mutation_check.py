"""Plants one bug at a time in the analysis code and checks the tests catch it.

A calibration or event study is easy to get subtly wrong in ways that make the result look better
(using a price from after the snapshot, letting a baseline see the future, counting one news story
many times). Each mutation below is one of those mistakes; the tests must fail for every one.

    python analysis/mutation_check.py      # exit 1 if any mutation survives
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

MUTATIONS = [
    ("calibration.py", "a bet exactly at the snapshot time is ignored", 'side="right"', 'side="left"'),
    (
        "calibration.py",
        "look-ahead: uses the price after the NEXT bet",
        "return float(after[i]) if i >= 0",
        "return float(after[min(i + 1, len(after) - 1)]) if i >= 0",
    ),
    ("calibration.py", "base-rate baseline sees the whole sample", "base = float(train_y.mean())", "base = float(y.mean())"),
    ("calibration.py", "trading end = the LATER of close and resolution", "np.fmin(close, res)", "np.fmax(close, res)"),
    ("calibration.py", "horizon snapshots taken even before the market existed", "if end - h >= start else", "if True else"),
    (
        "calibration.py",
        "probability 1.0 falls outside the top bin",
        "idx = np.minimum((p * bins).astype(int), bins - 1)\n    out = []",
        "idx = (p * bins).astype(int) % bins\n    out = []",
    ),
    ("calibration.py", "Wilson interval formula wrong", "den = 1 + z * z / n", "den = 1 + z / n"),
    ("calibration.py", "YES/NO outcome flipped", 'm["y"] = (m["resolution"] == "YES")', 'm["y"] = (m["resolution"] == "NO")'),
    # --- M5 news study -------------------------------------------------------------------
    ("news_study.py", "spike baseline looks into the future", "np.median(counts[lo:i])", "np.median(counts[i - 2 : i + BASELINE_D - 2])"),
    ("news_study.py", "no minimum article count for a spike", "max(MIN_ARTICLES, RATIO * base)", "RATIO * base"),
    ("news_study.py", "one story counted many times (no de-duplication)", "t - kept[-1] >= WINDOW_D * STEP", "t - kept[-1] >= 0"),
    ("news_study.py", "normal days allowed right next to spikes", "d > WINDOW_D * STEP", "d > 0"),
    ("news_study.py", "permutation ties counted in favour of the news", "np.sum(draws >= obs)", "np.sum(draws > obs)"),
    ("news_study.py", "multiple-testing threshold wrong (Bonferroni, not BH)", "q * np.arange(1, m + 1) / m", "q / m"),
    ("news_study.py", "price look-ahead (a bet exactly at t ignored)", 'side="right"', 'side="left"'),
    ("news_study.py", "lead window actually looks after the news", "np.abs(p(t) - p(t - WINDOW_D * STEP))", "np.abs(p(t + WINDOW_D * STEP) - p(t))"),
    ("news_fetch.py", "days with no articles dropped instead of counted as zero", "v, n = have.get(t, (0, 0))\n            rows.append((p.id, t, v, n))", "if t in have:\n                rows.append((p.id, t, *have[t]))"),
    ("news_fetch.py", "the day trading ended is not fetched", "hi = end_ms - end_ms % DAY_MS + DAY_MS", "hi = end_ms - end_ms % DAY_MS"),
    ("news_fetch.py", "GDELT error messages cached as data", "parse_timeline(text)  # raises on an error message, which is then NOT cached", "path.write_text(text)"),
]


def main() -> int:
    survived = 0
    with tempfile.TemporaryDirectory() as tmp:
        files = sorted({f for f, *_ in MUTATIONS})
        originals = {f: (HERE / f).read_text() for f in files}
        for f in files:
            shutil.copy(HERE / f, Path(tmp) / f)
        try:
            for f, name, old, new in MUTATIONS:
                src, original = HERE / f, originals[f]
                if original.count(old) != 1:
                    print(f"??        {name}: pattern found {original.count(old)} times")
                    survived += 1
                    continue
                src.write_text(original.replace(old, new))
                r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", str(HERE / "tests")], capture_output=True)
                src.write_text(original)
                if r.returncode == 0:
                    print(f"SURVIVED  {name}")
                    survived += 1
                else:
                    print(f"caught    {name}")
        finally:
            for f in files:
                shutil.copy(Path(tmp) / f, HERE / f)
    print(f"{len(MUTATIONS) - survived}/{len(MUTATIONS)} caught")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
