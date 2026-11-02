"""Independent BOOK-PA numbered visual evidence; does not affect trader charts."""

import hashlib
from dataclasses import asdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from backend.charts import render_ai_snapshot


def render_numbered_chart(candles, path):
    if len(candles) != 288 or any(not c.is_closed for c in candles):
        raise ValueError("CLOSED_288_REQUIRED")
    path = Path(path)
    old = render_ai_snapshot(candles, path, layout_version="v2")
    with Image.open(path) as original:
        image = Image.new("RGB", (1920, 1350), "#10161f")
        image.paste(original.crop((0, 0, 1920, 620)), (0, 0))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 17)
    draw.text(
        (72, 660),
        "LOCAL: LAST 48 CLOSED CANDLES | UTC | K01 oldest - K48 newest",
        font=font,
        fill="#e4e9f0",
    )
    local = candles[-48:]
    low, high = min(float(c.low) for c in local), max(float(c.high) for c in local)
    span = high - low or 1
    left, right, top, bottom = 112, 1658, 720, 1220
    dx = (right - left) / 48

    def y(price):
        return bottom - (price - low) / span * (bottom - top)

    for i, c in enumerate(local):
        x = left + (i + 0.5) * dx
        op, hi, lo, cl = map(float, (c.open, c.high, c.low, c.close))
        color = "#26b37a" if cl >= op else "#e2555b"
        draw.line((x, y(hi), x, y(lo)), fill=color, width=2)
        draw.rectangle(
            (x - 7, min(y(op), y(cl)), x + 7, max(y(op), y(cl), min(y(op), y(cl)) + 2)), fill=color
        )
        draw.text((x - 13, bottom + 12), f"K{i + 1:02}", font=font, fill="#c3ceda")
    image.save(path, format="PNG")
    mapping = [{"id": f"K{i + 1:02}", **asdict(c)} for i, c in enumerate(local)]
    return {
        "chart_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "candle_sha256": old["candle_sha256"],
        "numbered_candles": mapping,
        "layout_version": "book_pa_v03_numbered",
    }
