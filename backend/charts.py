from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from backend.market import Candle, candle_hash


def _font(size: int):
    windows_font = Path("C:/Windows/Fonts/segoeui.ttf")
    try:
        if windows_font.is_file():
            return ImageFont.truetype(str(windows_font), size=size)
    except OSError:
        pass
    return ImageFont.load_default()


def _render_v2_snapshot(closed: list[Candle], path: str | Path) -> dict:
    global_window, local_window = closed[-288:], closed[-96:]
    width, height = 1920, 1280
    image = Image.new("RGB", (width, height), "#10161f")
    draw = ImageDraw.Draw(image)
    title_font, axis_font = _font(22), _font(16)
    left, right = 112, 1658
    chart_top_offset, chart_bottom_offset = 84, 465
    panel_top = {"global": 22, "local": 660}

    def utc_label(value: str) -> str:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError("CHART_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
        return moment.astimezone(UTC).strftime("%m-%d %H:%MZ")

    def panel(data: list[Candle], name: str, window: str, top: int):
        label = (
            f"{name} WINDOW | {window} | {len(data)} x 15m candles | independent USD price scale"
        )
        draw.text((72, top), label, fill="#f0f4f8", font=title_font)
        start_label, end_label = utc_label(data[0].open_at), utc_label(data[-1].close_at)
        draw.text(
            (72, top + 32),
            f"UTC window: {start_label} to {end_label} | time axis: UTC",
            fill="#9aa7b7",
            font=axis_font,
        )
        ytop, ybottom = top + chart_top_offset, top + chart_bottom_offset
        low = min(float(c.low) for c in data)
        high = max(float(c.high) for c in data)
        span = high - low or 1
        price_ticks = []
        for tick in range(5):
            y = ytop + tick * (ybottom - ytop) / 4
            price = high - tick * span / 4
            price_ticks.append(f"{price:.2f}")
            draw.line((left, y, right, y), fill="#27313e", width=1)
            draw.text((right + 14, y - 10), f"{price:,.2f}", fill="#c3ceda", font=axis_font)
        draw.text((right + 14, ytop - 25), "PRICE (USD)", fill="#9aa7b7", font=axis_font)

        dx = (right - left) / len(data)
        body_w = max(2, int(dx * 0.58))

        def to_y(value: float) -> float:
            return ybottom - (value - low) / span * (ybottom - ytop)

        for index, candle in enumerate(data):
            x = left + (index + 0.5) * dx
            op, hi, lo, cl = map(float, (candle.open, candle.high, candle.low, candle.close))
            color = "#26b37a" if cl >= op else "#e2555b"
            draw.line((x, to_y(hi), x, to_y(lo)), fill=color, width=2)
            body_top = min(to_y(op), to_y(cl))
            body_bottom = max(to_y(op), to_y(cl), body_top + 2)
            draw.rectangle((x - body_w / 2, body_top, x + body_w / 2, body_bottom), fill=color)

        tick_indices = sorted({round(index * (len(data) - 1) / 4) for index in range(5)})
        utc_ticks = []
        for index in tick_indices:
            x = left + (index + 0.5) * dx
            label_value = utc_label(data[index].open_at)
            utc_ticks.append(label_value)
            draw.line((x, ytop, x, ybottom), fill="#1e2834", width=1)
            draw.line((x, ybottom, x, ybottom + 7), fill="#9aa7b7", width=1)
            draw.text((x - 42, ybottom + 12), label_value, fill="#c3ceda", font=axis_font)

        return {
            "label": label,
            "count": len(data),
            "start_utc": data[0].open_at,
            "end_utc": data[-1].close_at,
            "price_scale": "independent USD",
            "price_ticks": price_ticks,
            "utc_ticks": utc_ticks,
        }

    panels = {
        "global": panel(global_window, "GLOBAL", "3 days", panel_top["global"]),
        "local": panel(local_window, "LOCAL", "1 day", panel_top["local"]),
    }
    image.save(path, format="PNG", optimize=False)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "candle_sha256": candle_hash(closed[-288:]),
        "global_count": len(global_window),
        "local_count": len(local_window),
        "price_axes": {
            "global": panels["global"]["price_ticks"],
            "local": panels["local"]["price_ticks"],
        },
        "panels": panels,
        "image_size": [width, height],
        "layout_version": "v2",
        "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
        "indicators": [],
    }


def render_ai_snapshot(
    candles: list[Candle], path: str | Path, *, layout_version: str = "legacy"
) -> dict:
    closed = [c for c in candles if c.is_closed]
    if len(closed) < 288:
        raise ValueError(f"INSUFFICIENT_GLOBAL_CANDLES:{len(closed)}/288")
    if layout_version not in {"legacy", "v2"}:
        raise ValueError("UNKNOWN_CHART_LAYOUT_VERSION")
    if layout_version == "v2":
        return _render_v2_snapshot(closed, path)
    global_window, local_window = closed[-288:], closed[-96:]
    width, panel_h, pad = 1280, 430, 40
    image = Image.new("RGB", (width, panel_h * 2 + 90), "#10161f")
    draw = ImageDraw.Draw(image)

    def panel(data, top, label):
        draw.text((pad, top + 5), label, fill="#e4e9f0")
        left, right, ytop, ybottom = pad, width - pad - 90, top + 36, top + panel_h - 18
        low = min(float(c.low) for c in data)
        high = max(float(c.high) for c in data)
        span = high - low or 1
        price_labels = []
        for n in range(5):
            y = ytop + n * (ybottom - ytop) / 4
            draw.line((left, y, right, y), fill="#27313e", width=1)
            price = high - n * span / 4
            price_labels.append(f"{price:.2f}")
            draw.text((right + 8, y - 7), f"{price:,.2f}", fill="#9aa7b7")
        dx = (right - left) / len(data)
        body_w = max(1, int(dx * 0.55))
        for i, c in enumerate(data):
            x = left + (i + 0.5) * dx

            def to_y(value):
                return ybottom - (value - low) / span * (ybottom - ytop)

            op, hi, lo, cl = map(float, (c.open, c.high, c.low, c.close))
            color = "#26b37a" if cl >= op else "#e2555b"
            draw.line((x, to_y(hi), x, to_y(lo)), fill=color, width=1)
            draw.rectangle(
                (
                    x - body_w / 2,
                    min(to_y(op), to_y(cl)),
                    x + body_w / 2,
                    max(to_y(op), to_y(cl), to_y(op) + 1),
                ),
                fill=color,
            )
        return price_labels

    global_price_axis = panel(global_window, 0, "BTC/USD • 15m • 3 days global")
    local_price_axis = panel(local_window, panel_h + 45, "BTC/USD • 15m • 1 day local")
    image.save(path, format="PNG", optimize=False)
    data_hash = candle_hash(closed[-288:])
    image_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return {
        "path": str(path),
        "sha256": image_hash,
        "candle_sha256": data_hash,
        "global_count": len(global_window),
        "local_count": len(local_window),
        "image_size": list(image.size),
        "layout_version": "legacy",
        "price_axes": {"global": global_price_axis, "local": local_price_axis},
        "source": "BINANCE_USDM_PERP",
        "currency": "USD_RESEARCH",
        "indicators": [],
    }

