"""Book PA research only: isolated evidence, provenance and optional model adapter.

No trading, broker, account, scheduler, shadow or production budget dependencies.
Calling a model is an explicit operation supplied by the caller; nothing runs on import.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from backend.charts import render_ai_snapshot
from backend.market import Candle, candle_hash

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "knowledge" / "book_pa_v1"
MODULE = "BOOK-PA-V0.1"


def knowledge_files(root: Path = KNOWLEDGE) -> tuple[Path, Path]:
    markdown = list(root.glob("*.md"))
    indexes = list(root.glob("*.json"))
    if len(markdown) != 1 or len(indexes) != 1:
        raise ValueError("KNOWLEDGE_BASE_MISSING_OR_AMBIGUOUS")
    return markdown[0], indexes[0]


def knowledge_data(root: Path = KNOWLEDGE) -> tuple[str, dict, set[str]]:
    md, index = knowledge_files(root)
    text = md.read_text(encoding="utf-8-sig")
    data = json.loads(index.read_text(encoding="utf-8-sig"))
    if data.get("knowledge_base_id") != "BTC_VISUAL_PA_BOOK_KB" or data.get("version") != "1.0.0":
        raise ValueError("KNOWLEDGE_VERSION_INVALID")
    ids = {
        str(item[key])
        for group in (
            "core_principles",
            "pattern_and_term_entries",
            "strategy_cards",
            "market_states",
            "negative_tests",
            "structure_and_levels",
        )
        for item in data.get(group, [])
        if isinstance(item, dict)
        for key in ("id", "code")
        if item.get(key)
    }
    if not text or not ids:
        raise ValueError("KNOWLEDGE_CONTENT_INVALID")
    return text, data, ids


def _time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return moment.astimezone(UTC)


def prepare_research(
    candles: list[Candle], as_of: datetime, image_path: Path, *, observed_at: datetime | None = None
) -> dict:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("TIMEZONE_REQUIRED")
    cutoff = as_of.astimezone(UTC)
    observed = observed_at if observed_at is not None else datetime.now(UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("TIMEZONE_REQUIRED")
    # Research ingestion delay, not a book rule or a provider finality guarantee.
    if observed.astimezone(UTC) < cutoff + timedelta(seconds=10):
        raise ValueError("CANDLE_CONFIRMATION_PENDING")
    eligible = sorted(
        (c for c in candles if c.is_closed and _time(c.close_at) <= cutoff),
        key=lambda c: _time(c.close_at),
    )
    window = eligible[-288:]
    if len(window) != 288 or _time(window[-1].close_at) != cutoff:
        raise ValueError("CLOSED_WINDOW_INCOMPLETE")
    for i, c in enumerate(window):
        if (
            c.symbol != "BTC/USD"
            or c.source != "ALPACA_US_BTCUSD_SPOT"
            or c.currency != "USD_RESEARCH"
            or c.interval.lower() not in {"15min", "15m"}
        ):
            raise ValueError("CANDLE_SOURCE_INVALID")
        if _time(c.close_at) - _time(c.open_at) != timedelta(minutes=15) or (
            i and _time(c.open_at) != _time(window[i - 1].close_at)
        ):
            raise ValueError("CANDLE_GAP_OR_DUPLICATE")
        try:
            op, hi, lo, cl = (Decimal(x) for x in (c.open, c.high, c.low, c.close))
            if (
                not all(x.is_finite() and x > 0 for x in (op, hi, lo, cl))
                or lo > min(op, cl)
                or hi < max(op, cl)
                or lo > hi
            ):
                raise ValueError("OHLC_INVALID")
        except InvalidOperation as exc:
            raise ValueError("OHLC_INVALID") from exc
    md, index = knowledge_files()
    knowledge_data()
    rendered = render_ai_snapshot(window, image_path, layout_version="v2")
    return {
        "module": MODULE,
        "symbol": "BTC/USD",
        "market": "ALPACA_US_BTCUSD_SPOT",
        "currency": "USD_RESEARCH",
        "last_closed_candle_at": cutoff.isoformat(),
        "input_candle_count": len(window),
        "chart_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
        "candle_sha256": rendered["candle_sha256"],
        "knowledge_sha256": hashlib.sha256(md.read_bytes()).hexdigest(),
        "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
        "knowledge_version": "1.0.0",
        "trade_authorized": False,
        "candles": [asdict(c) for c in window],
    }


def validate_analysis(raw: dict, bars: list[Candle], ids: set[str]) -> dict:
    if not isinstance(raw, dict) or not all(
        isinstance(raw.get(k), str) and raw[k].strip()
        for k in ("market_structure", "trend_strength", "explanation")
    ):
        raise ValueError("ANALYSIS_INVALID")
    references = raw.get("knowledge_ids")
    if not isinstance(references, list) or not references or any(x not in ids for x in references):
        raise ValueError("UNKNOWN_KNOWLEDGE_REFERENCE")
    by_open = {_time(c.open_at): c for c in bars}
    zones, candidates = raw.get("zones"), raw.get("candidates")
    patterns = raw.get("patterns")
    if not all(isinstance(x, list) for x in (zones, candidates, patterns)):
        raise ValueError("ANALYSIS_INVALID")
    for pattern in patterns:
        if (
            not isinstance(pattern, dict)
            or pattern.get("knowledge_id") not in ids
            or not all(
                isinstance(pattern.get(k), str) and pattern[k].strip()
                for k in ("description", "confirmation", "invalidation")
            )
            or not isinstance(pattern.get("evidence_candle_ids"), list)
            or not pattern["evidence_candle_ids"]
        ):
            raise ValueError("PATTERN_EVIDENCE_INVALID")
        try:
            if any(_time(x) not in by_open for x in pattern["evidence_candle_ids"]):
                raise ValueError("PATTERN_EVIDENCE_INVALID")
        except (TypeError, ValueError) as exc:
            raise ValueError("PATTERN_EVIDENCE_INVALID") from exc
    for row in [*zones, *candidates]:
        if not isinstance(row, dict) or row.get("kind") not in {
            "support",
            "resistance",
            "candidate_long",
            "candidate_short",
            "invalidation",
        }:
            raise ValueError("ANNOTATION_INVALID")
        if (
            not isinstance(row.get("knowledge_ids"), list)
            or not row["knowledge_ids"]
            or any(x not in ids for x in row["knowledge_ids"])
        ):
            raise ValueError("UNKNOWN_KNOWLEDGE_REFERENCE")
        try:
            bar = by_open[_time(row["candle_open_at"])]
            price = Decimal(str(row["price"]))
            if not price.is_finite() or price not in {
                Decimal(bar.open),
                Decimal(bar.high),
                Decimal(bar.low),
                Decimal(bar.close),
            }:
                raise ValueError("ANNOTATION_PRICE_NOT_OHLC")
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise ValueError("ANNOTATION_INVALID") from exc
    return {
        "module": MODULE,
        "market_structure": raw["market_structure"],
        "trend_strength": raw["trend_strength"],
        "explanation": raw["explanation"],
        "patterns": patterns,
        "zones": zones,
        "candidates": candidates,
        "knowledge_ids": references,
        "trade_authorized": False,
        "status": "RESEARCH_ONLY_UNVALIDATED",
    }


def validate_analysis_v03(raw: dict, bars: list[Candle], ids: set[str], cutoff: str) -> dict:
    """Resolve explicit model-selected anchors; never repair historical model prices."""
    from backend.book_pa_prices_v02 import resolve_price

    if not isinstance(raw, dict):
        raise ValueError("ANALYSIS_INVALID")
    prose = [raw.get(key) for key in ("market_structure", "trend_strength", "explanation")]
    if isinstance(raw.get("patterns"), list):
        for pattern in raw["patterns"]:
            if isinstance(pattern, dict):
                prose.extend(
                    pattern.get(key) for key in ("description", "confirmation", "invalidation")
                )

    # Strip recognized references, timestamps, and interval descriptions first;
    # all remaining numeric prose is unverified and rejected, not reinterpreted.
    def has_unverified_number(value):
        if not isinstance(value, str):
            return False
        value = re.sub(
            r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?", "", value
        )
        value = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "", value)
        value = re.sub(r"\b(?:KB-[A-Z]+-?\d+|STR-\d+|NEG-\d+|K\d{2})\b", "", value, flags=re.I)
        value = re.sub(
            r"\b\d{1,3}\s*(?:分钟|小时|天|minutes?|hours?|days?)\b", "", value, flags=re.I
        )
        return any(ch.isdecimal() for ch in value)

    if any(has_unverified_number(value) for value in prose):
        raise ValueError("UNVERIFIED_NUMERIC_PROSE")
    transformed = dict(raw)
    by_open = {_time(bar.open_at): bar for bar in bars}
    source = [asdict(bar) for bar in bars]
    for group in ("zones", "candidates"):
        rows = raw.get(group)
        if not isinstance(rows, list):
            raise ValueError("ANALYSIS_INVALID")
        converted = []
        for row in rows:
            if not isinstance(row, dict) or "price" in row or "candle_open_at" in row:
                raise ValueError("MODEL_PRICE_NOT_ALLOWED")
            refs = row.get("knowledge_ids")
            if not isinstance(refs, list) or not refs or any(ref not in ids for ref in refs):
                raise ValueError("UNKNOWN_KNOWLEDGE_REFERENCE")
            spec = row.get("price_spec")
            if not isinstance(spec, dict):
                raise ValueError("PRICE_SPEC_REQUIRED")
            resolved = resolve_price(spec, source, cutoff=cutoff)
            field = spec["field"]
            anchor = next(
                open_at
                for open_at in resolved["candle_open_ats"]
                if Decimal(getattr(by_open[_time(open_at)], field)) == Decimal(resolved["price"])
            )
            converted.append(
                {
                    **row,
                    "price": resolved["price"],
                    "candle_open_at": anchor,
                    "price_provenance": {**resolved, "knowledge_ids": refs},
                }
            )
        transformed[group] = converted
    result = validate_analysis(transformed, bars, ids)
    result["price_contract"] = "BOOK_PA_PRICE_V03"
    return result


class ResearchModel(Protocol):
    async def analyze(self, *, image: bytes, instruction: str) -> dict: ...


class ResearchVisionModel:
    """Explicit opt-in adapter using its own configured budget database."""

    def __init__(self, config, *, image_capability_verified: bool = False, transport=None):
        from backend.vision_provider import ConfigurableVisionAdapter

        budget = Path(config.budget_db_path)
        if (
            budget.name != "book_pa_model_budget.sqlite3"
            or budget.resolve().parent != KNOWLEDGE.resolve()
        ):
            raise ValueError("RESEARCH_BUDGET_MUST_BE_ISOLATED")
        self.adapter = ConfigurableVisionAdapter(config, transport=transport)
        self.adapter.image_capability_verified = image_capability_verified
        self.model_id = config.model
        self.last_response = None

    async def analyze(self, *, image: bytes, instruction: str) -> dict:
        if not self.adapter.configured:
            raise ValueError("RESEARCH_MODEL_NOT_CONFIGURED")
        from uuid import uuid4

        result = await self.adapter._send_image_request(
            image, instruction, "book-pa:" + uuid4().hex
        )
        self.last_response = result
        if not isinstance(result, dict) or result.get("status"):
            raise ValueError("RESEARCH_MODEL_REQUEST_FAILED")
        return result


async def run_research(
    model: ResearchModel, evidence: dict, image_path: Path, db_path: Path
) -> dict:
    resolved = db_path.resolve()
    if resolved == ROOT.resolve() or (
        ROOT.resolve() in resolved.parents and KNOWLEDGE.resolve() not in resolved.parents
    ):
        raise ValueError("RESEARCH_DATABASE_MUST_BE_ISOLATED")
    if KNOWLEDGE.resolve() in resolved.parents and (
        not isinstance(model, ResearchVisionModel) or model.adapter.test_only
    ):
        raise ValueError("TEST_ADAPTER_CANNOT_WRITE_RESEARCH_RESULTS")
    image = image_path.read_bytes()
    if hashlib.sha256(image).hexdigest() != evidence["chart_sha256"]:
        raise ValueError("IMAGE_EVIDENCE_MISMATCH")
    if datetime.now(UTC) < _time(evidence["last_closed_candle_at"]) + timedelta(seconds=10):
        raise ValueError("CANDLE_CONFIRMATION_PENDING")
    bars = [Candle(**x) for x in evidence["candles"]]
    if (
        len(bars) != 288
        or candle_hash(bars) != evidence["candle_sha256"]
        or any(
            not bar.is_closed or _time(bar.close_at) > _time(evidence["last_closed_candle_at"])
            for bar in bars
        )
    ):
        raise ValueError("CANDLE_EVIDENCE_MISMATCH")
    md_path, index_path = knowledge_files()
    if (
        hashlib.sha256(md_path.read_bytes()).hexdigest() != evidence["knowledge_sha256"]
        or hashlib.sha256(index_path.read_bytes()).hexdigest() != evidence["index_sha256"]
    ):
        raise ValueError("KNOWLEDGE_EVIDENCE_MISMATCH")
    md, index, ids = knowledge_data()
    instruction = (
        "open_at is the candle OPEN time; close_at is its CLOSE time (UTC). "
        "Cite only exact open_at values supplied in the candle list. "
        "Never cite a candle opening at the cutoff; it is not closed. "
        "Never invent prices in free text; use structured price references. Time, candle identifiers and knowledge identifiers are allowed in text. Never return a numerical price in zones or candidates. Choose explicit candle IDs and OHLC field in price_spec; the program calculates exact prices. "
        "Last permitted open_at: "
        + evidence["candles"][-1]["open_at"]
        + "\n"
        + "独立研究，不下单、不操作账户；只分析已收盘K线，给出结构、强度、形态、突破、失效、"
        "候选位置并引用知识条目。未知则明确不确定。标注价格必须引用对应K线的真实OHLC。\n"
        "输出约束优先于知识库中的数值示例：所有自由文本字段不得包含价格、价格区间、百分比或其他未经核验的数值。"
        "这包括market_structure、trend_strength、explanation及每个pattern的description、confirmation、invalidation；"
        "不要写具体价位、整数、千位分隔符或涨跌幅，用‘区间上沿’、‘近期低点’等定性词表达。"
        "如需表达具体价格，只能通过zones和candidates的price_spec引用输入K线，由程序计算；不能在文本中复述计算结果。"
        "引用时间须使用输入中完整的ISO开盘时间，知识编号使用索引原文。无法引用时写不确定，不要猜测。\n"
        "返回JSON：market_structure、trend_strength、explanation为字符串；knowledge_ids为非空索引ID列表；"
        "patterns为对象列表，每项包括knowledge_id、description、confirmation、invalidation、evidence_candle_ids；"
        "zones与candidates为对象列表，每项包括kind、knowledge_ids、price_spec；禁止输出price。"
        "price_spec为{type:ohlc,field:open/high/low/close,candle_open_at:开盘时间}，或"
        "{type:boundary,field:high/low,operation:min/max,candle_open_ats:[开盘时间]}；不确定时不要给出价位。"
        "kind仅能为support、resistance、candidate_long、candidate_short、invalidation。无依据时返回空列表。\n"
        + md
        + "\n结构化规则索引："
        + json.dumps(index, ensure_ascii=False)
        + "\n与图像同步的已收盘K线（只用于精确价格和引用）："
        + json.dumps(
            [
                {k: row[k] for k in ("open_at", "close_at", "open", "high", "low", "close")}
                for row in evidence["candles"]
            ],
            ensure_ascii=False,
        )
    )
    raw = await model.analyze(image=image, instruction=instruction)
    result = validate_analysis_v03(raw, bars, ids, evidence["last_closed_candle_at"])
    result["model_id"] = getattr(model, "model_id", "UNSPECIFIED_RESEARCH_ADAPTER")
    public_evidence = {k: v for k, v in evidence.items() if k != "candles"}
    result["evidence"] = public_evidence
    referenced = (
        set(result["knowledge_ids"])
        | {p["knowledge_id"] for p in result["patterns"]}
        | {ref for row in result["zones"] + result["candidates"] for ref in row["knowledge_ids"]}
    )
    result["knowledge_references"] = {
        group: [
            item for item in index.get(group, []) if item.get("id", item.get("code")) in referenced
        ]
        for group in (
            "core_principles",
            "pattern_and_term_entries",
            "strategy_cards",
            "market_states",
            "negative_tests",
            "structure_and_levels",
        )
    }
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS research_results (id INTEGER PRIMARY KEY, candle_close TEXT NOT NULL, result_json TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO research_results(candle_close,result_json) VALUES (?,?)",
            (evidence["last_closed_candle_at"], json.dumps(result, ensure_ascii=False)),
        )
    return result


def latest_research(db_path: Path) -> dict:
    if not db_path.is_file():
        return {"status": "NO_RESEARCH_RESULTS", "item": None}
    try:
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT result_json FROM research_results ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return (
            {"status": "READY", "item": json.loads(row[0])}
            if row
            else {"status": "NO_RESEARCH_RESULTS", "item": None}
        )
    except (sqlite3.Error, ValueError, TypeError):
        return {"status": "RESEARCH_DATA_UNAVAILABLE", "item": None}
