import {useEffect, useRef, useState} from 'react'
import {get} from './api'
import {E_COLOR, eStatsView} from './eDesk'
import {formatBeijingDateTime} from './time'
import b40 from './b40Research.json'

const colors: Record<string, string> = {A: '#e2555b', B: '#26b37a', C: '#ed992f'}
const value = (x: any, dp = 2) => x === null || x === undefined ? '不可计算' : Number(x).toFixed(dp)
const latestBar = (bar: unknown) => {
  if (!bar) return '不可用'
  if (typeof bar === 'string' && Number.isNaN(Date.parse(bar))) return bar
  return formatBeijingDateTime(bar as string)
}

function EStatsCard({formal, open, onToggle}: {formal: any; open: boolean; onToggle: () => void}) {
  const view = eStatsView(formal)
  return <article className="card" aria-label="憨巴正式账本统计" style={{borderTop: `3px solid ${E_COLOR}`}}>
    <button aria-label="E 统计详情" onClick={onToggle} style={{color: E_COLOR}}>{view.availability === 'ready' ? `E · ${view.state}` : 'E · 憨巴正式账本'}</button>
    {view.availability === 'ready' ? <>
      <p>憨巴正式账本 · 不计入 ABC</p>
      <p>账户权益 {view.equity} · 已实现 {view.realized}</p>
      <p>闭合 {view.closedTrades} 笔 · 胜率 {view.winRate}</p>
      <p>浮动盈亏 {view.unrealized} · 正式成交 {view.fillCount} 笔</p>
      {open && <div>
        <p>现金 {view.cash} · 可用资金 {view.availableCash}</p>
        <p>持仓：{view.position}</p>
        <p>入场/止损：{view.entryStop}</p>
        <p>手续费 {view.fees} · 滑点 {view.slippage}</p>
        <p>已实现回撤：{view.drawdown}</p>
        <p>最近成交：{view.lastFill}</p>
        <p>下次复盘：第 {view.nextReview} 笔</p>
        {view.valuationNote && <p>{view.valuationNote}</p>}
        <p>不含影子前推。权益与成交只来自憨巴正式账本。</p>
      </div>}
    </> : <p>{view.availability === 'uninitialized' ? '正式账本未初始化' : view.availability === 'unavailable' ? '正式账本接口不可用' : '憨巴正式账本暂不可用'} · 不计入 ABC</p>}
  </article>
}

function AbcCard({agent, row, open, onToggle}: {agent: string; row: any; open: boolean; onToggle: () => void}) {
  const position = row.position
  return <article className="card" style={{borderTop: `3px solid ${colors[agent]}`}}>
    <button aria-label={`${agent} 统计详情`} onClick={onToggle} style={{color: colors[agent]}}>{agent} · {row.state}</button>
    <p>账户权益 ${value(row.equity)} · 已实现 ${value(row.realized_net_pnl)}</p>
    <p>闭合 {row.closed_trades ?? '不可计算'} 笔 · 已实现回撤 {row.max_drawdown_pct === null ? '不可计算' : `${value(row.max_drawdown_pct, 2)}%`}</p>
    <p>手续费 ${value(row.fees)} · 滑点 ${value(row.slippage)}</p>
    <p>影子前推 {row.shadow?.closed_since_review ?? '—'}/40</p>
    {open && <div>
      <p>现金 ${value(row.cash)} · 未实现浮动 ${value(row.unrealized_pnl)}</p>
      <p>胜率 {row.win_rate_pct === null ? '不可计算' : `${value(row.win_rate_pct)}%`} · 利润因子 {value(row.profit_factor, 4)}</p>
      <p>持仓：{position ? `${position.direction} ${position.quantity} BTC · 入场 ${position.entry_price} · 止损 ${position.stop_price}` : '空仓'}</p>
      <p>止损状态：{row.pending_stop ? '待退出' : position ? row.monitor_state || '不可确认' : '空仓'} · 杠杆 {position?.leverage || '1'}×</p>
      {agent === 'C' && position && <p>初始保证金 ${value(position.initial_margin)} · 维持保证金 ${value(row.maintenance_margin)} · 强平参考价 ${value(row.liquidation_reference_price)}</p>}
      <p>最近成交：{row.last_fill ? `${row.last_fill.position_effect} ${formatBeijingDateTime(row.last_fill.occurred_at)} · ${row.last_fill.price}` : '无'}</p>
      <p>最近决策：{row.last_decision ? `${formatBeijingDateTime(row.last_decision.candle_close)} · ${row.last_decision.action} · ${row.last_decision.outcome} · ${row.last_decision.execution?.reason || row.last_decision.reason || ''}` : '无'}</p>
      <p>影子版本信息：{row.shadow?.version || '不可用'}（含 batch 的字段仅代表候选记录；实际执行版本接口未提供） · {row.shadow?.review_status || '未知'} · 行情 {row.shadow?.feed_status || '未知'}</p>
      <p>影子最新K线：{latestBar(row.shadow?.latest_bar)} · 最近进化：{row.shadow?.latest_evolution_at ? formatBeijingDateTime(row.shadow.latest_evolution_at) : '尚无'} · 待复盘状态：{row.shadow?.review_status || '未知'}</p>
      {agent === 'C' && position && <p>强平监控：{row.monitor_state || '不可确认'}</p>}
    </div>}
  </article>
}

