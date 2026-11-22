"""Deterministic synthetic OHLC charts for future blinded visual-model evaluation."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from backend.charts import render_ai_snapshot
from backend.market import Candle

CASES = {
    "case_01": {
        "pattern": "trend_up",
        "global_bias": "rising",
        "local_bias": "rising",
        "assessment": "trend_structure",
    },
    "case_02": {
        "pattern": "trend_down",
        "global_bias": "falling",
        "local_bias": "falling",
        "assessment": "trend_structure",
    },
    "case_03": {
        "pattern": "range",
        "global_bias": "sideways",
        "local_bias": "sideways",
        "assessment": "range_or_wait",
    },
    "case_04": {
        "pattern": "downtrend_then_reversal",
        "global_bias": "falling",
        "local_bias": "rising_reversal",
        "assessment": "reversal_structure",
    },
}


def _close(pattern: str, index: int) -> int:
    if pattern == "trend_up":
        return 48_000 + index * 12 + ((index % 12) - 6) * 8
    if pattern == "trend_down":
        return 62_000 - index * 12 - ((index % 12) - 6) * 8
    if pattern == "range":
        return 52_000 + int(500 * math.sin(index * 0.14))
    if pattern == "downtrend_then_reversal":
        return 62_000 - index * 18 if index < 288 else 56_834 + (index - 287) * 42
    raise ValueError("unknown test pattern")


def candles_for(pattern: str) -> list[Candle]:
    candles = []
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(384):
        close = _close(pattern, index)
        open_ = (
            _close(pattern, index - 1) if index else close - (12 if pattern == "trend_up" else -12)
        )
        high = max(open_, close) + 70 + (index % 5) * 12
        low = min(open_, close) - 70 - (index % 7) * 10
        opened_at = start + timedelta(minutes=15 * index)
        candles.append(
            Candle(
                symbol="BTC/USD",
                interval="15m",
                open_at=opened_at.isoformat().replace("+00:00", "Z"),
                close_at=(opened_at + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
                open=str(open_),
                high=str(high),
                low=str(low),
                close=str(close),
                volume="0",
                trade_count=None,
                vwap=None,
                is_closed=True,
                source="SYNTHETIC_TEST_FIXTURE",
                currency="USD_RESEARCH",
                data_basis="Deterministic synthetic OHLC; not market data",
            )
        )
    return candles


def clean_reversal_candles() -> list[Candle]:
    """Deterministic 3-day decline with a structurally confirmed final-day rise."""
    candles: list[Candle] = []
    start = datetime(2026, 2, 1, tzinfo=UTC)
    for index in range(384):
        if index <= 287:
            close = 62_000 - index * 30
        else:
            reversal_anchor = 62_000 - 287 * 30
            close = reversal_anchor + (index - 287) * 28
        previous_close = (
            62_000 - (index - 1) * 30
            if index == 288
            else (candles[-1].close if candles else str(close - 30))
        )
        open_ = Decimal(previous_close)
        close_value = Decimal(close)
        high = max(open_, close_value) + Decimal(45 + (index % 5) * 7)
        low = min(open_, close_value) - Decimal(45 + (index % 7) * 6)
        opened_at = start + timedelta(minutes=15 * index)
        candles.append(
            Candle(
                symbol="BTC/USD",
                interval="15m",
                open_at=opened_at.isoformat().replace("+00:00", "Z"),
                close_at=(opened_at + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
                open=str(open_),
                high=str(high),
                low=str(low),
                close=str(close_value),
                volume="0",
                trade_count=None,
                vwap=None,
                is_closed=True,
                source="SYNTHETIC_TEST_FIXTURE",
                currency="USD_RESEARCH",
                data_basis="Deterministic synthetic OHLC; not market data",
            )
        )
    return candles


def generate_adjudicated_fixtures(destination: Path) -> dict:
    """Write a separate, hash-frozen fixture set; never touches historical fixtures."""
    from tests.vision_ground_truth import score_windows

    destination.mkdir(parents=True, exist_ok=True)
    legacy_dir = destination / "legacy"
    v2_dir = destination / "layout_v2"
    legacy_dir.mkdir(exist_ok=True)
    v2_dir.mkdir(exist_ok=True)
    source_cases = {
        "case_01": candles_for("trend_up"),
        "case_02": candles_for("trend_down"),
        "case_03": candles_for("range"),
        "case_05": clean_reversal_candles(),
    }
    cases: dict[str, dict] = {}
    for case_id, candles in source_cases.items():
        serialized = [asdict(item) for item in candles]
        canonical = json.dumps(serialized, sort_keys=True, separators=(",", ":")).encode()
        candle_digest = hashlib.sha256(canonical).hexdigest()
        ohlc_name = f"{case_id}_ohlc.json"
        (destination / ohlc_name).write_text(
            json.dumps(serialized, indent=2) + "\n", encoding="utf-8"
        )
        score = score_windows(candles)
        image_records = {}
        for variant, folder, layout in (
            ("legacy", legacy_dir, "legacy"),
            ("v2", v2_dir, "v2"),
        ):
            image_path = folder / f"{case_id}.png"
            evidence = render_ai_snapshot(candles, image_path, layout_version=layout)
            image_records[variant] = {
                "path": f"{folder.name}/{image_path.name}",
                "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                "candle_sha256": evidence["candle_sha256"],
                "size": evidence["image_size"],
            }
        cases[case_id] = {
            "ohlc_file": ohlc_name,
            "ohlc_sha256": candle_digest,
            "chart_candle_sha256": image_records["legacy"]["candle_sha256"],
            "global_count": 288,
            "local_count": 96,
            "score_key": {
                "global_bias": score["global"]["structure_direction"],
                "local_bias": score["local"]["structure_direction"],
                "local_event": score["local"]["event"],
                "assessment": score["assessment"],
            },
            "metrics": score,
            "images": image_records,
            "synthetic": True,
        }
    old_manifest = json.loads(
        (Path(__file__).parent / "fixtures/vision/ground_truth_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = {
        "dataset_version": "adjudicated-v1",
        "fixture_kind": "synthetic_blinded_visual_evaluation",
        "market_data": False,
        "prompt_must_not_include_ground_truth": True,
        "scoring_rules": "tests/vision_ground_truth_rules_v1.md",
        "bars_per_image": {"global_3d": 288, "local_1d": 96},
        "excluded_cases": {
            "case_04": {
                "scoring_status": "excluded_reclassified",
                "historical_label": old_manifest["cases"]["case_04"],
                "reason": "288-bar endpoint is positive; objective global structure is mixed, so the historical falling label is not retained for scoring.",
            }
        },
        "cases": cases,
    }
    (destination / "ground_truth_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    checksum_lines = []
    for path in sorted(item for item in destination.rglob("*") if item.is_file()):
        if path.name == "SHA256SUMS.txt":
            continue
        checksum_lines.append(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(destination).as_posix()}"
        )
    (destination / "SHA256SUMS.txt").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return manifest


def generate_vision_fixtures(destination: Path, *, layout_version: str = "legacy") -> dict:
    if layout_version not in {"legacy", "v2"}:
        raise ValueError("unknown fixture chart layout")
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {
        "fixture_kind": "synthetic_blinded_visual_evaluation",
        "market_data": False,
        "prompt_must_not_include_ground_truth": True,
        "bars_per_image": {"global_3d": 288, "local_1d": 96},
        "layout_version": layout_version,
        "image_size": [1280, 950] if layout_version == "legacy" else [1920, 1280],
        "cases": {},
    }
    for case_id, expected in CASES.items():
        output = destination / f"{case_id}.png"
        evidence = render_ai_snapshot(
            candles_for(expected["pattern"]), output, layout_version=layout_version
        )
        manifest["cases"][case_id] = {
            **expected,
            "image": output.name,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "global_count": evidence["global_count"],
            "local_count": evidence["local_count"],
            "price_axes": evidence["price_axes"],
            "candle_sha256": evidence["candle_sha256"],
            "synthetic": True,
        }
    manifest_path = destination / "ground_truth_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
