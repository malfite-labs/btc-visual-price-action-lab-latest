import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from backend.book_pa_research import (
    knowledge_data,
    latest_research,
    prepare_research,
    run_research,
    validate_analysis,
)
from backend.market import Candle


def test_negative_test_identifiers_are_valid_knowledge_references():
    _, _, ids = knowledge_data()
    assert "NEG-01" in ids
    assert "KB-S02" in ids
    assert "KB-S03" in ids


def test_valid_structure_ids_do_not_permit_a_future_candle():
    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    bars = candles(close)
    _, _, ids = knowledge_data()
    raw = {
        "market_structure": "未确认",
        "trend_strength": "不确定",
        "explanation": "仍需核验",
        "knowledge_ids": ["KB-S02", "KB-S03"],
        "zones": [],
        "candidates": [],
        "patterns": [
            {
                "knowledge_id": "KB-S02",
                "description": "候选",
                "confirmation": "待确认",
                "invalidation": "失效",
                "evidence_candle_ids": [close.isoformat()],
            }
        ],
    }
    with pytest.raises(ValueError, match="PATTERN_EVIDENCE_INVALID"):
        validate_analysis(raw, bars, ids)


def candles(close):
    start = close - timedelta(minutes=15 * 288)
    return [
        Candle(
            "BTC/USD",
            "15Min",
            (start + timedelta(minutes=15 * i)).isoformat(),
            (start + timedelta(minutes=15 * (i + 1))).isoformat(),
            "100",
            "102",
            "99",
            "101",
            "0",
            0,
            None,
            True,
        )
        for i in range(288)
    ]


def test_research_evidence_excludes_future_and_preserves_book_provenance(tmp_path):
    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    bars = candles(close)
    future = candles(close + timedelta(minutes=15))[-1]
    result = prepare_research(bars + [future], close, tmp_path / "image.png")
    assert result["last_closed_candle_at"] == close.isoformat()
    assert result["input_candle_count"] == 288
    assert result["chart_sha256"] and result["candle_sha256"] and result["knowledge_sha256"]
    assert result["trade_authorized"] is False


def test_gap_is_rejected_instead_of_fabricating_candle(tmp_path):
    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    bars = candles(close)
    bars.pop(50)
    with pytest.raises(ValueError, match="CLOSED_WINDOW_INCOMPLETE"):
        prepare_research(bars, close, tmp_path / "image.png")


def test_analysis_requires_real_book_references_and_window_coordinates():
    bar = candles(datetime(2026, 10, 29, 12, tzinfo=UTC))[-1]
    raw = {
        "market_structure": "区间",
        "trend_strength": "不确定",
        "patterns": [],
        "zones": [
            {
                "kind": "support",
                "price": "99",
                "candle_open_at": bar.open_at,
                "knowledge_ids": ["KB-K01"],
            }
        ],
        "candidates": [],
        "explanation": "待确认",
        "knowledge_ids": ["KB-K01"],
    }
    valid = validate_analysis(raw, [bar], {"KB-K01"})
    assert valid["trade_authorized"] is False and valid["zones"][0]["price"] == "99"
    raw["knowledge_ids"] = ["NOT-A-BOOK-RULE"]
    with pytest.raises(ValueError, match="UNKNOWN_KNOWLEDGE_REFERENCE"):
        validate_analysis(raw, [bar], {"KB-K01"})


def test_fake_model_research_is_separate_and_cites_source(tmp_path):
    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    image = tmp_path / "image.png"
    evidence = prepare_research(candles(close), close, image)

    class FakeResearch:
        async def analyze(self, *, image, instruction):
            assert image.startswith(b"\x89PNG")
            assert "BTC 视觉价格行为知识库" in instruction
            ohlc = json.loads(
                instruction.split("与图像同步的已收盘K线（只用于精确价格和引用）：")[-1]
            )
            assert len(ohlc) == 288
            assert set(ohlc[0]) == {"open_at", "close_at", "open", "high", "low", "close"}
            return {
                "market_structure": "未确定",
                "trend_strength": "不确定",
                "patterns": [],
                "zones": [],
                "candidates": [],
                "explanation": "测试模型，不代表真实分析",
                "knowledge_ids": ["KB-K01"],
            }

    result = asyncio.run(
        run_research(FakeResearch(), evidence, image, tmp_path / "research.sqlite")
    )
    assert result["trade_authorized"] is False
    assert result["knowledge_references"]["pattern_and_term_entries"][0]["source_references"]
    assert (tmp_path / "research.sqlite").is_file()
    assert latest_research(tmp_path / "research.sqlite")["status"] == "READY"


