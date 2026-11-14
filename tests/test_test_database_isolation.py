import os
import subprocess
import sys
from pathlib import Path

from backend import app


def test_application_ledgers_use_test_data_root():
    test_root = Path(os.environ["BTC_LAB_DATA_DIR"]).resolve()
    protected = (Path(__file__).resolve().parents[1] / "data").resolve()
    paths = [
        Path(app.ledger.path),
        Path(app.candle_archive.path),
        Path(app.strategy_pool.path),
        Path(app.g_mirror.path),
    ]
    for path in paths:
        resolved = path.resolve()
        assert resolved == test_root or test_root in resolved.parents
        assert resolved != protected and protected not in resolved.parents


def test_test_mode_refuses_missing_isolated_root():
    env = os.environ.copy()
    env.pop("BTC_LAB_DATA_DIR", None)
    env["BTC_LAB_TEST_MODE"] = "true"
    result = subprocess.run(
        [sys.executable, "-c", "import backend.app"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "TEST_DATA_ROOT_REQUIRED" in result.stderr

