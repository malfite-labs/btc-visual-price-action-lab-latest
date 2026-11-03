import {describe, expect, it} from 'vitest'
import {eDrivingVersion, eFormalFills, ePaperAccountLines, ePositionText, mergeLedgerFills, strategyLines} from './eDesk'

describe('E desk presentation', () => {
  it('shows a real empty position and does not invent fills', () => {
    expect(ePositionText({status: 'READY', account: {state: 'FLAT', position_json: null}})).toBe('空仓')
    expect(ePositionText({status: 'NOT_INITIALIZED'})).toBe('未初始化')
    expect(eFormalFills({status: 'NOT_INITIALIZED', fills: [{fill_id: 'x'}]})).toEqual([])
  })

  it('keeps E fills on the formal ledger and leaves ABC rows untouched', () => {
    const merged = mergeLedgerFills(
      [{fill_id: 'a', agent: 'A', side: 'BUY'}],
      {status: 'READY', fills: [{fill_id: 'e', agent: 'A', side: 'BUY', quantity: '0.01', price: '100'}]},
    )
    expect(merged.map(row => [row.fill_id, row.agent, row.ledger])).toEqual([
      ['a', 'A', 'ABC'],
      ['e', 'E', 'E_FORMAL'],
    ])
  })

  it('labels an AUTO_PAPER driver instead of an inactive candidate', () => {
    const view = strategyLines({
      agent: 'E', status: 'AUTO_PAPER', runtime_mode: 'AUTO_PAPER', driving_version: 'E-V1.1-QUANT',
      current_version: 'E-V1.0', archived_version: 'E-V1.0', candidate_version: 'E-V1.1-QUANT', candidate_status: 'AUTO_PAPER',
    })
    expect(view.title).toBe('E策略 · AUTO_PAPER')
    expect(view.lines.join('\n')).toContain('驱动版本：E-V1.1-QUANT')
    expect(view.lines.join('\n')).not.toContain('CANDIDATE_NOT_ACTIVE')
    expect(view.showDiff).toBe(false)
    expect(eDrivingVersion([], {runtime: {mode: 'AUTO_PAPER'}})).toBe('E-V1.1-QUANT')
  })

  it('reads an object position and does not turn ledger cash into equity', () => {
    expect(ePositionText({
      status: 'READY',
      account: {state: 'OPEN_LONG', position_json: {direction: 'LONG', quantity: '0.015', trade_id: '8716f491-2735-41a5-87bd-d86e944f1120'}},
    })).toBe('LONG / 0.015')
    const lines = ePaperAccountLines({
      status: 'READY',
      equity: null,
      unrealized_pnl: null,
      realized_pnl: '0',
      valuation_status: 'UNVALUED_STALE_OR_UNAVAILABLE_QUOTE',
      account: {
        state: 'OPEN_LONG',
        cash: '9939.69',
        position_json: JSON.stringify({direction: 'LONG', quantity: '0.015', entry_price: '64000', stop_price: '62000', trade_id: '8716f491-2735-41a5-87bd-d86e944f1120'}),
      },
      fills: Array.from({length: 5}, (_, index) => ({fill_id: `e${index}`, side: 'BUY', quantity: '0.015', price: '64000'})),
    }, 'E-V1.1-QUANT').lines.join('\n')
    expect(lines).toContain('持仓方向/数量：LONG / 0.015')
    expect(lines).toContain('入场/止损：64000 / 62000')
    expect(lines).toContain('持仓编号：8716f491-2735-41a5-87bd-d86e944f1120')
    expect(lines).toContain('当前权益：未估值')
    expect(lines).toContain('账户现金：$9,939.69')
    expect(lines).toContain('可用资金：未估值')
    expect(lines).toContain('正式成交 5 笔')
    expect(lines).not.toContain('当前权益：$9,939.69')
    expect(lines).not.toContain('当前权益：$0.00')
    expect(lines).not.toContain('空仓')
  })

  it('shows available cash only when the formal book provides it', () => {
    const valued = ePaperAccountLines({
      status: 'READY', equity: '10100.50', unrealized_pnl: '12.50', available_cash: '8000',
      account: {state: 'OPEN_LONG', cash: '9939.69', position_json: {direction: 'LONG', quantity: '0.02', entry_price: '100', stop_price: '90', trade_id: 'tid'}},
      fills: [],
    }).lines.join('\n')
    expect(valued).toContain('可用资金：$8,000.00')
    expect(valued).toContain('当前权益：$10,100.50')
    const flat = ePaperAccountLines({
      status: 'READY', equity: '10000', unrealized_pnl: '0',
      account: {state: 'FLAT', cash: '10000', position_json: null}, fills: [],
    }).lines.join('\n')
    expect(flat).toContain('持仓方向/数量：空仓')
    expect(flat).toContain('可用资金：$10,000.00')
    expect(flat).toContain('入场/止损：—')
    expect(flat).toContain('持仓编号：—')
  })
})