def test_missing_research_database_stays_read_only(tmp_path):
    path = tmp_path / "not-created.sqlite"
    assert latest_research(path)["status"] == "NO_RESEARCH_RESULTS"
    assert not path.exists()


def test_research_refuses_mutated_candle_evidence_before_model_call(tmp_path):
    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    image = tmp_path / "image.png"
    evidence = prepare_research(candles(close), close, image)
    evidence["candles"][-1]["close"] = "100"

    class MustNotCall:
        async def analyze(self, **kwargs):
            raise AssertionError("model must not be called")

    with pytest.raises(ValueError, match="CANDLE_EVIDENCE_MISMATCH"):
        asyncio.run(run_research(MustNotCall(), evidence, image, tmp_path / "research.sqlite"))


def test_cannot_write_research_into_project_formal_data_directory(tmp_path):
    from backend.book_pa_research import ROOT

    with pytest.raises(ValueError, match="RESEARCH_DATABASE_MUST_BE_ISOLATED"):
        asyncio.run(run_research(None, {}, tmp_path / "none", ROOT / "data" / "paper.sqlite3"))


def test_test_model_cannot_write_official_research_results(tmp_path):
    from backend.book_pa_research import KNOWLEDGE

    with pytest.raises(ValueError, match="TEST_ADAPTER_CANNOT_WRITE_RESEARCH_RESULTS"):
        asyncio.run(
            run_research(None, {}, tmp_path / "none", KNOWLEDGE / "research_results.sqlite3")
        )


def test_prompt_explicitly_distinguishes_open_and_close_and_limits_references(tmp_path):
    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    image = tmp_path / "chart.png"
    evidence = prepare_research(candles(close), close, image)

    class Inspect:
        async def analyze(self, *, image, instruction):
            assert "open_at is the candle OPEN time" in instruction
            assert "Last permitted open_at: " + evidence["candles"][-1]["open_at"] in instruction
            assert "Never cite a candle opening at the cutoff" in instruction
            return {
                "market_structure": "unknown",
                "trend_strength": "unknown",
                "explanation": "uncertain",
                "knowledge_ids": ["KB-K01"],
                "patterns": [],
                "zones": [],
                "candidates": [],
            }

    asyncio.run(run_research(Inspect(), evidence, image, tmp_path / "research.sqlite"))


def test_unconfirmed_current_candle_is_rejected(tmp_path):
    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="CANDLE_CONFIRMATION_PENDING"):
        prepare_research(
            candles(close), close, tmp_path / "chart.png", observed_at=close + timedelta(seconds=5)
        )


def test_model_call_rejects_future_cutoff_even_if_evidence_is_forged(tmp_path):
    close = datetime.now(UTC) + timedelta(minutes=15)
    image = tmp_path / "chart.png"
    evidence = prepare_research(
        candles(close), close, image, observed_at=close + timedelta(seconds=10)
    )

    class NeverCall:
        async def analyze(self, **kwargs):
            raise AssertionError("future evidence reached model")

    with pytest.raises(ValueError, match="CANDLE_CONFIRMATION_PENDING"):
        asyncio.run(run_research(NeverCall(), evidence, image, tmp_path / "research.sqlite"))


def test_prompt_requires_exact_ohlc_for_annotation_price(tmp_path):
    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    image = tmp_path / "chart.png"
    evidence = prepare_research(candles(close), close, image)

    class Inspect:
        async def analyze(self, *, image, instruction):
            assert "Never return a numerical price in zones or candidates" in instruction
            return {
                "market_structure": "unknown",
                "trend_strength": "unknown",
                "explanation": "uncertain",
                "knowledge_ids": ["KB-K01"],
                "patterns": [],
                "zones": [],
                "candidates": [],
            }

    asyncio.run(run_research(Inspect(), evidence, image, tmp_path / "research.sqlite"))


def test_v03_uses_exact_program_prices_and_keeps_provenance(tmp_path):
    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    image = tmp_path / "chart.png"
    evidence = prepare_research(candles(close), close, image)

    class Model:
        async def analyze(self, *, image, instruction):
            assert "price_spec" in instruction
            return {
                "market_structure": "unknown",
                "trend_strength": "unknown",
                "explanation": "uncertain",
                "knowledge_ids": ["KB-K01"],
                "patterns": [],
                "zones": [
                    {
                        "kind": "support",
                        "knowledge_ids": ["KB-K01"],
                        "price_spec": {
                            "type": "ohlc",
                            "field": "low",
                            "candle_open_at": evidence["candles"][-1]["open_at"],
                        },
                    }
                ],
                "candidates": [],
            }

    r = asyncio.run(run_research(Model(), evidence, image, tmp_path / "result.sqlite"))
    assert r["zones"][0]["price"] == "99"
    assert r["zones"][0]["price_provenance"]["formula"] == "OHLC.low"
    assert r["zones"][0]["knowledge_ids"] == ["KB-K01"]


