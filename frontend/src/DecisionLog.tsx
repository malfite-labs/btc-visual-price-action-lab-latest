import { useMemo, useState } from 'react'
import { epochMilliseconds, formatBeijing } from './time'

type Row = { agent:string; candle_close:string; action?:string|null; outcome?:string; mode?:string; md_version?:string; rationale?:string|null; raw_response?:any; fills?:any[] }
type Props = { items:Row[]; agents:any[]; cycleEvents:Record<string, any> }
const traders = ['A','B','C'] as const
const names:Record<string,string> = {A:'趋势结构交易员', B:'关键位置反转交易员', C:'Tony 风格交易员'}
const actionMap:Record<string,[string,string,string]> = {
  OPEN_LONG:['LONG','开多','long'], LONG:['LONG','开多','long'],
  OPEN_SHORT:['SHORT','开空','short'], SHORT:['SHORT','开空','short'],
  WAIT:['WAIT','观望','wait'], HOLD:['HOLD','持有','wait'],
  CLOSE_LONG:['CLOSE','平仓','close'], CLOSE_SHORT:['CLOSE','平仓','close'], CLOSE:['CLOSE','平仓','close']
}
const keyOf = (value:string) => { const time = epochMilliseconds(value); return Number.isNaN(time) ? '' : new Date(time).toISOString() }
const beijing = (value:string) => formatBeijing(value)
const shortReason = (reason?:string|null) => {
  if (!reason?.trim()) return '本周期未提供详细理由'
  const sentences = reason.trim().split(/(?<=[。！？])|(?<=[.!?])\s+/)
  return sentences.slice(0,2).join(' ').trim() || reason.trim()
}
const rejection:Record<string,string> = {
 TARGET_OR_OBSTACLE_REJECTED:'目标价、成本空间或结构障碍条件未通过；历史拒单详情不可用',
 INVALID_TARGET_PRICE:'目标价无效', TARGET_CANNOT_COVER_COSTS:'目标收益不足以覆盖手续费与滑点', STRUCTURE_OBSTACLE_BEFORE_TARGET:'止盈前存在结构障碍',
 STOP_ON_WRONG_SIDE:'止损方向无效', STOP_WRONG_SIDE_OF_ACTUAL_FILL:'模拟成交价对应的止损方向无效',
 RISK_LIMIT_AFTER_ROUNDING:'预计亏损超过风险上限', INVALID_LEVERAGE:'杠杆超过允许范围',
 LEVERAGE_OR_CASH_LIMIT:'名义仓位或可用保证金不足', ONE_X_OR_RISK_LIMIT:'名义仓位、保证金或风险限制未通过',
 QUOTE_OR_BOOK_STALE:'报价或盘口超过60秒或时间无效', BOOK_UNAVAILABLE:'盘口不可用', DEPTH_OR_POST_FILL_RISK_LIMIT:'盘口深度或成交后风险检查未通过',
 BELOW_MINIMUM:'可执行数量不足或低于最小成交额', INVALID_CONTEXT:'行情或账户上下文无效', SOURCE_MISMATCH:'行情来源不匹配'
}

export default function DecisionLog({items, agents, cycleEvents}:Props) {
  const [limit,setLimit] = useState(10)
  const periods = useMemo(() => {
    const grouped:Record<string,Record<string,Row>> = {}
    for (const row of items) {
      const key=keyOf(row.candle_close)
      if (key && traders.includes(row.agent as typeof traders[number])) (grouped[key] ||= {})[row.agent] = row
    }
    for (const key of Object.keys(cycleEvents)) if (keyOf(key)) grouped[keyOf(key)] ||= {}
    return Object.entries(grouped).sort(([a],[b])=>b.localeCompare(a))
  },[items,cycleEvents])
  const latest = useMemo(() => Object.fromEntries(traders.map(agent => [agent, periods.find(([,rows])=>rows[agent])?.[0]])),[periods])
  const resultFor = (row:Row, key:string) => {
    const event=cycleEvents[key]?.[row.agent]
    const execution=event?.execution || row.raw_response?.execution
    if (execution?.accepted && execution?.trade_id && row.action?.startsWith('CLOSE')) return {text:'已平仓',warning:false}
    if (execution?.accepted === false) return {text:`风控拒绝或执行失败：${execution.reason ? `${rejection[execution.reason] || '其他已记录拒单原因'}（${execution.reason}）` : '历史拒单详情不可用'}`,warning:true}
    if (row.outcome && row.outcome !== 'VALID') return {text:row.outcome==='RISK_REJECTED'?'决策未执行：历史拒单详情不可用':`决策未执行：${row.outcome}`,warning:true}
    const fill=row.fills?.find(f=>f.position_effect==='OPEN' || f.position_effect==='CLOSE')
    if (fill) return {text:fill.position_effect==='OPEN'?'已开仓':'已平仓',warning:false}
    if (row.action === 'WAIT' || row.action === 'HOLD') {
      if (latest[row.agent] !== key) return {text:'观望；历史持仓状态未记录',warning:false}
      const state=agents.find(a=>a.agent===row.agent)?.account?.state
      if (state==='OPEN_LONG') return {text:'观望，继续持仓（当前多仓）',warning:false}
      if (state==='OPEN_SHORT') return {text:'观望，继续持仓（当前空仓）',warning:false}
      if (state==='FLAT') return {text:'观望，当前空仓',warning:false}
      return {text:'观望；当前持仓状态不可用',warning:false}
    }
    return {text:execution?.reason ? `执行状态：${execution.reason}` : '执行结果未记录',warning:false}
  }
  return <section className="decision-log" aria-label="交易员实时决策日志">
    <div className="decision-log-head"><div><h2>交易员实时决策日志</h2><p>真实研究记录 · 北京时间 UTC+8</p></div><span>最近周期优先</span></div>
    {!periods.length ? <div className="empty">暂无决策记录</div> : periods.slice(0,limit).map(([key,rows])=><article className="decision-cycle" key={key}>
      <h3>{beijing(key)} <small>UTC+8 · 15分钟收盘</small></h3>
      <div className="decision-cards">{traders.map(agent=>{
        const row=rows[agent], event=cycleEvents[key]?.[agent]
        const action=row?.action ? actionMap[row.action] : undefined
        const execution=row ? resultFor(row,key) : null
        return <div className="decision-card" key={agent}>
          <div className="decision-card-head"><strong>{agent} · {names[agent]}</strong>{action && <span className={`decision-action ${action[2]}`}>{action[0]} · {action[1]}</span>}</div>
          {row ? <><small>{row.mode === 'RULE_DRIVEN' ? `RULE_DRIVEN · ${row.md_version || '—'}` : `${row.mode || '历史决策'} · ${row.md_version || '—'}`}</small><p className="decision-reason">{shortReason(row.rationale)}</p><p className={execution?.warning?'decision-result warning':'decision-result'}>{execution?.text}</p></> : <p className="decision-missing">{event?.status ? `尚无决策记录 · ${event.status}` : '尚无决策记录'}</p>}
        </div>
      })}</div>
    </article>)}
    {periods.length>limit && <button className="decision-more" onClick={()=>setLimit(limit+10)}>查看更多历史周期</button>}
  </section>
}
