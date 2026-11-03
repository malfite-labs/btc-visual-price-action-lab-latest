import {render, screen} from '@testing-library/react'
import {describe, expect, it} from 'vitest'
import EDeskCard from './EDeskCard'

describe('E account card', () => {
  it('shows an empty formal book without inventing a position or fill', () => {
    render(<EDeskCard data={{runtime: {mode: 'AUTO_PAPER'}, formal: {status: 'READY', equity: '10000', unrealized_pnl: '0', account: {state: 'FLAT', position_json: null}, fills: []}}} driving="E-V1.1-QUANT" />)
    expect(screen.getByLabelText('憨巴E账户')).toHaveTextContent('空仓')
    expect(screen.getByLabelText('憨巴E账户')).toHaveTextContent('正式盘空仓，无持仓')
    expect(screen.getByLabelText('憨巴E账户')).toHaveTextContent('最近成交暂无')
    expect(screen.getByLabelText('憨巴E账户')).toHaveTextContent('E-V1.1-QUANT')
  })
})