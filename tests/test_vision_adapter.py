import asyncio
import base64
import hashlib
import json
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from backend.vision import GenericVisionAdapter, VisionProviderConfig, public_vision_status


def make_png():
    output = BytesIO()
    image = Image.new("RGB", (320, 96), color="white")
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    draw.rectangle((18, 18, 42, 78), fill="blue")
    draw.text((60, 40), "BLUE_CANDLE_17", fill="black")
    image.save(output, format="PNG")
    return output.getvalue()


PNG = make_png()


def config(**changes):
    data = dict(
        enabled=True,
        calls_authorized=True,
        base_url="https://vision.example/v1",
        model="vision-test-v2",
        api_key_env="VISION_TEST_SECRET",
        timeout_seconds=2,
        max_concurrency=1,
        max_call_cost_usd=Decimal("0.02"),
        daily_budget_usd=Decimal("0.025"),
        estimated_call_cost_usd=Decimal("0.01"),
        max_output_tokens=1000,
        max_requests_per_day=12,
        input_price_per_million_usd=Decimal("0.30"),
        output_price_per_million_usd=Decimal("1.20"),
        budget_db_path=":memory:",
    )
    data.update(changes)
    return VisionProviderConfig(**data)


def provider_reply(content, model="vision-test-v2"):
    return httpx.Response(
        200,
        json={
            "id": "cmpl-local-test",
            "model": model,
            "system_fingerprint": "fp_local_test_only",
            "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        },
    )


def valid_wait_decision(rationale="No setup"):
    return json.dumps(
        {
            "action": "WAIT",
            "global_trend": "unclear",
            "local_trend": "unclear",
            "structure": "unclear",
            "key_highs": [],
            "key_lows": [],
            "rationale": rationale,
            "stop_loss": None,
        }
    )


def test_disabled_adapter_never_sends_without_authorization_or_capability(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "test-only")
    calls = []

    async def handler(request):
        calls.append(request)
        return provider_reply('{"action":"WAIT","rationale":"x"}')

    adapter = GenericVisionAdapter(
        config(calls_authorized=False), transport=httpx.MockTransport(handler)
    )
    result = asyncio.run(
        adapter.decide(
            image=PNG,
            agent="A",
            strategy_md="md",
            md_version="v1",
            bar_close="2026-01-01T00:00:00Z",
        )
    )
    assert result["status"] == "MODEL_CALLS_DISABLED"
    assert calls == []


def test_environment_defaults_fail_closed_without_persistent_budget_or_secret(
    monkeypatch, tmp_path
):
    for key in (
        "VISION_ENABLED",
        "VISION_CALLS_AUTHORIZED",
        "VISION_BASE_URL",
        "VISION_MODEL",
        "VISION_API_KEY_ENV",
        "VISION_DAILY_BUDGET_USD",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VISION_BUDGET_DB", ":memory:")
    cfg = VisionProviderConfig.from_env()
    adapter = GenericVisionAdapter(cfg)
    assert not adapter.configured
    assert cfg.enabled is False and cfg.calls_authorized is False
    assert cfg.estimated_call_cost_usd == 0 and cfg.daily_budget_usd == 0


def test_environment_defaults_to_deepseek_vision_but_stays_disabled():
    cfg = VisionProviderConfig.from_env(environ={})
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.endpoint_path == "/chat/completions"
    assert cfg.model == "deepseek-flash"
    assert cfg.api_key_env == "DEEPSEEK_API_KEY"
    assert cfg.enabled is False and cfg.calls_authorized is False
    assert cfg.estimated_call_cost_usd == 0
    assert cfg.daily_budget_usd == 0


def test_public_model_status_never_contains_credential_value():
    secret = "unit-test-secret-must-not-escape"
    cfg = config(model="vision-visible-model")
    status = public_vision_status(cfg, credential_present=bool(secret))
    assert status["credential_present"] is True
    assert status["state"] == "IMAGE_CAPABILITY_UNVERIFIED"
    assert secret not in json.dumps(status)
    assert "https://vision.example/v1" not in json.dumps(status)


def test_public_vision_status_exposes_hard_caps_and_rate_basis_without_static_estimate():
    cfg = config(
        max_output_tokens=512,
        max_requests_per_day=12,
        input_price_per_million_usd=Decimal("0.30"),
        output_price_per_million_usd=Decimal("1.20"),
    )
    status = public_vision_status(cfg, credential_present=False)
    assert status["max_output_tokens"] == 512
    assert status["max_requests_per_day"] == 12
    assert status["input_price_per_million_usd"] == "0.30"
    assert status["output_price_per_million_usd"] == "1.20"
    assert status["cost_reservation_basis"] == "conservative_input_and_max_output_tokens"
    assert "estimated_call_cost_usd" not in status


def test_non_tls_remote_model_endpoint_is_rejected_at_configuration():
    with pytest.raises(ValueError, match="HTTPS"):
        config(base_url="http://vision.example/v1")
    with pytest.raises(ValueError, match="embed credentials"):
        config(base_url="https://user:password@vision.example/v1")


def test_capability_probe_and_decide_send_image_data_and_enforce_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("VISION_TEST_SECRET", "unit-test-secret")
    payloads = []

    async def handler(request):
        body = json.loads(request.content)
        payloads.append(body)
        if "probe" in body["messages"][0]["content"][0]["text"].lower():
            return provider_reply('{"observed_marker":"BLUE_CANDLE_17"}')
        return provider_reply(valid_wait_decision())

    cfg = config(
        max_requests_per_day=2,
        budget_db_path=str(tmp_path / "budget.sqlite"),
    )
    adapter = GenericVisionAdapter(cfg, transport=httpx.MockTransport(handler))
    verified = asyncio.run(adapter.verify_image_capability(PNG, expected_marker="BLUE_CANDLE_17"))
    assert verified["verified"] is False
    assert verified["status"] == "TEST_TRANSPORT_ONLY"
    assert verified["simulated_marker_match"] is True
    assert verified["provider_fingerprint"] == "fp_local_test_only"
    adapter.image_capability_verified = (
        True  # Exercises request serialization only; not a capability claim.
    )
    decision = asyncio.run(
        adapter.decide(
            image=PNG,
            agent="A",
            strategy_md="only A md",
            md_version="v1",
            bar_close="2026-01-01T00:00:00Z",
        )
    )
    assert decision["action"] == "WAIT" and decision["mode"] == "TEST_TRANSPORT_ONLY"
    for body in payloads:
        assert body["model"] == "vision-test-v2"
        assert body["response_format"] == {"type": "json_object"}
        image = body["messages"][0]["content"][1]["image_url"]["url"]
        assert image == "data:image/png;base64," + base64.b64encode(PNG).decode()
    exhausted = asyncio.run(
        adapter.decide(
            image=PNG,
            agent="B",
            strategy_md="B md",
            md_version="v1",
            bar_close="2026-01-01T00:00:00Z",
        )
    )
    assert exhausted["status"] == "VISION_CALL_COUNT_EXHAUSTED"
    assert len(payloads) == 2


def test_provider_response_records_model_fingerprint_usage_and_configured_cost(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "unit-test-secret")
    response = httpx.Response(
        200,
        json={
            "model": "vision-test-v2",
            "system_fingerprint": "fp_local_test_only",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": '{"observed_marker":"BLUE_CANDLE_17"}'},
                }
            ],
        },
    )
    adapter = GenericVisionAdapter(
        config(
            input_price_per_million_usd=Decimal("0.30"),
            output_price_per_million_usd=Decimal("1.20"),
        ),
        transport=httpx.MockTransport(lambda _: response),
    )
    result = asyncio.run(adapter.verify_image_capability(PNG, expected_marker="BLUE_CANDLE_17"))
    assert result["provider_fingerprint"] == "fp_local_test_only"
    assert result["usage"]["total_tokens"] == 15
    assert result["estimated_cost_usd"] == "0.000009"
    assert result["provider_diagnostics"]["finish_reason"] == "stop"
    assert result["provider_diagnostics"]["usage"]["total_tokens"] == 15


