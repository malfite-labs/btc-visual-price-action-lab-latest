import {describe, expect, it} from 'vitest'
import {eDrivingVersion, eFormalFills, ePositionText, mergeLedgerFills, strategyLines} from './eDesk'

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
})
