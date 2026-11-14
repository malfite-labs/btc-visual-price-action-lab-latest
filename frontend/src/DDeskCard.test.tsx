import {cleanup, render, screen} from '@testing-library/react'
import {afterEach, expect, it} from 'vitest'
import DDeskCard from './DDeskCard'

afterEach(() => cleanup())
it('显示观察模式和数据不足原因，不显示虚构账户', () => {
  render(<DDeskCard data={{status: 'OBSERVE_ONLY', reason: 'DATA_NOT_READY', account: null, equity: null, decision: null}} />)
  const card = screen.getByRole('article', {name: 'D策略状态'})
  expect(card).toHaveTextContent('观察中')
  expect(card).toHaveTextContent('模拟执行尚未接入')
  expect(card).toHaveTextContent('行情数据尚未就绪')
  expect(card).not.toHaveTextContent('10,000.00')
})
it('无法访问接口时清楚显示不可用', () => {
  render(<DDeskCard data={null} />)
  expect(screen.getByRole('article', {name: 'D策略状态'})).toHaveTextContent('接口不可用')
})

it('自动纸盘就绪时显示运行，不显示观察模式', () => {
  render(<DDeskCard data={{status:'AUTO_PAPER', execution_ready:true, account:{state:'OPEN_LONG'}, equity:'9999.50'}} />)
  const card = screen.getByRole('article', {name:'D策略状态'})
  expect(card).toHaveTextContent('自动纸盘运行中')
  expect(card).toHaveTextContent('做多')
  expect(card).not.toHaveTextContent('模拟执行尚未接入')
})
it('保护异常和不一致的运行标志不会显示正常运行', () => {
  const {rerender} = render(<DDeskCard data={{status:'PROTECTION_UNCERTAIN', execution_ready:false, reason:'MARKET_STREAM_UNAVAILABLE'}} />)
  expect(screen.getByRole('article', {name:'D策略状态'})).toHaveTextContent('保护待确认')
  rerender(<DDeskCard data={{status:'AUTO_PAPER', execution_ready:false}} />)
  expect(screen.getByRole('article', {name:'D策略状态'})).not.toHaveTextContent('自动纸盘运行中')
})
