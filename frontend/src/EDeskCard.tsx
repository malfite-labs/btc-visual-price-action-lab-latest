import {E_COLOR, eEquityText, ePositionText, eRecentFillText} from './eDesk'

export default function EDeskCard({data, driving}: {data: any; driving?: string}) {
  const formal = data?.formal
  const ready = formal?.status === 'READY'
  const position = ePositionText(formal)
  const unrealized = !ready || formal?.unrealized_pnl == null ? '未估值' : `$${Number(formal.unrealized_pnl).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`
  return <article className="card agent" aria-label="憨巴E账户">
    <div className="cardhead"><b style={{color: E_COLOR}}>E · 憨巴</b><span>{data?.runtime?.mode || formal?.status || '未知'}</span></div>
    <div className="big">{eEquityText(formal)}<small> USD研究权益 · 憨巴正式账本</small></div>
    <div className="subgrid">
      <span>持仓状态<strong>{position}</strong></span>
      <span>未实现盈亏<strong>{unrealized}</strong></span>
      <span>最近成交<strong>{eRecentFillText(formal)}</strong></span>
      <span>驱动版本<strong>{driving || '—'}</strong></span>
    </div>
    <p className="muted">{ready && position === '空仓' ? '正式盘空仓，无持仓。' : '权益、持仓与成交只来自憨巴正式账本，不计入 ABC。'}</p>
  </article>
}
