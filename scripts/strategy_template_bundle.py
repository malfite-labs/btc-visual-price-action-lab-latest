"""BTC V3.1 Strategy Development Template Bundle generator (in-sync with backend/strategy_pool.py)."""
from __future__ import annotations

import json
from pathlib import Path
import zipfile

TEMPLATE_ZIP_NAME = 'BTC_V3_策略开发模板.zip'


def strategy_template_dict() -> dict:
    """Blank placeholder template; intentionally invalid until filled so it can never be accidentally enabled."""
    return {
        'format': 'btc-lab-declarative-strategy/v2',
        'strategy_id': '',
        'version': '',
        'name': '',
        'market': 'BTC/USD',
        'timeframe': '15m',
        'description': '',
        'parameters': {},
        'risk': {
            'initial_equity': '10000',
            'currency': 'USD_RESEARCH',
            'max_leverage': 5,
            'risk_fraction': '0.02',
        },
        'entry': {
            'long': None,
            'short': None,
        },
        'position': {
            'direction': None,
            'entry_timing': 'next_open',
            'stop': {
                'kind': 'swing_atr',
                'lookback': None,
                'atr_period': None,
                'buffer_atr': None,
                'min_distance_atr': None,
                'max_distance_atr': None,
            },
            'target_r': None,
            'max_hold_bars': None,
            'same_bar_priority': 'stop',
        },
    }


def _op_schema(op: str, prefix_items: list[dict]) -> dict:
    n = len(prefix_items)
    return {
        'type': 'object',
        'properties': {
            'op': {'const': op},
            'args': {
                'type': 'array',
                'prefixItems': prefix_items,
                'items': False,
                'minItems': n,
                'maxItems': n,
            },
        },
        'required': ['op', 'args'],
        'additionalProperties': False,
    }


def _op_1_or_2_schema(op: str) -> dict:
    period = {'type': 'integer', 'minimum': 1, 'maximum': 200}
    offset = {'type': 'integer', 'minimum': 0, 'maximum': 199}
    return {
        'type': 'object',
        'properties': {
            'op': {'const': op},
            'args': {
                'oneOf': [
                    {'type': 'array', 'prefixItems': [period], 'items': False, 'minItems': 1, 'maxItems': 1},
                    {'type': 'array', 'prefixItems': [period, offset], 'items': False, 'minItems': 2, 'maxItems': 2},
                ]
            },
        },
        'required': ['op', 'args'],
        'additionalProperties': False,
    }