def test_decide_rejects_json_object_missing_required_visual_decision_fields(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "unit-test-secret")
    adapter = GenericVisionAdapter(
        config(),
        transport=httpx.MockTransport(
            lambda _: provider_reply('{"action":"WAIT","rationale":"partial"}')
        ),
    )
    adapter.image_capability_verified = True
    result = asyncio.run(
        adapter.decide(
            image=PNG,
            agent="A",
            strategy_md="A-only MD",
            md_version="v1.0.0",
            bar_close="2026-01-01T00:00:00Z",
        )
    )
    assert result["status"] == "MODEL_DECISION_SCHEMA_INVALID"


def test_text_only_or_wrong_marker_does_not_verify_image_support(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "unit-test-secret")
    adapter = GenericVisionAdapter(
        config(),
        transport=httpx.MockTransport(lambda _: provider_reply('{"observed_marker":"WRONG"}')),
    )
    result = asyncio.run(adapter.verify_image_capability(PNG, expected_marker="BLUE_CANDLE_17"))
    assert result["verified"] is False and result["status"] == "TEST_TRANSPORT_ONLY"
    assert result["simulated_marker_match"] is False
    assert (
        asyncio.run(
            adapter.decide(
                image=PNG,
                agent="A",
                strategy_md="md",
                md_version="v1",
                bar_close="2026-01-01T00:00:00Z",
            )
        )["status"]
        == "IMAGE_CAPABILITY_NOT_VERIFIED"
    )


