import {formatBeijingDateTime} from './time'

const display = (value: unknown) => value == null ? '—' : String(value)
const accountState = (value: unknown) => ({FLAT: '空仓', OPEN_LONG: '做多', OPEN_SHORT: '做空'}[String(value)] || display(value))
const reasonText = (value: unknown) => ({EXECUTION_NOT_IMPLEMENTED: '模拟执行尚未接入', DATA_NOT_READY: '行情数据尚未就绪', LATEST_CANDLE_MISSING: '缺少最近收盘 K 线', D_STORE_READ_ERROR: 'D 账本读取失败', MARKET_STREAM_UNAVAILABLE: '实时行情不可用'}[String(value)] || display(value))
const statusText = (value: unknown) => ({OBSERVE_ONLY: '观察中', AUTO_PAPER: '自动纸盘运行中', PROTECTION_UNCERTAIN: '保护待确认', DATA_NOT_READY: '数据未就绪', NO_SETUP: '无交易形态', WAIT: '等待', UNAVAILABLE: '不可用', ERROR: '异常'}[String(value)] || display(value))

export default function DDeskCard({data}: {data: any}) {
  const available = data != null
  const decision = data?.decision
  const state = data?.status === 'AUTO_PAPER' && data?.execution_ready !== true ? 'PROTECTION_UNCERTAIN' : data?.status
  const mode = state === 'AUTO_PAPER' ? '自动纸盘已启用。' : state === 'OBSERVE_ONLY' ? '观察模式，模拟执行尚未接入。' : state === 'PROTECTION_UNCERTAIN' ? '保护状态待确认，请检查运行原因。' : 'D 当前不可用，请检查运行状态。'
  const amount = data?.equity == null ? '—' : Number(data.equity).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})
  return <article className="card agent" aria-label="D策略状态">
    <div className="cardhead"><b style={{color: '#58bde0'}}>D · 急跌反转</b><span>{available ? statusText(state) : '接口不可用'}</span></div>
    <div className="big">{amount}<small> USD研究权益</small></div>
    <div className="subgrid">
      <span>持仓状态<strong>{accountState(data?.account?.state)}</strong></span>
      <span>未实现盈亏<strong>{display(data?.unrealized_pnl)}</strong></span>
      <span>最近决策<strong>{statusText(decision?.action)}</strong></span>
      <span>最近周期<strong>{decision?.candle_close ? formatBeijingDateTime(decision.candle_close) : '—'}</strong></span>
    </div>
    <p className="muted">{available ? mode : 'D 状态接口不可用，请检查后端。'}</p>
    {data?.reason && <p className="muted">运行状态：{reasonText(data.reason)}</p>}
    {decision?.reason && <p className="muted">决策原因：{reasonText(decision.reason)}</p>}
  </article>
}
