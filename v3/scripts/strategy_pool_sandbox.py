"""Isolated F strategy-pool upload/backtest UI; deliberately has no enable endpoint."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from backend.strategy_pool import StrategyPool, StrategyValidationError

ROOT = Path(__file__).resolve().parents[1]
POOL_DB = Path(os.environ["BTC_LAB_SANDBOX_POOL_DB"]).resolve()
CANDLE_DB = Path(os.environ.get("BTC_LAB_SANDBOX_CANDLE_DB", ROOT / "data" / "market-candles.sqlite3")).resolve()
POOL_DB.parent.mkdir(parents=True, exist_ok=True)
pool = StrategyPool(POOL_DB)
app = FastAPI(title="Isolated F Strategy Pool Sandbox", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8012", "http://localhost:8012"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

PAGE = r"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>F 策略池隔离测试</title><style>
body{font:15px system-ui,Segoe UI,sans-serif;max-width:1100px;margin:28px auto;padding:0 18px;color:#18212f;background:#f4f6f9}h1{margin-bottom:6px}.safe{background:#e8f6ef;border:1px solid #95d4b0;padding:12px;border-radius:8px}.card{background:white;border:1px solid #dbe1e8;border-radius:8px;padding:18px;margin:14px 0}button{padding:8px 13px;margin:5px;cursor:pointer}input{padding:7px;margin:5px}pre{white-space:pre-wrap;overflow:auto;background:#f4f6f9;padding:12px;max-height:420px}.bad{color:#a21b1b}.good{color:#17653b}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:7px;text-align:left}
</style><body><h1>F 策略池隔离测试</h1><p class="safe"><b>独立测试实例</b>：策略注册库为临时 SQLite；归档行情只读；此界面不提供启用功能，不连接实时周期，不会写正式账户或订单。</p>
<section class="card"><h2>上传并校验</h2><input id="file" type="file" accept="application/json,.json"><button onclick="upload()">上传并登记（保持暂停）</button><span id="message"></span></section>
<section class="card"><h2>隔离策略</h2><button onclick="load()">刷新</button><div id="items">加载中…</div></section>
<section class="card"><h2>历史回测</h2><p>最长30天；仅读取现有15分钟归档，缺K线会停止回测。</p><label>策略 <select id="strategy"></select></label><label>版本 <select id="version"></select></label><br><label>开始 <input id="start" type="datetime-local" step="900"></label><label>结束 <input id="end" type="datetime-local" step="900"></label><button onclick="backtest()">运行隔离回测</button><div id="report"></div></section>
<script>
const api='/api/v1';const $=id=>document.getElementById(id);async function req(path,opts){const r=await fetch(api+path,opts);const d=await r.json();if(!r.ok)throw Error(JSON.stringify(d.detail||d));return d}
async function load(){try{const d=await req('/strategy-pool');$('items').innerHTML=d.items.length?'<table><tr><th>策略</th><th>状态</th><th>启用</th></tr>'+d.items.map(x=>`<tr><td>${x.id} v${x.position_version||x.active_version||'1.0.0'}</td><td>${x.status}</td><td>${x.enabled?'是':'否'}</td></tr>`).join('')+'</table>':'当前隔离库为空';const s=$('strategy');const old=s.value;s.innerHTML=d.items.map(x=>`<option value="${x.id}">${x.id}</option>`).join('');if(d.items.some(x=>x.id===old))s.value=old;await versions()}catch(e){$('items').textContent='读取失败：'+e.message}}
async function versions(){const id=$('strategy').value;if(!id)return;$('version').innerHTML='';const d=await req(`/strategy-pool/${encodeURIComponent(id)}/versions`);$('version').innerHTML=d.items.map(x=>`<option value="${x.version}">${x.version}</option>`).join('')}
$('strategy').addEventListener('change',versions);
async function upload(){const f=$('file').files[0];if(!f)return;const m=$('message');m.textContent='校验中…';m.className='';try{const d=await req('/strategy-pool/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:await f.text()});m.textContent=`登记成功：${d.strategy_id} v${d.version}；启用状态：否`;m.className='good';await load();$('strategy').value=d.strategy_id;await versions()}catch(e){m.textContent='上传失败：'+e.message;m.className='bad'}}
async function backtest(){const id=$('strategy').value;if(!id)return;const report=$('report');report.textContent='回测中…';try{const start=new Date($('start').value).toISOString(),end=new Date($('end').value).toISOString();const d=await req(`/strategy-pool/${encodeURIComponent(id)}/backtest`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:$('version').value,start,end})});report.innerHTML=`<p><b>净盈亏：</b>${d.net_pnl}　<b>已实现：</b>${d.realized_net_pnl}　<b>费用：</b>${d.fees}　<b>最大回撤：</b>${(Number(d.max_drawdown)*100).toFixed(2)}%　<b>成交记录：</b>${d.trades.length}　<b>缺口：</b>${d.data_gaps.length}</p><details><summary>成交和权益曲线 JSON</summary><pre>${JSON.stringify({trades:d.trades,equity_curve:d.equity_curve,data_gaps:d.data_gaps,censored_positions_at_gaps_or_end:d.censored_positions_at_gaps_or_end,same_bar_stop_target_ambiguities:d.same_bar_stop_target_ambiguities},null,2)}</pre></details>`}catch(e){report.textContent='回测未生成：'+e.message;report.className='bad'}}
load();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def home():
    return PAGE


@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "mode": "ISOLATED_STRATEGY_POOL_SANDBOX", "enable_route": False}


@app.get("/api/v1/strategy-pool")
async def list_pool():
    return {"items": pool.list()}


@app.get("/api/v1/strategy-pool/{strategy_id}/versions")
async def versions(strategy_id: str):
    return {"items": pool.versions(strategy_id)}


@app.get("/api/v1/strategy-pool/{strategy_id}/fills")
async def fills(strategy_id: str):
    return {"items": pool.fills(strategy_id)}


@app.post("/api/v1/strategy-pool/upload")
async def upload(request: Request):
    try:
        raw = await request.body()
        if len(raw) > 64_000:
            raise StrategyValidationError("文件超过 64KB")
        return pool.upload(raw)
    except StrategyValidationError as exc:
        raise HTTPException(422, detail={"valid": False, "errors": [str(exc)]}) from exc


def read_archive(start: datetime, end: datetime):
    if not CANDLE_DB.is_file():
        raise StrategyValidationError("本机行情归档不存在；未创建或补取数据")
    uri = CANDLE_DB.as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=10) as conn:
            rows = conn.execute(
                "SELECT payload FROM candles WHERE symbol=? AND open_at>=? AND open_at<? ORDER BY open_at",
                ("BTC/USD", start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
            ).fetchall()
    except sqlite3.Error as exc:
        raise StrategyValidationError("只读行情归档读取失败") from exc
    return [json.loads(row[0]) for row in rows]


@app.post("/api/v1/strategy-pool/{strategy_id}/backtest")
async def backtest(strategy_id: str, request: Request):
    body = await request.json()
    try:
        version = body.get("version")
        start = datetime.fromisoformat(str(body.get("start")).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(body.get("end")).replace("Z", "+00:00"))
        now = datetime.now(UTC)
        if (not version or start.tzinfo is None or end.tzinfo is None or start >= end
                or end - start > timedelta(days=30) or end > now
                or start.minute % 15 or start.second or start.microsecond
                or end.minute % 15 or end.second or end.microsecond):
            raise ValueError("INVALID_RANGE")
        rows = read_archive(start, end)
        expected = int((end - start).total_seconds() // 900)
        if len(rows) != expected:
            raise StrategyValidationError(f"历史数据不完整：需要 {expected} 根，当前 {len(rows)} 根；未请求补取数据")
        return pool.backtest(strategy_id, version, rows, start=start.isoformat(), end=end.isoformat(),
                             fee_rate=Decimal("0.0005"), slippage=Decimal("0.0002"))
    except StrategyValidationError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, detail="INVALID_BACKTEST_RANGE_OR_DATA") from exc

