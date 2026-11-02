from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from backend.vision_prompt import DECISION_SCHEMA, build_decision_prompt, validate_decision_json


@dataclass(frozen=True)
class VisionProviderConfig:
    enabled: bool = False
    calls_authorized: bool = False
    base_url: str = "https://api.deepseek.com"
    endpoint_path: str = "/chat/completions"
    model: str = "deepseek-flash"
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout_seconds: float = 60
    max_concurrency: int = 3
    max_output_tokens: int = 1000
    thinking_mode: str | None = None
    prompt_variant: str = "wait_example"
    max_requests_per_day: int = 0
    max_call_cost_usd: Decimal = Decimal("0")
    daily_budget_usd: Decimal = Decimal("0")
    total_budget_usd: Decimal | None = None
    estimated_call_cost_usd: Decimal = Decimal("0")
    input_price_per_million_usd: Decimal | None = None
    output_price_per_million_usd: Decimal | None = None
    budget_db_path: str = ":memory:"

    def __post_init__(self):
        if not 1 <= self.max_concurrency <= 8:
            raise ValueError("max_concurrency must be between 1 and 8")
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be in (0, 300]")
        if not 1 <= self.max_output_tokens <= 384_000:
            raise ValueError(
                "max_output_tokens must be between 1 and the documented provider maximum"
            )
        if self.thinking_mode not in {None, "enabled", "disabled"}:
            raise ValueError("thinking_mode must be enabled, disabled, or omitted")
        if self.prompt_variant not in {"wait_example", "neutral"}:
            raise ValueError("unknown prompt variant")
        if self.max_requests_per_day < 0:
            raise ValueError("max_requests_per_day cannot be negative")
        if any(
            value < 0
            for value in (
                self.max_call_cost_usd,
                self.daily_budget_usd,
                self.estimated_call_cost_usd,
            )
        ):
            raise ValueError("vision cost limits cannot be negative")
        if self.total_budget_usd is not None and (
            not self.total_budget_usd.is_finite() or self.total_budget_usd <= 0
        ):
            raise ValueError("total budget must be finite and positive")
        if any(
            value is not None and value < 0
            for value in (self.input_price_per_million_usd, self.output_price_per_million_usd)
        ):
            raise ValueError("vision token prices cannot be negative")
        if not self.endpoint_path.startswith("/"):
            raise ValueError("endpoint_path must be an absolute URL path")
        if "?" in self.endpoint_path or "#" in self.endpoint_path or ".." in self.endpoint_path:
            raise ValueError("endpoint_path cannot contain query, fragment, or traversal")
        if self.base_url:
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname:
                raise ValueError("base_url must be an absolute HTTPS URL")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("base_url cannot embed credentials or query parameters")
            if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
                raise ValueError("non-local model endpoints must use HTTPS")
        if not self.api_key_env.isidentifier():
            raise ValueError("api_key_env must be an environment-variable name")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> VisionProviderConfig:
        env = os.environ if environ is None else environ
        enabled = env.get("VISION_ENABLED", "false").lower() == "true"
        authorized = env.get("VISION_CALLS_AUTHORIZED", "false").lower() == "true"
        input_price = env.get("VISION_INPUT_PRICE_PER_MILLION_USD")
        output_price = env.get("VISION_OUTPUT_PRICE_PER_MILLION_USD")
        return cls(
            enabled=enabled,
            calls_authorized=authorized,
            base_url=env.get("VISION_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            endpoint_path=env.get("VISION_ENDPOINT_PATH", "/chat/completions"),
            model=env.get("VISION_MODEL", "deepseek-flash"),
            api_key_env=env.get("VISION_API_KEY_ENV", "DEEPSEEK_API_KEY"),
            timeout_seconds=float(env.get("VISION_TIMEOUT_SECONDS", "60")),
            max_concurrency=int(env.get("VISION_MAX_CONCURRENCY", "3")),
            max_output_tokens=int(env.get("VISION_MAX_OUTPUT_TOKENS", "1000")),
            thinking_mode=env.get("VISION_THINKING_MODE") or None,
            prompt_variant=env.get("VISION_PROMPT_VARIANT", "wait_example"),
            max_requests_per_day=int(env.get("VISION_MAX_CALLS_PER_UTC_DAY", "0")),
            max_call_cost_usd=Decimal(env.get("VISION_MAX_CALL_COST_USD", "0")),
            daily_budget_usd=Decimal(env.get("VISION_DAILY_BUDGET_USD", "0")),
            total_budget_usd=Decimal(env["VISION_TOTAL_BUDGET_USD"])
            if env.get("VISION_TOTAL_BUDGET_USD")
            else None,
            estimated_call_cost_usd=Decimal(env.get("VISION_ESTIMATED_CALL_COST_USD", "0")),
            input_price_per_million_usd=(Decimal(input_price) if input_price else None),
            output_price_per_million_usd=(Decimal(output_price) if output_price else None),
            budget_db_path=env.get(
                "VISION_BUDGET_DB",
                "data/vision-budget.sqlite3" if enabled and authorized else ":memory:",
            ),
        )


class VisionBudget:
    """Durably reserve conservative estimated maximum cost before each external request."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            from pathlib import Path

            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS vision_budget_calls (request_id TEXT PRIMARY KEY, utc_day TEXT NOT NULL, reserved_usd TEXT NOT NULL, status TEXT NOT NULL)"
        )
        if "actual_cost_usd" not in {
            row[1] for row in self._db.execute("PRAGMA table_info(vision_budget_calls)")
        }:
            self._db.execute("ALTER TABLE vision_budget_calls ADD COLUMN actual_cost_usd TEXT")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS vision_budget_scope (id INTEGER PRIMARY KEY CHECK(id=1), total_limit_usd TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS vision_provider_fault (id INTEGER PRIMARY KEY CHECK(id=1), reason TEXT NOT NULL)"
        )
        self._db.commit()
        self._lock = threading.Lock()

    def reserve(
        self,
        request_id: str,
        amount: Decimal,
        daily_limit: Decimal,
        daily_call_limit: int,
        *,
        total_limit: Decimal | None = None,
    ) -> tuple[bool, str]:
        day = datetime.now(UTC).date().isoformat()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            old = self._db.execute(
                "SELECT status FROM vision_budget_calls WHERE request_id=?", (request_id,)
            ).fetchone()
            if old:
                self._db.rollback()
                return False, "VISION_REQUEST_DUPLICATE"
            count = self._db.execute(
                "SELECT COUNT(*) FROM vision_budget_calls WHERE utc_day=?", (day,)
            ).fetchone()[0]
            if count >= daily_call_limit:
                self._db.rollback()
                return False, "VISION_CALL_COUNT_EXHAUSTED"
            # Add with exact Decimal rows instead of accepting SQLite float aggregation drift.
            total = sum(
                Decimal(row[0])
                for row in self._db.execute(
                    "SELECT reserved_usd FROM vision_budget_calls WHERE utc_day=?", (day,)
                ).fetchall()
            )
            if total + amount > daily_limit:
                self._db.rollback()
                return False, "VISION_BUDGET_EXHAUSTED"
            scope = self._db.execute(
                "SELECT total_limit_usd FROM vision_budget_scope WHERE id=1"
            ).fetchone()
            if scope is None and total_limit is not None:
                self._db.execute("INSERT INTO vision_budget_scope VALUES(1,?)", (str(total_limit),))
                scope = (str(total_limit),)
            if scope is not None:
                cap = Decimal(scope[0])
                if total_limit is not None:
                    cap = min(cap, total_limit)
                lifetime = sum(
                    (
                        Decimal(row[0])
                        for row in self._db.execute(
                            "SELECT reserved_usd FROM vision_budget_calls"
                        ).fetchall()
                    ),
                    Decimal(0),
                )
                if lifetime + amount > cap:
                    self._db.rollback()
                    return False, "VISION_TOTAL_BUDGET_EXHAUSTED"
            self._db.execute(
                "INSERT INTO vision_budget_calls(request_id,utc_day,reserved_usd,status) VALUES(?,?,?,?)",
                (request_id, day, str(amount), "RESERVED"),
            )
            self._db.commit()
        return True, "RESERVED"

    def mark(self, request_id: str, status: str, actual_cost: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE vision_budget_calls SET status=?,actual_cost_usd=COALESCE(?,actual_cost_usd) WHERE request_id=?",
                (status, actual_cost, request_id),
            )
            self._db.commit()

    def bind_total_limit(self, limit: Decimal) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT total_limit_usd FROM vision_budget_scope WHERE id=1"
            ).fetchone()
            if row is None:
                self._db.execute("INSERT INTO vision_budget_scope VALUES(1,?)", (str(limit),))
            elif limit < Decimal(row[0]):
                self._db.execute(
                    "UPDATE vision_budget_scope SET total_limit_usd=? WHERE id=1", (str(limit),)
                )
            self._db.commit()

    def summary(self) -> dict:
        with self._lock:
            rows = self._db.execute(
                "SELECT reserved_usd,actual_cost_usd FROM vision_budget_calls"
            ).fetchall()
            scope = self._db.execute(
                "SELECT total_limit_usd FROM vision_budget_scope WHERE id=1"
            ).fetchone()
        reserved = sum((Decimal(r[0]) for r in rows), Decimal(0))
        known = sum((Decimal(r[1]) for r in rows if r[1] is not None), Decimal(0))
        unknown = sum((Decimal(r[0]) for r in rows if r[1] is None), Decimal(0))
        return {
            "requests": len(rows),
            "reserved_usd": str(reserved),
            "known_usage_estimate_usd": str(known),
            "unknown_usage_reserve_usd": str(unknown),
            "total_limit_usd": scope[0] if scope else None,
            "provider_fault": self.fault(),
        }

    def latch_fault(self, reason: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO vision_provider_fault VALUES(1,?)", (reason,))
            self._db.commit()

    def fault(self) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT reason FROM vision_provider_fault WHERE id=1").fetchone()
        return row[0] if row else None


def public_vision_status(
    config: VisionProviderConfig,
    *,
    credential_present: bool,
    image_capability_verified: bool = False,
) -> dict:
    """Safe UI/API projection: expose settings and booleans, never key material or raw URL."""
    configured = bool(config.base_url and config.model and credential_present)
    if not config.enabled or not config.calls_authorized:
        state = "MODEL_CALLS_DISABLED"
    elif not configured:
        state = "MODEL_NOT_CONFIGURED"
    elif not image_capability_verified:
        state = "IMAGE_CAPABILITY_UNVERIFIED"
    else:
        state = "IMAGE_CAPABILITY_VERIFIED"
    return {
        "state": state,
        "enabled": config.enabled,
        "calls_authorized": config.calls_authorized,
        "credential_present": bool(credential_present),
        "endpoint_configured": bool(config.base_url),
        "model_name": config.model or None,
        "image_capability_verified": bool(image_capability_verified),
        "timeout_seconds": config.timeout_seconds,
        "max_concurrency": config.max_concurrency,
        "max_output_tokens": config.max_output_tokens,
        "max_requests_per_day": config.max_requests_per_day,
        "max_call_cost_usd": str(config.max_call_cost_usd),
        "daily_budget_usd": str(config.daily_budget_usd),
        "total_budget_usd": str(config.total_budget_usd)
        if config.total_budget_usd is not None
        else None,
        "thinking_mode": config.thinking_mode or "provider_default",
        "input_price_per_million_usd": (
            str(config.input_price_per_million_usd)
            if config.input_price_per_million_usd is not None
            else None
        ),
        "output_price_per_million_usd": (
            str(config.output_price_per_million_usd)
            if config.output_price_per_million_usd is not None
            else None
        ),
        "cost_reservation_basis": "conservative_input_and_max_output_tokens",
    }


class ConfigurableVisionAdapter:
    """OpenAI-compatible multimodal adapter; disabled until authorized and image-probed."""

    def __init__(
        self,
        config: VisionProviderConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        config = config or VisionProviderConfig.from_env()
        self.config = config
        self.model_id = config.model or None
        self._transport = transport
        self.test_only = transport is not None
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._budget = VisionBudget(config.budget_db_path)
        if config.total_budget_usd is not None:
            self._budget.bind_total_limit(config.total_budget_usd)
        self.image_capability_verified = False
        self._api_key = os.getenv(config.api_key_env, "")

    @property
    def configured(self) -> bool:
        return bool(
            self.config.enabled
            and self.config.calls_authorized
            and self._api_key
            and self.config.base_url
            and self.config.model
            and self.image_capability_verified
            and self.config.max_call_cost_usd > 0
            and self.config.daily_budget_usd > 0
            and self.config.max_requests_per_day > 0
            and self.config.input_price_per_million_usd is not None
            and self.config.input_price_per_million_usd > 0
            and self.config.output_price_per_million_usd is not None
            and self.config.output_price_per_million_usd > 0
        )

    def _preflight(self, request_id: str, prompt: str) -> tuple[bool, dict]:
        if self._budget.fault():
            return False, {"status": "MODEL_PROVIDER_AUTH_BLOCKED"}
        if not self.config.enabled or not self.config.calls_authorized:
            return False, {"status": "MODEL_CALLS_DISABLED"}
        if not self._api_key or not self.config.base_url or not self.config.model:
            return False, {"status": "MODEL_NOT_CONFIGURED"}
        if (
            self.config.daily_budget_usd <= 0
            or self.config.max_call_cost_usd <= 0
            or self.config.max_requests_per_day <= 0
            or self.config.input_price_per_million_usd is None
            or self.config.output_price_per_million_usd is None
            or self.config.input_price_per_million_usd <= 0
            or self.config.output_price_per_million_usd <= 0
        ):
            return False, {"status": "VISION_BUDGET_NOT_CONFIGURED"}
        # UTF-8 bytes upper-bound the prompt tokens; reserve the documented 1024 image
        # tokens plus conservative message/schema overhead. This never rejects or shortens MD.
        input_token_ceiling = len(prompt.encode("utf-8")) + 1280
        maximum_cost = (
            Decimal(input_token_ceiling) * self.config.input_price_per_million_usd
            + Decimal(self.config.max_output_tokens) * self.config.output_price_per_million_usd
        ) / Decimal(1_000_000)
        if maximum_cost > self.config.max_call_cost_usd:
            return False, {"status": "VISION_CALL_COST_CAP_EXCEEDED"}
        ok, reason = self._budget.reserve(
            request_id,
            maximum_cost,
            self.config.daily_budget_usd,
            self.config.max_requests_per_day,
            total_limit=self.config.total_budget_usd,
        )
        return ok, {"status": reason}

    def _estimate_usage_cost(self, usage: dict | None) -> tuple[str | None, str | None]:
        if (
            not isinstance(usage, dict)
            or self.config.input_price_per_million_usd is None
            or self.config.output_price_per_million_usd is None
        ):
            return None, None
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (prompt_tokens, completion_tokens)
        ):
            return None, None
        # Treat every input token as an uncached token for a conservative estimate.
        cost = (
            Decimal(prompt_tokens) * self.config.input_price_per_million_usd
            + Decimal(completion_tokens) * self.config.output_price_per_million_usd
        ) / Decimal(1_000_000)
        return format(cost, "f"), "configured_rates_all_input_tokens_as_uncached"

    def _response_diagnostics(
        self,
        request_id: str,
        response: httpx.Response,
        *,
        payload: dict | None = None,
        parse_error: str | None = None,
    ) -> dict:
        """Return bounded, redacted evidence without retaining request headers or prompt data."""
        raw = response.content
        text = self._redact_response_text(raw.decode("utf-8", errors="replace"))
        try:
            parsed_excerpt = json.loads(text)

            def omit_private_reasoning(value):
                if isinstance(value, dict):
                    return {
                        key: omit_private_reasoning(item)
                        for key, item in value.items()
                        if key not in {"reasoning_content", "reasoning"}
                    }
                if isinstance(value, list):
                    return [omit_private_reasoning(item) for item in value]
                return value

            text = json.dumps(omit_private_reasoning(parsed_excerpt), ensure_ascii=False)
        except (ValueError, TypeError):
            pass
        choices = payload.get("choices") if isinstance(payload, dict) else None
        first_choice = choices[0] if isinstance(choices, list) and choices else None
        choice = first_choice if isinstance(first_choice, dict) else {}
        message = choice.get("message")
        usage = payload.get("usage") if isinstance(payload, dict) else None
        safe_usage = None
        if isinstance(usage, dict):
            allowed_usage = {
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
            }
            safe_usage = {
                name: value
                for name, value in usage.items()
                if name in allowed_usage
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            }
            completion_details = usage.get("completion_tokens_details")
            if (
                isinstance(completion_details, dict)
                and isinstance(completion_details.get("reasoning_tokens"), int)
                and not isinstance(completion_details.get("reasoning_tokens"), bool)
                and completion_details["reasoning_tokens"] >= 0
            ):
                safe_usage["reasoning_tokens"] = completion_details["reasoning_tokens"]
            details = usage.get("prompt_tokens_details")
            if (
                isinstance(details, dict)
                and isinstance(details.get("cached_tokens"), int)
                and not isinstance(details.get("cached_tokens"), bool)
            ):
                safe_usage["cached_tokens"] = max(0, details["cached_tokens"])
        estimated_cost, cost_basis = self._estimate_usage_cost(safe_usage)
        return {
            "request_id": request_id,
            "http_status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "provider_response_id": (
                self._safe_provider_text(payload.get("id")) if isinstance(payload, dict) else None
            ),
            "model_id": (
                self._safe_provider_text(payload.get("model"))
                if isinstance(payload, dict)
                else None
            ),
            "system_fingerprint": (
                self._safe_provider_text(payload.get("system_fingerprint"))
                if isinstance(payload, dict)
                else None
            ),
            "finish_reason": choice.get("finish_reason"),
            "usage": safe_usage,
            "estimated_cost_usd": estimated_cost,
            "cost_estimate_basis": cost_basis,
            "parse_error": parse_error,
            "response_body_sha256": hashlib.sha256(raw).hexdigest(),
            "response_excerpt_redacted": text[:4096],
            "response_excerpt_truncated": len(text) > 4096,
            "message_content_type": (
                type(message.get("content")).__name__ if isinstance(message, dict) else None
            ),
            "message_content_length_chars": self._content_length(message.get("content"))
            if isinstance(message, dict)
            else None,
            "reasoning_content_length_chars": self._content_length(message.get("reasoning_content"))
            if isinstance(message, dict)
            else None,
            "requested_max_tokens": self.config.max_output_tokens,
            "requested_thinking_mode": self.config.thinking_mode or "provider_default_enabled",
            "requested_response_format": "json_object",
        }

    @staticmethod
    def _content_length(value: object) -> int | None:
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            total = 0
            for part in value:
                if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                    return None
                total += len(part["text"])
            return total
        return None

    def _redact_response_text(self, text: str) -> str:
        if self._api_key:
            text = text.replace(self._api_key, "[REDACTED]")
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED_KEY]", text)
        text = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", text)
        return re.sub(
            r'(?i)("(?:api[_-]?key|secret|authorization|access[_-]?token)"\s*:\s*")[^"]*(")',
            r"\1[REDACTED]\2",
            text,
        )

    def _safe_provider_text(self, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        return self._redact_response_text(value)[:256]

    def _exception_diagnostics(self, request_id: str, error_class: str) -> dict:
        return {
            "request_id": request_id,
            "http_status": None,
            "content_type": None,
            "provider_response_id": None,
            "model_id": self.config.model,
            "system_fingerprint": None,
            "finish_reason": None,
            "usage": None,
            "parse_error": error_class,
            "response_body_sha256": None,
            "response_excerpt_redacted": None,
            "response_excerpt_truncated": False,
            "message_content_type": None,
            "message_content_length_chars": None,
            "reasoning_content_length_chars": None,
            "requested_max_tokens": self.config.max_output_tokens,
            "requested_thinking_mode": self.config.thinking_mode or "provider_default_enabled",
            "requested_response_format": "json_object",
        }

    @staticmethod
    def _provider_failure(status: str, diagnostics: dict) -> dict:
        return {"status": status, "provider_diagnostics": diagnostics}

    async def _send_image_request(self, image: bytes, prompt: str, request_id: str) -> dict:
        if len(image) > 32 * 1024 * 1024:
            return {"status": "IMAGE_SIZE_EXCEEDED"}
        try:
            from PIL import Image

            with Image.open(BytesIO(image)) as candidate:
                if candidate.format != "PNG" or candidate.width < 1 or candidate.height < 1:
                    return {"status": "IMAGE_FORMAT_INVALID"}
                if candidate.width > 8192 or candidate.height > 8192:
                    return {"status": "IMAGE_DIMENSIONS_EXCEEDED"}
                candidate.verify()
        except Exception:
            return {"status": "IMAGE_FORMAT_INVALID"}
        url = self.config.base_url.rstrip("/") + self.config.endpoint_path
        if (
            not url.startswith("https://")
            and not url.startswith("http://127.0.0.1")
            and not url.startswith("http://localhost")
        ):
            self._budget.mark(request_id, "REJECTED_INSECURE_ENDPOINT")
            return {"status": "INSECURE_ENDPOINT_REJECTED"}
        body = {
            "model": self.config.model,
            "temperature": 0,
            "max_tokens": self.config.max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(image).decode("ascii")
                            },
                        },
                    ],
                }
            ],
        }
        if self.config.thinking_mode is not None:
            body["thinking"] = {"type": self.config.thinking_mode}
        request_bytes = len(
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if request_bytes > 48 * 1024 * 1024:
            return {"status": "MODEL_REQUEST_SIZE_EXCEEDED"}
        ok, failure = self._preflight(request_id, prompt)
        if not ok:
            return failure
        response = None
        payload = None
        try:
            async with self._semaphore:
                if self._budget.fault():
                    self._budget.mark(request_id, "NOT_SENT_AUTH_BLOCKED", "0")
                    return {"status": "MODEL_PROVIDER_AUTH_BLOCKED"}
                async with httpx.AsyncClient(
                    timeout=self.config.timeout_seconds, transport=self._transport
                ) as client:
                    response = await asyncio.wait_for(
                        client.post(
                            url,
                            headers={"Authorization": f"Bearer {self._api_key}"},
                            json=body,
                        ),
                        timeout=self.config.timeout_seconds,
                    )
                if response.status_code in {401, 402, 403}:
                    self._budget.latch_fault(f"HTTP_{response.status_code}")
            if response.status_code >= 400:
                self._budget.mark(request_id, "HTTP_ERROR")
                diagnostics = self._response_diagnostics(
                    request_id, response, parse_error=f"HTTP_{response.status_code}"
                )
                try:
                    detail = json.dumps(response.json()).casefold()
                except (ValueError, TypeError):
                    detail = ""
                if any(
                    term in detail
                    for term in (
                        "context length",
                        "maximum context",
                        "request too large",
                        "too many tokens",
                    )
                ):
                    return self._provider_failure("MODEL_CONTEXT_OR_REQUEST_LIMIT", diagnostics)
                return self._provider_failure(f"MODEL_HTTP_{response.status_code}", diagnostics)
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json" and not media_type.endswith("+json"):
                self._budget.mark(request_id, "RESPONSE_CONTENT_TYPE_INVALID")
                return self._provider_failure(
                    "MODEL_RESPONSE_CONTENT_TYPE_INVALID",
                    self._response_diagnostics(
                        request_id, response, parse_error="CONTENT_TYPE_NOT_JSON"
                    ),
                )
            try:
                payload = response.json()
            except (ValueError, TypeError):
                self._budget.mark(request_id, "RESPONSE_BODY_INVALID_JSON")
                return self._provider_failure(
                    "MODEL_RESPONSE_BODY_INVALID_JSON",
                    self._response_diagnostics(
                        request_id, response, parse_error="BODY_INVALID_JSON"
                    ),
                )
            if not isinstance(payload, dict):
                self._budget.mark(request_id, "RESPONSE_TOP_LEVEL_NOT_OBJECT")
                return self._provider_failure(
                    "MODEL_RESPONSE_TOP_LEVEL_NOT_OBJECT",
                    self._response_diagnostics(
                        request_id, response, parse_error="TOP_LEVEL_NOT_OBJECT"
                    ),
                )
            diagnostics = self._response_diagnostics(request_id, response, payload=payload)
            returned_model = payload.get("model")
            if returned_model != self.config.model:
                self._budget.mark(request_id, "MODEL_VERSION_MISMATCH")
                return self._provider_failure(
                    "MODEL_VERSION_MISMATCH",
                    {**diagnostics, "parse_error": "MODEL_VERSION_MISMATCH"},
                )
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                self._budget.mark(request_id, "RESPONSE_CHOICES_MISSING")
                return self._provider_failure(
                    "MODEL_RESPONSE_CHOICES_MISSING",
                    {**diagnostics, "parse_error": "CHOICES_MISSING"},
                )
            if not isinstance(choices[0], dict):
                self._budget.mark(request_id, "RESPONSE_CHOICE_NOT_OBJECT")
                return self._provider_failure(
                    "MODEL_RESPONSE_CHOICE_NOT_OBJECT",
                    {**diagnostics, "parse_error": "CHOICE_NOT_OBJECT"},
                )
            finish_reason = choices[0].get("finish_reason")
            if finish_reason == "length":
                self._budget.mark(request_id, "OUTPUT_TRUNCATED")
                return self._provider_failure(
                    "MODEL_OUTPUT_TRUNCATED", {**diagnostics, "parse_error": "OUTPUT_TRUNCATED"}
                )
            finish_failures = {
                "content_filter": "MODEL_RESPONSE_CONTENT_FILTERED",
                "tool_calls": "MODEL_RESPONSE_UNSUPPORTED_FINISH_REASON",
                "insufficient_system_resource": "MODEL_RESPONSE_PROVIDER_INTERRUPTED",
                "aborted": "MODEL_RESPONSE_PROVIDER_INTERRUPTED",
            }
            if finish_reason != "stop":
                status = finish_failures.get(finish_reason, "MODEL_RESPONSE_FINISH_REASON_INVALID")
                self._budget.mark(request_id, status)
                return self._provider_failure(status, {**diagnostics, "parse_error": status})
            message = choices[0].get("message")
            if not isinstance(message, dict):
                self._budget.mark(request_id, "RESPONSE_MESSAGE_MISSING")
                return self._provider_failure(
                    "MODEL_RESPONSE_MESSAGE_MISSING",
                    {**diagnostics, "parse_error": "MESSAGE_MISSING"},
                )
            content = message.get("content")
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                        self._budget.mark(request_id, "RESPONSE_CONTENT_PART_INVALID")
                        return self._provider_failure(
                            "MODEL_RESPONSE_CONTENT_NOT_TEXT",
                            {
                                **diagnostics,
                                "parse_error": "CONTENT_PART_TEXT_NOT_STRING",
                            },
                        )
                    text_parts.append(part["text"])
                content = "".join(text_parts)
            if not isinstance(content, str):
                self._budget.mark(request_id, "RESPONSE_CONTENT_NOT_TEXT")
                return self._provider_failure(
                    "MODEL_RESPONSE_CONTENT_NOT_TEXT",
                    {**diagnostics, "parse_error": "CONTENT_NOT_STRING"},
                )
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                self._budget.mark(request_id, "RESPONSE_CONTENT_NOT_JSON")
                return self._provider_failure(
                    "MODEL_RESPONSE_CONTENT_NOT_JSON",
                    {**diagnostics, "parse_error": "CONTENT_INVALID_JSON"},
                )
            if not isinstance(result, dict):
                self._budget.mark(request_id, "RESPONSE_JSON_NOT_OBJECT")
                return self._provider_failure(
                    "MODEL_RESPONSE_JSON_NOT_OBJECT",
                    {**diagnostics, "parse_error": "CONTENT_JSON_NOT_OBJECT"},
                )
            self._budget.mark(request_id, "RESPONSE_RECEIVED")
            usage = payload.get("usage")
            estimated_cost, cost_basis = self._estimate_usage_cost(usage)
            self._budget.mark(request_id, "RESPONSE_RECEIVED", estimated_cost)
            return {
                **result,
                "model_id": returned_model,
                "provider_fingerprint": self._safe_provider_text(payload.get("system_fingerprint")),
                "usage": usage if isinstance(usage, dict) else None,
                "estimated_cost_usd": estimated_cost,
                "cost_estimate_basis": cost_basis,
                "provider_diagnostics": diagnostics,
            }
        except TimeoutError:
            self._budget.mark(request_id, "TIMEOUT")
            return self._provider_failure(
                "MODEL_TIMEOUT", self._exception_diagnostics(request_id, "TIMEOUT")
            )
        except httpx.HTTPError as exc:
            self._budget.mark(request_id, "NETWORK_ERROR")
            error_response = getattr(exc, "response", None)
            if isinstance(error_response, httpx.Response):
                diagnostics = self._response_diagnostics(
                    request_id, error_response, parse_error=type(exc).__name__
                )
            else:
                diagnostics = self._exception_diagnostics(request_id, type(exc).__name__)
            return self._provider_failure("MODEL_NETWORK_ERROR", diagnostics)
        except (ValueError, KeyError, TypeError) as exc:
            self._budget.mark(request_id, "RESPONSE_INVALID")
            diagnostics = (
                self._response_diagnostics(request_id, response, payload=payload)
                if isinstance(response, httpx.Response)
                else self._exception_diagnostics(request_id, type(exc).__name__)
            )
            return self._provider_failure(
                "MODEL_RESPONSE_INVALID",
                {**diagnostics, "parse_error": type(exc).__name__},
            )

    async def verify_image_capability(
        self, image: bytes, *, expected_marker: str, request_id: str | None = None
    ) -> dict:
        prompt = (
            "This is a multimodal image-capability probe. Inspect the supplied image and return JSON only: "
            '{"observed_marker":"..."}. Copy the exact unique marker visibly printed in the chart.'
        )
        response = await self._send_image_request(
            image, prompt, request_id or "probe:" + str(uuid4())
        )
        if response.get("status"):
            return {"verified": False, **response}
        verified = response.get("observed_marker") == expected_marker
        if self.test_only:
            return {
                "verified": False,
                "status": "TEST_TRANSPORT_ONLY",
                "simulated_marker_match": verified,
                "model_id": response.get("model_id"),
                "provider_fingerprint": response.get("provider_fingerprint"),
                "usage": response.get("usage"),
                "estimated_cost_usd": response.get("estimated_cost_usd"),
                "cost_estimate_basis": response.get("cost_estimate_basis"),
                "provider_diagnostics": response.get("provider_diagnostics"),
            }
        self.image_capability_verified = verified
        return {
            "verified": verified,
            "status": "IMAGE_CAPABILITY_VERIFIED" if verified else "IMAGE_CAPABILITY_NOT_VERIFIED",
            "model_id": response.get("model_id"),
            "provider_fingerprint": response.get("provider_fingerprint"),
            "usage": response.get("usage"),
            "estimated_cost_usd": response.get("estimated_cost_usd"),
            "cost_estimate_basis": response.get("cost_estimate_basis"),
            "provider_diagnostics": response.get("provider_diagnostics"),
        }

    async def decide(
        self,
        *,
        image: bytes,
        strategy_md: str,
        md_version: str,
        agent: str,
        bar_close: datetime | str,
        request_id: str | None = None,
        agent_memory: str = "",
        account_state: dict | None = None,
    ) -> dict:
        if not self.configured:
            state = (
                "IMAGE_CAPABILITY_NOT_VERIFIED"
                if self.config.enabled and self.config.calls_authorized and self._api_key
                else "MODEL_CALLS_DISABLED"
            )
            return {"status": state, "agent": agent, "mode": "PRODUCTION_BLOCKED"}
        try:
            prompt = build_decision_prompt(
                agent=agent,
                md_version=md_version,
                strategy_md=strategy_md,
                account_state=account_state,
                agent_memory=agent_memory,
                prompt_variant=self.config.prompt_variant,
            )
        except ValueError as exc:
            return {
                "status": "PROMPT_REJECTED",
                "reason_code": str(exc),
                "agent": agent,
                "mode": "PRODUCTION_BLOCKED",
            }
        result = await self._send_image_request(image, prompt, request_id or str(uuid4()))
        if result.get("status"):
            return {**result, "agent": agent, "mode": "PRODUCTION_BLOCKED"}
        decision_fields = set(DECISION_SCHEMA["required"])
        provider_fields = {
            "model_id",
            "provider_fingerprint",
            "usage",
            "estimated_cost_usd",
            "cost_estimate_basis",
            "provider_diagnostics",
        }
        if set(result) - decision_fields - provider_fields:
            return {
                "status": "MODEL_DECISION_SCHEMA_INVALID",
                "agent": agent,
                "mode": "PRODUCTION_BLOCKED",
                "provider_diagnostics": result.get("provider_diagnostics"),
            }
        try:
            validate_decision_json({key: result.get(key) for key in decision_fields})
        except (TypeError, ValueError):
            return {
                "status": "MODEL_DECISION_SCHEMA_INVALID",
                "agent": agent,
                "mode": "PRODUCTION_BLOCKED",
                "provider_diagnostics": result.get("provider_diagnostics"),
            }
        close = bar_close.isoformat() if isinstance(bar_close, datetime) else str(bar_close)
        return {
            **result,
            "agent": agent,
            "mode": "TEST_TRANSPORT_ONLY" if self.test_only else "REAL_PROVIDER",
            "md_version": md_version,
            "bar_close_utc": close,
            "snapshot_sha256": hashlib.sha256(image).hexdigest(),
        }


GenericVisionAdapter = ConfigurableVisionAdapter
