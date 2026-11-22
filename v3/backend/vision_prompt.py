"""Shared, lossless prompt contract for the multimodal decision adapter."""

from __future__ import annotations

import json
import math
import re
from typing import Any

ALLOWED_AGENTS = frozenset({"A", "B", "C"})
ALLOWED_ACCOUNT_FIELDS = frozenset(
    {
        "position",
        "entry_price",
        "stop_loss",
        "equity",
        "available_cash",
        "bid",
        "ask",
        "mid_price",
        "quote_time_utc",
        "quote_age_ms",
        "candle_close_utc",
        "image_sha256",
        "candle_sha256",
        "market_source",
        "currency",
        "data_mode",
        "open_allowed",
        "open_reason",
        "account_version",
        "stop_state",
    }
)
ALLOWED_ACTIONS = frozenset(
    {"OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT", "HOLD", "WAIT"}
)
API_KEY_SHAPE = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "action",
        "global_trend",
        "local_trend",
        "structure",
        "key_highs",
        "key_lows",
        "rationale",
        "stop_loss",
    ],
    "properties": {
        "action": {"type": "string", "enum": sorted(ALLOWED_ACTIONS)},
        "global_trend": {"type": "string"},
        "local_trend": {"type": "string"},
        "structure": {"type": "string"},
        "key_highs": {"type": "array", "items": {"type": "number"}},
        "key_lows": {"type": "array", "items": {"type": "number"}},
        "rationale": {"type": "string"},
        "stop_loss": {"type": ["number", "null"]},
    },
    "additionalProperties": False,
}


def _validated_account_state(account_state: dict[str, Any] | None) -> dict[str, Any]:
    state = account_state or {}
    unknown = set(state) - ALLOWED_ACCOUNT_FIELDS
    if unknown:
        raise ValueError("account_state contains fields outside the model context allow-list")
    for key, value in state.items():
        if value is not None and not isinstance(value, (str, int, float)):
            raise ValueError(f"account_state.{key} must be a scalar or null")
        if isinstance(value, bool) and key != "open_allowed":
            raise ValueError(f"account_state.{key} cannot be boolean")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"account_state.{key} must be finite")
    return {key: state[key] for key in sorted(state)}


def build_decision_prompt(
    *,
    agent: str,
    md_version: str,
    strategy_md: str,
    account_state: dict[str, Any] | None = None,
    agent_memory: str = "",
    prompt_variant: str = "wait_example",
) -> str:
    """Build a prompt from decision inputs only; fixture identifiers and labels are not accepted."""
    if agent not in ALLOWED_AGENTS:
        raise ValueError("unknown agent")
    if not md_version.strip() or not strategy_md.strip():
        raise ValueError("strategy version and MD are required")
    if not isinstance(agent_memory, str):
        raise ValueError("agent_memory must be text")
    if prompt_variant not in {"wait_example", "neutral"}:
        raise ValueError("unknown prompt variant")

    state_json = json.dumps(
        _validated_account_state(account_state), ensure_ascii=False, sort_keys=True
    )
    memory_section = f"\nAgent memory:\n{agent_memory.strip()}" if agent_memory.strip() else ""
    prompt = (
        "Analyze the attached BTC/USD 15m naked-candle chart with closed candles only; "
        "do not use future information or invent prices. First assess the upper GLOBAL panel "
        "independently. Then assess the lower LOCAL panel independently. Finally describe how "
        "the two structures relate, including whether the local move confirms, pauses, or reverses "
        "the global structure. Do not infer one panel's direction from the other.\n"
        f"Agent={agent}; MD version={md_version}; account={state_json}\n"
        f"Agent MD:\n{strategy_md}{memory_section}\n"
        "Return JSON only with exactly these fields: action, global_trend, local_trend, structure, "
        "key_highs, key_lows, rationale, stop_loss. action must be one of OPEN_LONG, OPEN_SHORT, "
        "CLOSE_LONG, CLOSE_SHORT, HOLD, WAIT. Trend/structure/rationale are strings; highs/lows "
        "are numeric arrays; stop_loss is a number or null and is required for a new position. "
        "Do not provide chain-of-thought or multi-step analysis. Keep structure and rationale concise; "
        "rationale must be one short sentence. "
    )
    if prompt_variant == "wait_example":
        prompt += (
            "Return one JSON object using this complete shape: "
            '{"action":"WAIT","global_trend":"unclear","local_trend":"unclear",'
            '"structure":"No confirmed setup.","key_highs":[],"key_lows":[],'
            '"rationale":"The closed candles do not confirm an entry.","stop_loss":null}'
        )
    else:
        prompt += "Return one JSON object satisfying this schema: " + json.dumps(
            DECISION_SCHEMA, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    if API_KEY_SHAPE.search(prompt):
        raise ValueError("credential-shaped material rejected from model prompt")
    return prompt


def validate_decision_json(value: str | dict[str, Any]) -> dict[str, Any]:
    """Validate the locally defined decision schema without consulting test answers."""
    decision = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decision, dict) or set(decision) != set(DECISION_SCHEMA["required"]):
        raise ValueError("decision fields do not match schema")
    if not isinstance(decision["action"], str) or decision["action"] not in ALLOWED_ACTIONS:
        raise ValueError("invalid decision action")
    for name in ("global_trend", "local_trend", "structure", "rationale"):
        if not isinstance(decision[name], str):
            raise ValueError(f"{name} must be text")
    if not decision["rationale"].strip():
        raise ValueError("rationale cannot be empty")
    for name in ("key_highs", "key_lows"):
        values = decision[name]
        if not isinstance(values, list) or any(
            isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
            for item in values
        ):
            raise ValueError(f"{name} must be a finite numeric array")
    stop_loss = decision["stop_loss"]
    if stop_loss is not None and (
        isinstance(stop_loss, bool)
        or not isinstance(stop_loss, (int, float))
        or not math.isfinite(stop_loss)
        or stop_loss <= 0
    ):
        raise ValueError("stop_loss must be a positive finite number or null")
    if decision["action"] in {"OPEN_LONG", "OPEN_SHORT"} and stop_loss is None:
        raise ValueError("new positions require a stop_loss")
    return decision