def strategy_schema_dict() -> dict:
    """Machine-readable JSON Schema (Draft 2020-12) matching backend/strategy_pool.py v2 safe subset."""
    expr_ref = {'$ref': '#/$defs/expression'}
    offset_int = {'type': 'integer', 'minimum': 0, 'maximum': 199}
    period_int = {'type': 'integer', 'minimum': 1, 'maximum': 200}

    expr_variants = [
        {'type': 'number'},
        {
            'type': 'object',
            'properties': {
                'op': {'const': 'and'},
                'args': {'type': 'array', 'items': expr_ref, 'minItems': 2, 'maxItems': 16},
            },
            'required': ['op', 'args'],
            'additionalProperties': False,
        },
        {
            'type': 'object',
            'properties': {
                'op': {'const': 'or'},
                'args': {'type': 'array', 'items': expr_ref, 'minItems': 2, 'maxItems': 16},
            },
            'required': ['op', 'args'],
            'additionalProperties': False,
        },
        _op_schema('not', [expr_ref]),
        _op_schema('gt', [expr_ref, expr_ref]),
        _op_schema('gte', [expr_ref, expr_ref]),
        _op_schema('lt', [expr_ref, expr_ref]),
        _op_schema('lte', [expr_ref, expr_ref]),
        _op_schema('eq', [expr_ref, expr_ref]),
        _op_schema('open', [offset_int]),
        _op_schema('high', [offset_int]),
        _op_schema('low', [offset_int]),
        _op_schema('close', [offset_int]),
        _op_schema('volume', [offset_int]),
        _op_schema('body_fraction', [offset_int]),
        _op_schema('ema', [period_int]),
        _op_schema('rsi', [period_int]),
        _op_1_or_2_schema('sma'),
        _op_1_or_2_schema('atr'),
        _op_schema('highest_high', [period_int, offset_int]),
        _op_schema('lowest_low', [period_int, offset_int]),
        _op_schema('param', [{'type': 'string', 'minLength': 1, 'maxLength': 48, 'pattern': '^[A-Za-z_][A-Za-z0-9_]*$'}]),
    ]

    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        'title': 'BTC Paper Lab V3.1 — 声明式策略 v2 安全格式规范',
        'description': '与正式部署 backend/strategy_pool.py 的 validate_strategy 及 _v2_value 保持一致；跨字段关系、参数引用存在性、深度/节点数及运行语义由正式解析器与隔离回放复核。',
        'type': 'object',
        'properties': {
            'format': {'const': 'btc-lab-declarative-strategy/v2'},
            'strategy_id': {'type': 'string', 'minLength': 1, 'maxLength': 48, 'pattern': '^[A-Za-z0-9_-]+$'},
            'version': {'type': 'string', 'minLength': 1, 'maxLength': 32},
            'name': {'type': 'string', 'minLength': 1, 'maxLength': 80},
            'market': {'const': 'BTC/USD'},
            'timeframe': {'const': '15m'},
            'description': {'type': 'string', 'maxLength': 1000},
            'parameters': {
                'type': 'object',
                'maxProperties': 32,
                'additionalProperties': {'type': 'number'},
            },
            'risk': {
                'const': {
                    'initial_equity': '10000',
                    'currency': 'USD_RESEARCH',
                    'max_leverage': 5,
                    'risk_fraction': '0.02',
                }
            },
            'entry': {
                'type': 'object',
                'properties': {'long': expr_ref, 'short': expr_ref},
                'required': ['long', 'short'],
                'additionalProperties': False,
            },
            'position': {
                'type': 'object',
                'properties': {
                    'direction': {'enum': ['long', 'short', 'both']},
                    'entry_timing': {'const': 'next_open'},
                    'stop': {
                        'type': 'object',
                        'properties': {
                            'kind': {'const': 'swing_atr'},
                            'lookback': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                            'atr_period': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                            'buffer_atr': {'type': 'number', 'minimum': 0},
                            'min_distance_atr': {'type': 'number', 'exclusiveMinimum': 0},
                            'max_distance_atr': {'type': 'number', 'exclusiveMinimum': 0},
                        },
                        'required': ['kind', 'lookback', 'atr_period', 'buffer_atr', 'min_distance_atr', 'max_distance_atr'],
                        'additionalProperties': False,
                    },
                    'target_r': {'type': 'number', 'exclusiveMinimum': 0},
                    'max_hold_bars': {'type': 'integer', 'minimum': 1, 'maximum': 10000},
                    'same_bar_priority': {'const': 'stop'},
                },
                'required': ['direction', 'entry_timing', 'stop', 'target_r', 'max_hold_bars', 'same_bar_priority'],
                'additionalProperties': False,
            },
        },
        'required': [
            'format', 'strategy_id', 'version', 'name', 'market', 'timeframe',
            'description', 'parameters', 'risk', 'entry', 'position',
        ],
        'additionalProperties': False,
        '$defs': {
            'expression': {'oneOf': expr_variants},
        },
    }


