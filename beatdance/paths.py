"""Repository-relative locations, independent of the launch directory."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ANALYSIS = PROJECT_ROOT / "data" / "examples" / "analysis_result.csv"
DEFAULT_RECORDINGS = PROJECT_ROOT / "data" / "recordings"
RESULTS_ROOT = PROJECT_ROOT / "results"


def new_result_directory(category):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    path = RESULTS_ROOT / category / f"{stamp}_{uuid4().hex[:8]}"
    path.mkdir(parents=True)
    return path
