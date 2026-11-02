# STRATEGY_REGISTRY — 策略版本、逻辑与状态
generated_at: 2026-11-03 00:25 Asia/Shanghai
evidence_policy: 每条结论标注 [代码确认]/[运行验证]/[历史记录未复核]/[不确定/未实现]；运行状态引用前须重新核查实时数据；文档与代码冲突以本轮核实的代码与运行证据为准。本文件只读整理，未改任何代码/配置/策略。

## 1. 两套版本口径（注意区分）
- 运行实际使用：规则档 `config/rule_v26/profile.json` name=RULE_DRIVEN_V26 [代码确认][运行验证: paper_gate_reason=RULE_PROFILE_READY]
- `/api/v1/strategies` 显示的是视觉/MD 种子版本（strategies/*/v1.0.0.md），A/B=SEED_VERSION_MODEL_BLOCKED，C=ACTIVE core_lock PROTECTED [运行验证]。视觉模型调用已禁用，故正式盘由规则档驱动。

## 2. A/B/C（profile.json）[代码确认]
| 策略 | 版本 | 标签 | 文件 | risk_fraction | max_leverage | 目标/持有 | 成本覆盖 |
|---|---|---|---|---|---|---|---|
| A | V2.5 | 突破回踩 | A-rule.json | 0.02 | 1 | 见 A-rule.json | require_target_cover_costs=true |
| B | V3.0 | 反转 | v3-signals.py | 0.02 | 1 | target_r 1.5, max_hold 12 | true |
| C | V3.1 | Tony裸K研究近似（无结构障碍否决） | v3-signals.py | 0.02 | 5 | target_r 2, max_hold 12 | false |
- 详细入场/出场逻辑以 A-rule.json 与 v3-signals.py 源码为准；本文未逐行展开 → 策略研究官需直接读码 [不确定/未展开]
- B 影子成交 rationale 示例 "B_FALSE_BREAK" [运行验证:影子库]
- 回滚档: config/rule_v26/profile-v30-archive.json，scripts/rollback_rule_v26.ps1（Windows）[代码确认]
- baseline-md + baseline-md-hashes.json 为冻结基线哈希 [代码确认]

## 3. E 策略 [代码确认][运行验证]
- 当前 E-V1.0（READY_FOR_VALIDATION）；候选 E-V1.1-QUANT（CANDIDATE_NOT_ACTIVE），core_lock PROTECTED
- 文件: strategies/E/{E-V1.0.md,E-V1.1-QUANT.md,E-V1.1-STRUCTURE-ADDENDUM.md,E-shadow-parameter-whitelist.json,HANBA_SEED_V1.md}
- 运行: mode=AUTO_PAPER，setups E1–E4=true，shadow_execution=BASELINE；正式/影子均 FLAT 0 笔，next_review_at=40 [运行验证]
- 接口 /api/v1/e/*；后端 e_*.py

## 4. 当前状态（本轮 2026-11-03 00:2x 上海）[运行验证，引用前须重查]
- 正式 A FLAT cash 9976.69；B OPEN_SHORT 0.11665220@77735.84 stop 78115.79；C FLAT cash 9875.14；E FLAT 10000
- 影子 A/B/C 均 FLAT（cash 9973.97/9888.58/9498.13），worker HALTED_POSITION_GAP

## 5. BOOK 与策略关系
BOOK 为隔离研究，不直接驱动正式下单 [代码确认: book_pa_research 文首 "research only"]；详见 TRADING_EXECUTION §6。