SPEC_MARKDOWN = """# BTC Paper Lab V3.1｜策略开发与上传规范

**用途：** 为可扩展策略池新增声明式策略提供统一的格式、校验、保护及跟单接入规范。本规范只约束接口与运行机制，不限制交易思想，不预设具体指标或交易参数，也不修改现有 16 套策略。

**源码依据：** `backend/strategy_pool.py`（声明式解析器、纸盘执行器、缺口对账与回测）、`scripts/live_copy_v2.py`（统一确认成交触发器）、`scripts/local_dashboard.py`（策略池与纸盘控制台动态发现）。

## 1. 模板包文件说明

- `strategy.template.json`：空白策略文件模板（含空字符串与 `null` 占位符，**不可直接上传或启用**）。
- `strategy.schema.json`：机器可读 JSON Schema（Draft 2020-12），与正式 v2 解析器安全子集一致。
- `策略开发与上传规范.md`：本规范文档。
- `AI策略开发指令.md`：交给其他 AI Agent 编写合规策略的标准指令。

使用流程：解压后复制 `strategy.template.json` 为 `<strategy_id>.json`，按规范填写真实策略规则并通过校验后，在策略池点击「添加策略文件」上传（切勿上传 ZIP、Schema 或 Markdown）。上传成功后策略默认为**未启用**状态，由用户自行确认启用。

## 2. 统一策略文件结构与现有支持边界

| 顶层字段 | 规则与说明 |
|---|---|
| `format` | 固定为 `btc-lab-declarative-strategy/v2`（同时保留对历史 v1 及 PA-01..PA-10 策略文件的兼容） |
| `strategy_id` | 策略唯一 ID，1–48 字符，仅允许 ASCII 字母、数字、`-`、`_`（建议语义化命名如 `pa-my-strategy` 或 `custom-my-strategy`，不得与已有策略冲突） |
| `version` | 非空字符串，最多 32 字符，建议使用语义化版本（如 `1.0.0`）；同一策略已存在版本不可覆盖 |
| `name` | 策略中文名称，1–80 字符（注册后自动用于策略池、纸盘控制台及实盘跟单选择器显示） |
| `description` | 策略描述，最多 1000 字符 |
| `market` / `timeframe` | 当前加载器支持 `BTC/USD` / `15m` |
| `parameters` | 策略自定义参数字典：最多 32 个有限数值，键名必须为合法 Python 标识符（如 `rsi_low`, `body_min`），不预设任何固定参数 |
| `risk` | 纸盘公共研究账户约束，当前加载器固定要求 `{"initial_equity":"10000","currency":"USD_RESEARCH","max_leverage":5,"risk_fraction":"0.02"}`（此为纸盘研究账户公共限制，与 Demo/正式定额跟单最高 10× 杠杆相互独立） |
| `entry` | 必须包含 `long` 与 `short` 两棵布尔表达式树 |
| `position` | 必须包含 `direction`（`long`/`short`/`both`）、`entry_timing`（`next_open`）、`stop`（`swing_atr` 参数）、`target_r`（>0）、`max_hold_bars`（1–10000 整数）、`same_bar_priority`（`stop`） |

**格式约束**：顶层字段必须且只能包含上述 11 个键；UTF-8 JSON，禁止 `NaN`/`Infinity`；单文件最大 `64,000` 字节；表达式最大深度 `12`、最大节点数 `256`。

**当前执行器支持边界（不得假装支持未实现能力）**：
- 仅支持 `BTC/USD` `15m` 确认收盘 K 线及共享实时报价。
- `position.stop.kind` 当前仅支持 `swing_atr`；`entry_timing` 仅支持 `next_open`；`same_bar_priority` 仅支持 `stop`。
- 若交易思想需要多周期共振、自定义指标（如 MACD/布林带）、自定义动态平仓表达式、分批加仓或冷却状态机，必须在设计说明中明确列出所需引擎扩展点，而不能编造未支持的 JSON 字段。

## 3. 表达式规范与禁止项

表达式节点为有限数字或 `{"op":"运算符","args":[...]}`（禁止 Python 代码、`eval`/`exec`、动态导入或文件访问）。v2 校验器与求值器实际支持的运算符：

| 运算符 | 参数说明 |
|---|---|
| `and` / `or` | 2–16 个子表达式 |
| `not` | 1 个子表达式 |
| `gt` / `gte` / `lt` / `lte` / `eq` | 2 个子表达式 |
| `param` | 1 个字符串参数名（**必须在 `parameters` 中已定义**，上传时自动校验） |
| `open` / `high` / `low` / `close` / `volume` / `body_fraction` | 1 个整数偏移 `0–199`（`0` 为最新已收盘 K 线，禁止负数或未来数据） |
| `sma` / `atr` | `[period]` 或 `[period, offset]`（`period` 1–200，`offset` 0–199，且 `period + offset <= 200`） |
| `ema` / `rsi` | `[period]`（1–200 整数） |
| `highest_high` / `lowest_low` | `[period, offset]`（`period` 1–200，`offset` 0–199，且 `period + offset <= 200`） |

- 不使用的方向（例如仅做多策略的 `entry.short`）可填写恒假表达式 `{"op":"eq","args":[0,1]}`。
- 禁止使用 `crosses_above` / `crosses_below`（v2 求值器未实现，上传校验器将直接拒绝；可用偏移比较组合表达）。
- 禁止任何未来函数或未确认 K 线引用；多空同时触发时系统自动拒绝开仓。

## 4. 统一进入保护与退出保护机制（公共系统机制）

所有新策略自动复用系统公共行情门禁、D-COMMON-GUARD-V1 同源保护及恢复机制，**严禁**策略自建行情连接、永久锁或独立恢复逻辑：

| 运行状态 | 触发条件 | 系统处理与恢复规则 |
|---|---|---|
| **正常运行** | 已启用、行情 `LIVE`、报价 `FRESH`、账户一致 | 按策略规则处理每个自然 15m 确认周期 |
| **行情异常保护** | WS 断联、报价过期/无效、周期数据缺口 | 暂停新增开仓；保留已有持仓及止盈止损；自动重连；验证连接/报价时效/K线连续性/账本一致后自动解除普通行情保护，绝不因短暂断联永久锁死 |
| **人工暂停** | 用户主动点击暂停 | 只暂停新开仓；已有持仓继续按开仓版本的止盈止损与退出规则管理；行情恢复**不会**自动解除人工暂停 |
| **持仓/账本异常** | 持仓周期缺口（`BLOCKED_GAP_RECONCILIATION_REQUIRED`）或账本状态异常 | 暂停新开仓并进入公共对账恢复机制，明确展示原因；禁止通过删除保护记录绕过风险 |
| **重启恢复** | 进程或系统重启 | 恢复原持仓、交易 ID、开仓策略版本、止盈止损及未完成退出；去重保护，不重复下单、不补开过期历史信号 |

## 5. 统一确认成交事件与实盘跟单接入

策略池执行器在开仓（`ENTRY_NEXT_OPEN`）及平仓（`STOP`/`TARGET`/`TIME`/`STOP_QUOTE`/`TARGET_QUOTE`）时，统一写入带方向一致性校验（做多 `BUY` 开/`SELL` 平，做空 `SELL` 开/`BUY` 平）的确认成交记录，并由 `scripts/live_copy_v2.py::paper_fills()` 转换为标准 `Fill` 事件：

- 字段：`cursor`、`event_id`（含 `strategy_id` 前缀）、`trade_id`、`version`、`action`（`OPEN` / `FULL_CLOSE` / `PARTIAL_CLOSE`）、`direction`（`LONG` / `SHORT`）、`quantity`、`price`、`occurred_at`（带时区）。
- 未确认信号绝不当作已成交事件；完整平仓按剩余关联仓位执行，部分平仓按减仓比例执行。
- 符合规范的新策略上传注册后，**自动进入策略池、纸盘控制台及实盘跟单选择器**，直接复用现有 Demo 与正式实盘定额跟单引擎，无需编写专属跟单执行器。

## 6. 上传、版本更新与回滚

- **上传校验**：自动校验文件大小、JSON 合法性、必填/禁止字段、ID 与版本合法性、运算符白名单、参数引用完整性、历史窗口与 ATR 距离合法性；失败时返回明确字段及原因。
- **更新版本**：保留原账户、历史账本与历史版本记录；已有持仓继续由开仓时的策略版本管理，新版本仅对后续新开仓生效；完整保留版本差异对比、历史回测与指定版本回滚功能。
"""


