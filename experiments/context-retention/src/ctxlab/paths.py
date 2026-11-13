from pathlib import Path

EXPERIMENT_DIR = Path(__file__).resolve().parents[2]
FIXTURE_DIR = EXPERIMENT_DIR / "fixtures" / "checkout-latency-v1"
GOLD_DIR = EXPERIMENT_DIR / "gold" / "checkout-latency-v1"
RUNS_DIR = EXPERIMENT_DIR / "runs"
