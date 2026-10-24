"""Plants one bug at a time in calibration.py and checks the tests catch it.

A calibration study is easy to get subtly wrong in ways that make the result look better
(using a price from after the snapshot, letting the baseline see the future). Each mutation
below is one of those mistakes; the tests must fail for every one.

    python analysis/mutation_check.py      # exit 1 if any mutation survives
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "calibration.py"

MUTATIONS = [
    ("a bet exactly at the snapshot time is ignored", 'side="right"', 'side="left"'),
    (
        "look-ahead: uses the price after the NEXT bet",
        "return float(after[i]) if i >= 0",
        "return float(after[min(i + 1, len(after) - 1)]) if i >= 0",
    ),
    ("base-rate baseline sees the whole sample", "base = float(train_y.mean())", "base = float(y.mean())"),
    ("trading end = the LATER of close and resolution", "np.fmin(close, res)", "np.fmax(close, res)"),
    ("horizon snapshots taken even before the market existed", "if end - h >= start else", "if True else"),
    (
        "probability 1.0 falls outside the top bin",
        "idx = np.minimum((p * bins).astype(int), bins - 1)\n    out = []",
        "idx = (p * bins).astype(int) % bins\n    out = []",
    ),
    ("Wilson interval formula wrong", "den = 1 + z * z / n", "den = 1 + z / n"),
    ("YES/NO outcome flipped", 'm["y"] = (m["resolution"] == "YES")', 'm["y"] = (m["resolution"] == "NO")'),
]


def main() -> int:
    original = SRC.read_text()
    survived = 0
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "calibration.py"
        shutil.copy(SRC, backup)
        try:
            for name, old, new in MUTATIONS:
                if original.count(old) != 1:
                    print(f"??        {name}: pattern found {original.count(old)} times")
                    survived += 1
                    continue
                SRC.write_text(original.replace(old, new))
                r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", str(HERE / "tests")], capture_output=True)
                if r.returncode == 0:
                    print(f"SURVIVED  {name}")
                    survived += 1
                else:
                    print(f"caught    {name}")
        finally:
            shutil.copy(backup, SRC)
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