def test_per_call_budget_rejects_before_network(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "unit-test-secret")
    calls = []
    adapter = GenericVisionAdapter(
        config(max_call_cost_usd=Decimal("0.0000001")),
        transport=httpx.MockTransport(lambda req: calls.append(req) or provider_reply("{}")),
    )
    result = asyncio.run(adapter.verify_image_capability(PNG, expected_marker="x"))
    assert result["status"] == "VISION_CALL_COST_CAP_EXCEEDED"
    assert calls == []


def test_zero_token_price_schedule_fails_closed_before_network(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    calls = []
    adapter = GenericVisionAdapter(
        config(
            input_price_per_million_usd=Decimal("0"),
            output_price_per_million_usd=Decimal("1.20"),
        ),
        transport=httpx.MockTransport(lambda req: calls.append(req) or provider_reply("{}")),
    )
    result = asyncio.run(adapter.verify_image_capability(PNG, expected_marker="x"))
    assert result["status"] == "VISION_BUDGET_NOT_CONFIGURED"
    assert calls == []


def test_zero_token_price_schedule_never_reports_adapter_configured(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    adapter = GenericVisionAdapter(
        config(input_price_per_million_usd=Decimal("0")),
        transport=httpx.MockTransport(lambda _: provider_reply("{}")),
    )
    adapter.image_capability_verified = True
    assert adapter.configured is False


def test_different_provider_model_version_is_rejected(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "unit-test-secret")
    response = httpx.Response(
        200,
        json={
            "model": "unexpected-model-version",
            "choices": [{"message": {"content": '{"observed_marker":"BLUE_CANDLE_17"}'}}],
        },
    )
    adapter = GenericVisionAdapter(config(), transport=httpx.MockTransport(lambda _: response))
    result = asyncio.run(adapter.verify_image_capability(PNG, expected_marker="BLUE_CANDLE_17"))
    assert result["verified"] is False
    assert result["status"] == "MODEL_VERSION_MISMATCH"


def test_provider_timeout_is_not_retried(monkeypatch, tmp_path):
    monkeypatch.setenv("VISION_TEST_SECRET", "unit-test-secret")
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.05)
        return provider_reply('{"observed_marker":"X"}')

    adapter = GenericVisionAdapter(
        config(timeout_seconds=0.01, budget_db_path=str(tmp_path / "t.sqlite")),
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(adapter.verify_image_capability(PNG, expected_marker="X"))
    assert result["status"] == "MODEL_TIMEOUT" and len(calls) == 1


def test_image_concurrency_cap_is_enforced(monkeypatch, tmp_path):
    monkeypatch.setenv("VISION_TEST_SECRET", "unit-test-secret")
    active = 0
    peak = 0

    async def handler(_request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return provider_reply('{"observed_marker":"BLUE_CANDLE_17"}')

    adapter = GenericVisionAdapter(
        config(
            max_concurrency=1,
            daily_budget_usd=Decimal("0.05"),
            budget_db_path=str(tmp_path / "parallel.sqlite"),
        ),
        transport=httpx.MockTransport(handler),
    )

    async def run():
        return await asyncio.gather(
            *(
                adapter.verify_image_capability(PNG, expected_marker="BLUE_CANDLE_17")
                for _ in range(4)
            )
        )

    results = asyncio.run(run())
    assert peak == 1
    assert all(item["status"] == "TEST_TRANSPORT_ONLY" for item in results)
    assert all(item["simulated_marker_match"] for item in results)


def test_corrupt_png_is_rejected_without_network(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "unit-test-secret")
    calls = []
    adapter = GenericVisionAdapter(
        config(),
        transport=httpx.MockTransport(lambda req: calls.append(req) or provider_reply("{}")),
    )
    result = asyncio.run(
        adapter.verify_image_capability(b"\x89PNG\r\n\x1a\nnot-a-png", expected_marker="X")
    )
    assert result["status"] == "IMAGE_FORMAT_INVALID" and calls == []


def test_image_over_provider_dimension_limit_is_rejected_before_request(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "unit-test-secret")
    output = BytesIO()
    Image.new("RGB", (8193, 1), color="red").save(output, format="PNG")
    calls = []
    adapter = GenericVisionAdapter(
        config(),
        transport=httpx.MockTransport(lambda req: calls.append(req) or provider_reply("{}")),
    )
    result = asyncio.run(adapter.verify_image_capability(output.getvalue(), expected_marker="X"))
    assert result["status"] == "IMAGE_DIMENSIONS_EXCEEDED"
    assert calls == []


def test_inline_request_over_provider_body_limit_is_rejected_before_network(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    calls = []
    adapter = GenericVisionAdapter(
        config(),
        transport=httpx.MockTransport(lambda req: calls.append(req) or provider_reply("{}")),
    )
    prompt = "x" * (48 * 1024 * 1024)
    result = asyncio.run(adapter._send_image_request(PNG, prompt, "oversized-request"))
    assert result["status"] == "MODEL_REQUEST_SIZE_EXCEEDED"
    assert calls == []


def test_deepseek_contract_receives_real_combined_chart_png_via_local_transport(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key-only")
    image = (Path(__file__).parent / "fixtures" / "vision" / "case_04.png").read_bytes()
    seen = []

    async def handler(request):
        seen.append(request)
        return provider_reply(valid_wait_decision("local transport fixture"), "deepseek-flash")

    cfg = config(
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        api_key_env="DEEPSEEK_API_KEY",
        budget_db_path=str(tmp_path / "deepseek-budget.sqlite"),
    )
    adapter = GenericVisionAdapter(cfg, transport=httpx.MockTransport(handler))
    adapter.image_capability_verified = (
        True  # Serialization test only; the real probe remains unrun.
    )
    response = asyncio.run(
        adapter.decide(
            image=image,
            agent="A",
            strategy_md="A-only-test-MD",
            md_version="A-test-v1",
            bar_close="2026-01-01T00:00:00Z",
            agent_memory="A-only-memory",
            account_state={"position": "FLAT"},
        )
    )
    assert response["mode"] == "TEST_TRANSPORT_ONLY"
    assert response["action"] == "WAIT"
    assert response["provider_fingerprint"] == "fp_local_test_only"
    diagnostics = response["provider_diagnostics"]
    assert diagnostics["request_id"]
    assert diagnostics["http_status"] == 200
    assert diagnostics["content_type"].startswith("application/json")
    assert diagnostics["provider_response_id"] == "cmpl-local-test"
    assert diagnostics["finish_reason"] == "stop"
    assert diagnostics["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 5,
        "total_tokens": 12,
    }
    assert len(diagnostics["response_body_sha256"]) == 64
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["Authorization"] == "Bearer local-test-key-only"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-flash"
    assert body["response_format"] == {"type": "json_object"}
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "A-only-test-MD" in content[0]["text"]
    assert "A-only-memory" in content[0]["text"]
    assert '"position": "FLAT"' in content[0]["text"]
    assert "downtrend_then_reversal" not in content[0]["text"]
    assert "rising_reversal" not in content[0]["text"]
    data_url = content[1]["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    decoded = base64.b64decode(data_url.split(",", 1)[1])
    assert decoded == image
    with Image.open(BytesIO(decoded)) as chart:
        assert chart.size == (1280, 950)


def test_request_uses_configured_output_tokens_and_lossless_long_md(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    long_md = "FULL_RULE_MARKER " * 5000
    seen = []

    async def handler(request):
        seen.append(json.loads(request.content))
        return provider_reply(valid_wait_decision())

    adapter = GenericVisionAdapter(
        config(
            max_output_tokens=333,
            max_call_cost_usd=Decimal("0.10"),
            daily_budget_usd=Decimal("0.20"),
        ),
        transport=httpx.MockTransport(handler),
    )
    adapter.image_capability_verified = True
    result = asyncio.run(
        adapter.decide(
            image=PNG,
            agent="A",
            strategy_md=long_md,
            md_version="v-long",
            bar_close="2026-01-01T00:00:00Z",
        )
    )
    assert result["action"] == "WAIT"
    assert seen[0]["max_tokens"] == 333
    prompt = seen[0]["messages"][0]["content"][0]["text"]
    assert prompt.count("FULL_RULE_MARKER") == 5000


def test_provider_context_rejection_is_explicit_and_never_truncates_md(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"message": "maximum context length exceeded"}})

    adapter = GenericVisionAdapter(config(), transport=httpx.MockTransport(handler))
    adapter.image_capability_verified = True
    result = asyncio.run(
        adapter.decide(
            image=PNG,
            agent="A",
            strategy_md="FULL_RULE_MARKER " * 100,
            md_version="v1",
            bar_close="2026-01-01T00:00:00Z",
        )
    )
    assert result["status"] == "MODEL_CONTEXT_OR_REQUEST_LIMIT"
    assert len(calls) == 1


def test_truncated_provider_json_is_rejected_on_finish_reason_length(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    response = httpx.Response(
        200,
        json={
            "model": "vision-test-v2",
            "choices": [{"finish_reason": "length", "message": {"content": '{"action":"WAIT"'}}],
        },
    )
    adapter = GenericVisionAdapter(config(), transport=httpx.MockTransport(lambda _: response))
    result = asyncio.run(adapter.verify_image_capability(PNG, expected_marker="x"))
    assert result["status"] == "MODEL_OUTPUT_TRUNCATED"


def test_candidate_disabled_thinking_is_explicit_and_default_preserves_provider_mode(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    payloads = []

    async def handler(request):
        payloads.append(json.loads(request.content))
        return provider_reply(valid_wait_decision())

    for mode in (None, "disabled"):
        adapter = GenericVisionAdapter(
            config(thinking_mode=mode), transport=httpx.MockTransport(handler)
        )
        result = asyncio.run(adapter._send_image_request(PNG, "json only", f"thinking-{mode}"))
        assert result["action"] == "WAIT"

    assert "thinking" not in payloads[0]
    assert payloads[1]["thinking"] == {"type": "disabled"}
    assert payloads[1]["response_format"] == {"type": "json_object"}


def test_diagnostics_record_reasoning_token_count_without_reasoning_text(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    raw = json.dumps(
        {
            "model": "vision-test-v2",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "completion_tokens_details": {"reasoning_tokens": 17},
            },
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": valid_wait_decision(),
                        "reasoning_content": "private reasoning must not persist",
                    },
                }
            ],
        }
    ).encode()
    response = httpx.Response(200, headers={"content-type": "application/json"}, content=raw)
    adapter = GenericVisionAdapter(config(), transport=httpx.MockTransport(lambda _: response))

    result = asyncio.run(adapter._send_image_request(PNG, "json only", "reasoning-usage"))

    evidence = result["provider_diagnostics"]
    assert evidence["usage"]["reasoning_tokens"] == 17
    assert evidence["message_content_length_chars"] == len(valid_wait_decision())
    assert evidence["reasoning_content_length_chars"] == len("private reasoning must not persist")
    assert "private reasoning must not persist" not in json.dumps(evidence)


def test_request_count_cap_rejects_before_network(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    calls = []
    adapter = GenericVisionAdapter(
        config(max_requests_per_day=1, daily_budget_usd=Decimal("0.05")),
        transport=httpx.MockTransport(
            lambda req: calls.append(req) or provider_reply('{"observed_marker":"x"}')
        ),
    )
    first = asyncio.run(adapter.verify_image_capability(PNG, expected_marker="x", request_id="one"))
    second = asyncio.run(
        adapter.verify_image_capability(PNG, expected_marker="x", request_id="two")
    )
    assert first["status"] == "TEST_TRANSPORT_ONLY"
    assert second["status"] == "VISION_CALL_COUNT_EXHAUSTED"
    assert len(calls) == 1


def test_cost_reservation_includes_image_and_message_overhead_before_output_ceiling(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    cfg = config(max_output_tokens=512)
    adapter = GenericVisionAdapter(
        cfg, transport=httpx.MockTransport(lambda _: provider_reply("{}"))
    )
    ok, result = adapter._preflight("cost-ceiling", "x")
    assert ok and result["status"] == "RESERVED"
    reserved = adapter._budget._db.execute(
        "SELECT reserved_usd FROM vision_budget_calls WHERE request_id='cost-ceiling'"
    ).fetchone()[0]
    expected = (Decimal(1 + 1280) * Decimal("0.30") + Decimal(512) * Decimal("1.20")) / Decimal(
        1_000_000
    )
    assert Decimal(reserved) == expected


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ([], "MODEL_RESPONSE_TOP_LEVEL_NOT_OBJECT"),
        ({"model": "vision-test-v2", "choices": []}, "MODEL_RESPONSE_CHOICES_MISSING"),
        (
            {
                "model": "vision-test-v2",
                "choices": [{"finish_reason": "stop", "message": "wrong-shape"}],
            },
            "MODEL_RESPONSE_MESSAGE_MISSING",
        ),
        (
            {
                "model": "vision-test-v2",
                "choices": [{"finish_reason": "stop", "message": {"content": "private body"}}],
            },
            "MODEL_RESPONSE_CONTENT_NOT_JSON",
        ),
    ],
)
def test_provider_malformed_responses_return_safe_specific_diagnostics(
    monkeypatch, payload, expected_status
):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    response = httpx.Response(200, json=payload)
    adapter = GenericVisionAdapter(config(), transport=httpx.MockTransport(lambda _: response))
    result = asyncio.run(adapter._send_image_request(PNG, "unit test prompt", expected_status))
    assert result["status"] == expected_status
    assert result["provider_diagnostics"]["request_id"] == expected_status
    if payload and payload["choices"] and isinstance(payload["choices"][0].get("message"), dict):
        assert "private body" in result["provider_diagnostics"]["response_excerpt_redacted"]
    assert "VISION_TEST_SECRET" not in json.dumps(result)


def test_nullable_content_part_returns_a_request_scoped_diagnostic(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-secret-value")
    payload = {
        "id": "cmpl-local-response",
        "model": "vision-test-v2",
        "system_fingerprint": "fp_local_test_only",
        "usage": {"prompt_tokens": 20, "completion_tokens": 0, "total_tokens": 20},
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": [{"type": "text", "text": None}]},
            }
        ],
    }
    response = httpx.Response(200, json=payload)
    adapter = GenericVisionAdapter(config(), transport=httpx.MockTransport(lambda _: response))

    result = asyncio.run(
        adapter._send_image_request(PNG, "unit test prompt", "request-diagnostic-001")
    )

    assert result["status"] == "MODEL_RESPONSE_CONTENT_NOT_TEXT"
    evidence = result["provider_diagnostics"]
    assert evidence["request_id"] == "request-diagnostic-001"
    assert evidence["http_status"] == 200
    assert evidence["content_type"] == "application/json"
    assert evidence["model_id"] == "vision-test-v2"
    assert evidence["system_fingerprint"] == "fp_local_test_only"
    assert evidence["finish_reason"] == "stop"
    assert evidence["usage"] == payload["usage"]
    assert evidence["parse_error"] == "CONTENT_PART_TEXT_NOT_STRING"
    assert "local-test-secret-value" not in json.dumps(evidence)


def test_redacted_response_excerpt_keeps_parse_evidence_without_echoing_key(monkeypatch):
    local_key = "local-test-secret-value"
    monkeypatch.setenv("VISION_TEST_SECRET", local_key)
    response = provider_reply(f"not-json {local_key}")
    adapter = GenericVisionAdapter(config(), transport=httpx.MockTransport(lambda _: response))

    result = asyncio.run(
        adapter._send_image_request(PNG, "unit test prompt", "redaction-diagnostic-001")
    )

    assert result["status"] == "MODEL_RESPONSE_CONTENT_NOT_JSON"
    excerpt = result["provider_diagnostics"]["response_excerpt_redacted"]
    assert "not-json" in excerpt
    assert local_key not in excerpt
    assert "[REDACTED]" in excerpt


def test_response_excerpt_omits_provider_reasoning_but_retains_raw_hash(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    raw = json.dumps(
        {
            "model": "vision-test-v2",
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "content": "",
                        "reasoning_content": "private chain of thought",
                    },
                }
            ],
        }
    ).encode()
    response = httpx.Response(200, headers={"content-type": "application/json"}, content=raw)
    adapter = GenericVisionAdapter(config(), transport=httpx.MockTransport(lambda _: response))

    result = asyncio.run(
        adapter._send_image_request(PNG, "unit test prompt", "reasoning-redaction-001")
    )

    evidence = result["provider_diagnostics"]
    assert result["status"] == "MODEL_OUTPUT_TRUNCATED"
    assert "private chain of thought" not in evidence["response_excerpt_redacted"]
    assert evidence["response_body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert evidence["finish_reason"] == "length"


def test_json_body_with_wrong_content_type_is_rejected_and_evidenced(monkeypatch):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    response = httpx.Response(
        200,
        headers={"content-type": "text/plain"},
        content=json.dumps(
            {
                "model": "vision-test-v2",
                "choices": [
                    {"finish_reason": "stop", "message": {"content": valid_wait_decision()}}
                ],
            }
        ),
    )
    adapter = GenericVisionAdapter(config(), transport=httpx.MockTransport(lambda _: response))

    result = asyncio.run(
        adapter._send_image_request(PNG, "unit test prompt", "wrong-content-type-001")
    )

    assert result["status"] == "MODEL_RESPONSE_CONTENT_TYPE_INVALID"
    evidence = result["provider_diagnostics"]
    assert evidence["http_status"] == 200
    assert evidence["content_type"] == "text/plain"
    assert evidence["parse_error"] == "CONTENT_TYPE_NOT_JSON"
    assert evidence["request_id"] == "wrong-content-type-001"


@pytest.mark.parametrize(
    ("finish_reason", "expected_status"),
    [
        ("content_filter", "MODEL_RESPONSE_CONTENT_FILTERED"),
        ("tool_calls", "MODEL_RESPONSE_UNSUPPORTED_FINISH_REASON"),
        ("insufficient_system_resource", "MODEL_RESPONSE_PROVIDER_INTERRUPTED"),
        ("aborted", "MODEL_RESPONSE_PROVIDER_INTERRUPTED"),
    ],
)
def test_non_success_finish_reasons_are_rejected_with_metadata(
    monkeypatch, finish_reason, expected_status
):
    monkeypatch.setenv("VISION_TEST_SECRET", "local-test-only")
    response = httpx.Response(
        200,
        json={
            "model": "vision-test-v2",
            "system_fingerprint": "fp_local_test_only",
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            "choices": [
                {"finish_reason": finish_reason, "message": {"content": valid_wait_decision()}}
            ],
        },
    )
    adapter = GenericVisionAdapter(config(), transport=httpx.MockTransport(lambda _: response))

    result = asyncio.run(
        adapter._send_image_request(PNG, "unit test prompt", "finish-reason-request")
    )

    assert result["status"] == expected_status
    assert result["provider_diagnostics"]["finish_reason"] == finish_reason
    assert result["provider_diagnostics"]["request_id"] == "finish-reason-request"