export default function StatisticsPanel({initial, eAccounts}: {initial?: any; eAccounts?: any}) {
  const [data, setData] = useState<any>(initial), [error, setError] = useState(false), [open, setOpen] = useState<string | null>(null)
  const pending = useRef<Promise<any> | null>(null)
  useEffect(() => {
    if (initial) return
    let active = true
    const refresh = () => {
      if (!pending.current) pending.current = get('/statistics/abc').finally(() => { pending.current = null })
      pending.current.then(x => {
        if (active && x?.agents) { setData(x); setError(false) }
        else if (active) setError(true)
      }).catch(() => { if (active) setError(true) })
    }
    refresh()
    const timer = setInterval(refresh, 12000)
    return () => { active = false; clearInterval(timer) }
  }, [initial])
  const formal = eAccounts?.formal
  if (!data?.agents && eAccounts == null && !error) return <section className="notice" aria-label="实时统计">实时统计：加载中</section>
  if (!data?.agents && eAccounts == null && error) return <section className="notice" aria-label="实时统计">实时统计：连接异常，暂无数据</section>
  const quoteAt = data?.quote?.event_at ? formatBeijingDateTime(data.quote.event_at) : '不可用'
  return <section aria-label="实时统计">
    <h2>实时统计</h2>
    {error && data?.agents && <p className="error">A/B/C 连接异常；以下是上次成功数据，非实时值。</p>}
    {!data?.agents && <p className="error">A/B/C 统计暂不可用，E 仍单独显示。</p>}
    {data?.agents
      ? <p>A/B/C 数据更新时间：{formatBeijingDateTime(data.updated_at)} · 报价时间：{quoteAt} · A/B/C 已实现回撤按闭合交易净值计算；滑点已包含在成交价中。E 来自憨巴正式账本，不并入 ABC。</p>
      : <p>E 来自憨巴正式账本，不并入 ABC。</p>}
    <div className="stats-grid">
      {data?.agents && ['A', 'B', 'C'].map(agent => data.agents[agent] ? <AbcCard key={agent} agent={agent} row={data.agents[agent]} open={open === agent} onToggle={() => setOpen(open === agent ? null : agent)} /> : null)}
      <EStatsCard formal={formal} open={open === 'E'} onToggle={() => setOpen(open === 'E' ? null : 'E')} />
    </div>
    <details>
      <summary>影子 B 首次 40 笔进化研究</summary>
      <p>研究快照：{b40.as_of}（不是实时审批状态）。原版本 V3；候选已生成、规则校验通过；快照时无批准记录，最近实际决策仍为 V3。</p>
      <p>首批 40 笔：净损益 ${b40.batch.net_pnl}，胜率 {b40.batch.win_rate}% ，利润因子 {b40.batch.profit_factor}，已实现最大回撤 {b40.batch.max_drawdown}% 。</p>
      <p>候选：24 根趋势幅度超过 1.2%、拒绝影线超过 45%、要求同向实体；止损结构窗口 4→6、最长持有 12→8 根，目标仍为 1.5R。12 根极值条件存在恒真问题。</p>
      <p>内部历史比较：原版 {b40.baseline.trades} 笔 / 净损益 ${b40.baseline.net_pnl} / PF {b40.baseline.profit_factor}；候选 {b40.candidate.trades} 笔 / 净损益 ${b40.candidate.net_pnl} / PF {b40.candidate.profit_factor}。候选样本不足，历史行情曾被研究，不能视为独立样本外。</p>
      <p>当时研究建议：暂不启用；后续实际版本须以运行决策记录为准。40 笔只是复盘触发，并非晋级。</p>
    </details>
  </section>
}