AI_INSTRUCTIONS_MARKDOWN = """# AI Agent｜BTC V3.1 标准策略开发指令

你是一名量化策略开发工程师，负责将用户提供的交易思想转换为 **BTC Paper Lab V3.1 可扩展策略池** 可直接上传、校验、注册与跟单的声明式策略 JSON 文件。

## 必须先阅读的模板包文件

1. `strategy.template.json`（空白模板骨架）
2. `strategy.schema.json`（JSON Schema 机器校验规范）
3. `策略开发与上传规范.md`（支持边界、公共保护与成交事件规范）

## 严格执行准则

1. **忠实还原用户交易思想**：仔细拆解用户的入场条件、方向、自定义参数、止损止盈与持仓时限。禁止擅自修改用户交易逻辑或替用户拍脑袋编造参数；如关键条件缺失，先向用户澄清。
2. **严守现有执行器支持边界**：
   - 仅使用 `strategy.schema.json` 中声明的 v2 运算符（`and/or/not/gt/gte/lt/lte/eq/param/open/high/low/close/volume/body_fraction/sma/ema/rsi/atr/highest_high/lowest_low`）。
   - 禁止使用未实现的运算符（如 v2 中的 `crosses_above`/`crosses_below`）、禁止编写 Python 脚本、禁止添加未支持的顶层字段。
   - 若用户策略思想超出现有 v2 表达能力（如多周期、自定义指标、自定义平仓表达式、分批加仓或冷却状态机），必须**明确说明所需最小引擎扩展点**，不得生成假装可运行的无效 JSON。
3. **禁止未来函数与未确认 K 线**：所有 K 线偏移必须为 `0–199` 非负整数（仅引用已确认收盘 K 线），且满足 `period + offset <= 200`；所有 `{"op":"param","args":["name"]}` 引用必须在 `parameters` 中显式定义。
4. **复用公共保护与统一成交机制**：策略文件只定义交易规则与自定义参数；行情保护、断联重连、人工暂停、重启恢复、止损监控及确认成交事件全部由系统公共引擎处理，不得为新策略设计独立行情源或独立跟单器。
5. **交付前自检**：
   - 核对符合 `strategy.schema.json` 与 `backend/strategy_pool.py::validate_strategy()`；
   - 确认所有空白占位符和 `null` 已替换为真实有效配置（不使用方向可写 `{"op":"eq","args":[0,1]}`）；
   - 确认 `strategy_id` 唯一、`version` 合法、中文 `name` 清晰；
   - 如具备本地环境，须在**隔离策略库**完成上传、正反信号求值、多空开平仓（尤其空头退出方向 `BUY`）、暂停持仓退出、历史回测及统一 `paper_fills()` 适配验证；严禁向正式策略池写入测试数据或修改既有 16 套策略。

## 标准交付物

1. `<strategy_id>.json`：可直接通过策略池「添加策略文件」上传的完整声明式策略文件。
2. 规则对照说明：逐条对照用户交易思想、自定义参数、开仓/退出逻辑及执行边界。
3. 校验清单：明确标出 `SCHEMA_VALID`、`LOADER_VALID`、`PARAM_REFS_VALID`、`NO_LOOKAHEAD`、`ISOLATED_EXECUTION`、`UNIFIED_FILL_EVENT` 的实测或待测状态。

**核心原则：策略思想完全自由，系统格式与保护完全统一。**
"""


def build_strategy_template_files() -> dict[str, bytes]:
    """Return the 4 canonical template bundle files as UTF-8 bytes."""
    return {
        'strategy.template.json': (json.dumps(strategy_template_dict(), ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
        'strategy.schema.json': (json.dumps(strategy_schema_dict(), ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
        '策略开发与上传规范.md': SPEC_MARKDOWN.encode('utf-8'),
        'AI策略开发指令.md': AI_INSTRUCTIONS_MARKDOWN.encode('utf-8'),
    }


def export_strategy_template_zip(target_path: str | Path) -> Path:
    """Write the complete BTC_V3_策略开发模板.zip atomically to target_path."""
    dest = Path(target_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.tmp')
    files = build_strategy_template_files()
    with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in files.items():
            zf.writestr(name, payload)
    tmp.replace(dest)
    return dest
