import {cleanup, render, screen} from '@testing-library/react'
import {afterEach, describe, expect, it} from 'vitest'
import EDeskCard from './EDeskCard'

afterEach(() => cleanup())

describe('E account card', () => {
  it('shows an empty formal book without inventing a position or fill', () => {
    render(<EDeskCard data={{runtime: {mode: 'AUTO_PAPER'}, formal: {status: 'READY', equity: '10000', unrealized_pnl: '0', account: {state: 'FLAT', position_json: null}, fills: []}}} driving="E-V1.1-QUANT" />)
    expect(screen.getByLabelText('憨巴E账户')).toHaveTextContent('空仓')
    expect(screen.getByLabelText('憨巴E账户')).toHaveTextContent('正式盘空仓，无持仓')
    expect(screen.getByLabelText('憨巴E账户')).toHaveTextContent('最近成交暂无')
    expect(screen.getByLabelText('憨巴E账户')).toHaveTextContent('E-V1.1-QUANT')
  })

  it('renders the accounts-tab paper row for an open formal position when equity is unvalued', () => {
    render(<EDeskCard variant="account" driving="E-V1.1-QUANT" data={{runtime: {mode: 'AUTO_PAPER'}, formal: {
      status: 'READY', equity: null, unrealized_pnl: null, realized_pnl: '0',
      valuation_status: 'UNVALUED_STALE_OR_UNAVAILABLE_QUOTE',
      account: {state: 'OPEN_LONG', cash: '9939.69', position_json: JSON.stringify({direction: 'LONG', quantity: '0.015', entry_price: '64000', stop_price: '62000', trade_id: '8716f491-2735-41a5-87bd-d86e944f1120'})},
      fills: [{fill_id: 'e1', side: 'BUY', quantity: '0.015', price: '64000', occurred_at: '2026-11-03T07:15:00Z'}],
    }}} />)
    const card = screen.getByLabelText('憨巴E账户')
    expect(screen.getByRole('heading', {name: 'E账户'})).toBeInTheDocument()
    expect(card).toHaveTextContent('持仓方向/数量：LONG / 0.015')
    expect(card).toHaveTextContent('入场/止损：64000 / 62000')
    expect(card).toHaveTextContent('持仓编号：8716f491-2735-41a5-87bd-d86e944f1120')
    expect(card).toHaveTextContent('当前权益：未估值')
    expect(card).toHaveTextContent('账户现金：$9,939.69')
    expect(card).toHaveTextContent('可用资金：未估值')
    expect(card).toHaveTextContent('正式成交 1 笔')
    expect(card).toHaveTextContent('策略：E-V1.1-QUANT')
    expect(card).not.toHaveTextContent('正式盘空仓')
    expect(card).not.toHaveTextContent('当前权益：$9,939.69')
  })
})