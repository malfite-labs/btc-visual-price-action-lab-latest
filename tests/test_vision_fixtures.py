import hashlib
import json
from pathlib import Path

from PIL import Image

from tests.vision_fixture_factory import CASES, generate_vision_fixtures


def test_blinded_vision_fixtures_are_reproducible_combined_ohlc_images(tmp_path):
    manifest = generate_vision_fixtures(tmp_path / "vision")
    assert manifest["market_data"] is False
    assert manifest["bars_per_image"] == {"global_3d": 288, "local_1d": 96}
    assert manifest["prompt_must_not_include_ground_truth"] is True
    assert set(manifest["cases"]) == set(CASES)

    for item in manifest["cases"].values():
        image_path = tmp_path / "vision" / item["image"]
        assert item["global_count"] == 288
        assert item["local_count"] == 96
        assert len(item["price_axes"]["global"]) == 5
        assert len(item["price_axes"]["local"]) == 5
        assert hashlib.sha256(image_path.read_bytes()).hexdigest() == item["sha256"]
        with Image.open(image_path) as image:
            assert image.format == "PNG"
            assert image.size == (1280, 950)
            image.verify()

    saved = json.loads((tmp_path / "vision" / "ground_truth_manifest.json").read_text())
    assert saved["cases"]["case_04"]["global_bias"] == "falling"
    assert saved["cases"]["case_04"]["local_bias"] == "rising_reversal"


def test_legacy_and_v2_variants_preserve_identical_ohlc_and_ground_truth(tmp_path):
    legacy = generate_vision_fixtures(tmp_path / "legacy", layout_version="legacy")
    optimized = generate_vision_fixtures(tmp_path / "layout_v2", layout_version="v2")
    frozen = json.loads(
        Path("tests/fixtures/vision/ground_truth_manifest.json").read_text(encoding="utf-8")
    )

    assert legacy["cases"].keys() == optimized["cases"].keys()
    assert legacy["cases"]["case_04"]["global_bias"] == "falling"
    assert optimized["cases"]["case_04"]["global_bias"] == "falling"
    for case_id in legacy["cases"]:
        old = legacy["cases"][case_id]
        new = optimized["cases"][case_id]
        assert old["candle_sha256"] == new["candle_sha256"]
        assert old["sha256"] == frozen["cases"][case_id]["sha256"]
        assert old["global_count"] == new["global_count"] == 288
        assert old["local_count"] == new["local_count"] == 96
        assert old["sha256"] != new["sha256"]
        assert optimized["image_size"] == [1920, 1280]