def test_v03_rejects_model_price_and_unknown_candle(tmp_path):
    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    image = tmp_path / "chart.png"
    evidence = prepare_research(candles(close), close, image)

    class Model:
        async def analyze(self, *, image, instruction):
            return {
                "market_structure": "unknown",
                "trend_strength": "unknown",
                "explanation": "uncertain",
                "knowledge_ids": ["KB-K01"],
                "patterns": [],
                "zones": [
                    {
                        "kind": "support",
                        "price": "99",
                        "knowledge_ids": ["KB-K01"],
                        "price_spec": {
                            "type": "ohlc",
                            "field": "low",
                            "candle_open_at": close.isoformat(),
                        },
                    }
                ],
                "candidates": [],
            }

    with pytest.raises(ValueError, match="MODEL_PRICE_NOT_ALLOWED"):
        asyncio.run(run_research(Model(), evidence, image, tmp_path / "result.sqlite"))


def test_v03_boundary_and_invalid_anchor_fail_closed():
    from backend.book_pa_research import validate_analysis_v03

    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    bars = candles(close)
    raw = {
        "market_structure": "unknown",
        "trend_strength": "unknown",
        "explanation": "uncertain",
        "knowledge_ids": ["KB-K01"],
        "patterns": [],
        "zones": [
            {
                "kind": "resistance",
                "knowledge_ids": ["KB-K01"],
                "price_spec": {
                    "type": "boundary",
                    "field": "high",
                    "operation": "max",
                    "candle_open_ats": [bars[-2].open_at, bars[-1].open_at],
                },
            }
        ],
        "candidates": [],
    }
    result = validate_analysis_v03(raw, bars, {"KB-K01"}, close.isoformat())
    assert result["zones"][0]["price"] == "102"
    assert result["zones"][0]["price_provenance"]["formula"] == "max(OHLC.high)"
    raw["zones"][0]["price_spec"]["candle_open_ats"] = [close.isoformat()]
    with pytest.raises(ValueError, match="INVALID_PRICE_REFERENCE"):
        validate_analysis_v03(raw, bars, {"KB-K01"}, close.isoformat())


def test_v03_rejects_unverified_prices_in_prose():
    from backend.book_pa_research import validate_analysis_v03

    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    bars = candles(close)
    raw = {
        "market_structure": "down",
        "trend_strength": "weak",
        "explanation": "support 78231.75 USD",
        "knowledge_ids": ["KB-K01"],
        "patterns": [],
        "zones": [],
        "candidates": [],
    }
    with pytest.raises(ValueError, match="UNVERIFIED_NUMERIC_PROSE"):
        validate_analysis_v03(raw, bars, {"KB-K01"}, close.isoformat())


def test_v03_prompt_forbids_numeric_prices_in_every_prose_field(tmp_path):
    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    image = tmp_path / "chart.png"
    evidence = prepare_research(candles(close), close, image)

    class Model:
        async def analyze(self, *, image, instruction):
            assert "所有自由文本字段不得包含价格、价格区间、百分比或其他未经核验的数值" in instruction
            assert "zones和candidates的price_spec" in instruction
            return {"market_structure": "区间震荡", "trend_strength": "偏弱", "explanation": "价格接近支撑", "knowledge_ids": ["KB-K01"], "patterns": [], "zones": [], "candidates": []}

    result = asyncio.run(run_research(Model(), evidence, image, tmp_path / "result.sqlite"))
    assert result["market_structure"] == "区间震荡"


def test_v03_allows_candle_ids_and_times_but_rejects_prices():
    from backend.book_pa_research import validate_analysis_v03

    close = datetime(2026, 10, 29, 12, tzinfo=UTC)
    raw = {
        "market_structure": "K01 15分钟",
        "trend_strength": "weak",
        "explanation": "2026-10-29 12:00 UTC KB-K01",
        "knowledge_ids": ["KB-K01"],
        "patterns": [],
        "zones": [],
        "candidates": [],
    }
    validate_analysis_v03(raw, candles(close), {"KB-K01"}, close.isoformat())
    raw["explanation"] = "支撑 78231.75 USD"
    with pytest.raises(ValueError, match="UNVERIFIED_NUMERIC_PROSE"):
        validate_analysis_v03(raw, candles(close), {"KB-K01"}, close.isoformat())
