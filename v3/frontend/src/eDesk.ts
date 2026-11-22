import {epochMilliseconds, formatBeijingDateTime} from './time'

export const E_COLOR = '#a970ed'

export const chartLegend = [
  {agent: 'A', color: '#e2555b', label: '趋势结构'},
  {agent: 'B', color: '#26b37a', label: '关键位反转'},
  {agent: 'C', color: '#ed992f', label: '用户策略'},
  {agent: 'E', color: E_COLOR, label: '憨巴'},
] as const

const money = (x: unknown) => Number(x || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})

const ACCOUNT_NOTE = '权益、持仓与成交只来自憨巴正式账本，不计入 ABC。'

function parsedPosition(formal: any): {ok: true; position: any | null} | {ok: false} {
  const raw = formal?.account?.position_json
  if (raw == null || raw === '' || raw === 'null') return {ok: true, position: null}
  if (typeof raw === 'object') return {ok: true, position: raw}
  try {
    const position = JSON.parse(raw)
    if (!position || typeof position !== 'object') return {ok: true, position: null}
    return {ok: true, position}
  } catch {
    return {ok: false}
  }
}

export function ePositionText(formal: any): string {
  if (!formal) return '接口不可用'
  if (formal.status === 'NOT_INITIALIZED') return '未初始化'
  if (formal.status !== 'READY') return '接口不可用'
  const parsed = parsedPosition(formal)
  if (!parsed.ok) return '持仓数据不可用'
  const state = formal.account?.state
  if (!parsed.position) return state && state !== 'FLAT' ? String(state) : '空仓'
  const direction = parsed.position.direction || (state && state !== 'FLAT' ? state : '')
  const quantity = parsed.position.quantity
  if (direction && quantity != null && quantity !== '') return `${direction} / ${quantity}`
  if (direction) return String(direction)
  return state && state !== 'FLAT' ? String(state) : '持仓数据不可用'
}

function eAvailableCashText(formal: any): string {
  const status = formal?.valuation_status
  const open = Boolean(formal?.account?.state && formal.account.state !== 'FLAT')
  if (status === 'UNVALUED_STALE_OR_UNAVAILABLE_QUOTE' || status === 'EXECUTABLE_SIDE_MISSING' || (open && formal?.equity == null)) return '未估值'
  if (formal?.available_cash != null && formal.available_cash !== '') return `$${money(formal.available_cash)}`
  const raw = formal?.account?.position_json
  const noPosition = raw == null || raw === '' || raw === 'null'
  if (!open && noPosition && formal?.account?.cash != null && formal.account.cash !== '') return `$${money(formal.account.cash)}`
  return '接口未提供'
}

export function ePaperAccountLines(formal: any, driving?: string): {title: string; lines: string[]; note: string} {
  const title = 'E账户'
  if (!formal || (formal.status !== 'READY' && formal.status !== 'NOT_INITIALIZED')) {
    return {title, lines: ['正式账本：接口不可用'], note: ACCOUNT_NOTE}
  }
  if (formal.status === 'NOT_INITIALIZED') return {title, lines: ['正式账本：未初始化'], note: ACCOUNT_NOTE}
  const parsed = parsedPosition(formal)
  const position = parsed.ok ? parsed.position : null
  const entryStop = !parsed.ok
    ? '持仓数据不可用'
    : position && (position.entry_price != null || position.stop_price != null)
      ? `${position.entry_price ?? '—'} / ${position.stop_price ?? '—'}`
      : '—'
  const cash = formal.account?.cash
  const realized = formal.realized_pnl == null || formal.realized_pnl === '' ? '接口未提供' : `$${money(formal.realized_pnl)}`
  const unrealized = formal.unrealized_pnl == null ? '未估值' : `$${money(formal.unrealized_pnl)}`
  const fills = eFormalFills(formal)
  const lines = [
    '计价单位：USD研究模式',
    `当前权益：${eEquityText(formal)}`,
    `初始本金：接口未提供 · 已实现盈亏：${realized}`,
    `账户现金：${cash == null || cash === '' ? '接口未提供' : `$${money(cash)}`}`,
    `可用资金：${eAvailableCashText(formal)}`,
    `持仓方向/数量：${ePositionText(formal)}`,
    `入场/止损：${entryStop}`,
    `浮动盈亏：${unrealized}`,
    `持仓编号：${position?.trade_id ? String(position.trade_id) : '—'}`,
    `最近成交：${eRecentFillText(formal)} · 正式成交 ${fills.length} 笔`,
    `策略：${driving || '—'}`,
  ]
  if (formal.valuation_status === 'UNVALUED_STALE_OR_UNAVAILABLE_QUOTE' || formal.valuation_status === 'EXECUTABLE_SIDE_MISSING') {
    lines.push('估值：报价不可用，权益与浮动盈亏未重估')
  }
  return {title, lines, note: ACCOUNT_NOTE}
}

export function eFormalFills(formal: any) {
  if (formal?.status !== 'READY' || !Array.isArray(formal.fills)) return []
  return formal.fills.filter((fill: any) => fill?.fill_id).map((fill: any) => ({...fill, agent: 'E', ledger: 'E_FORMAL'}))
}

export function mergeLedgerFills(abc: any[] | undefined, formal: any) {
  const hanba = eFormalFills(formal)
  const seen = new Set(hanba.map((fill: any) => fill.fill_id))
  const base = (abc || []).filter(fill => fill?.fill_id && fill.agent !== 'E' && !seen.has(fill.fill_id)).map(fill => ({...fill, ledger: fill.ledger || 'ABC'}))
  return [...base, ...hanba]
}

export function eDrivingVersion(strategies: any[] | undefined, data: any) {
  const row = strategies?.find(item => item.agent === 'E')
  if (row?.driving_version) return row.driving_version
  if (data?.runtime?.mode === 'AUTO_PAPER') return 'E-V1.1-QUANT'
  return row?.current_version || '—'
}

export function strategyLines(strategy: any): {title: string; lines: string[]; showDiff: boolean} {
  if (strategy?.agent === 'E' && strategy.runtime_mode === 'AUTO_PAPER' && strategy.driving_version) {
    return {
      title: `E策略 · ${strategy.runtime_mode}`,
      showDiff: false,
      lines: [
        `运行口径：${strategy.runtime_mode}`,
        `驱动版本：${strategy.driving_version}`,
        `归档版本：${strategy.archived_version || strategy.current_version || '—'}`,
        `注册状态：${strategy.candidate_status || strategy.runtime_mode}`,
        ...(strategy.status === 'INTEGRITY_ERROR' ? ['归档完整性：INTEGRITY_ERROR'] : []),
      ],
    }
  }
  return {
    title: `${strategy.agent}策略 · ${strategy.status}`,
    showDiff: strategy.agent !== 'E' && (strategy.versions?.length || 0) > 1,
    lines: [
      `现役版本：${strategy.current_version || '—'}`,
      `候选版本：${strategy.candidate_version || '—'}`,
      ...(strategy.candidate_status ? [`候选状态：${strategy.candidate_status}`] : []),
    ],
  }
}

export function eEquityText(formal: any) {
  if (!formal) return '接口不可用'
  if (formal.status === 'NOT_INITIALIZED') return '未初始化'
  if (formal.status !== 'READY') return '接口不可用'
  return formal.equity == null ? '未估值' : `$${money(formal.equity)}`
}

export function eRecentFillText(formal: any) {
  const fills = eFormalFills(formal)
  const last = fills.at(-1)
  if (!last) return '暂无'
  const when = last.occurred_at ? formatBeijingDateTime(last.occurred_at) : ''
  return `${last.side || ''} ${last.quantity ?? ''} @ ${last.price ?? ''}${when ? ` · ${when}` : ''}`.trim()
}

export function eEntryStopText(formal: any): string {
  if (!formal || formal.status !== 'READY') return '—'
  const parsed = parsedPosition(formal)
  if (!parsed.ok) return '持仓数据不可用'
  const position = parsed.position
  if (position && (position.entry_price != null || position.stop_price != null)) {
    return `${position.entry_price ?? '—'} / ${position.stop_price ?? '—'}`
  }
  return '—'
}

export type EStatsView = {
  availability: 'missing' | 'unavailable' | 'uninitialized' | 'ready'
  state: string
  equity: string
  realized: string
  unrealized: string
  closedTrades: string
  winRate: string
  position: string
  entryStop: string
  cash: string
  availableCash: string
  fillCount: string
  fees: string
  slippage: string
  lastFill: string
  nextReview: string
  drawdown: string
  valuationNote: string | null
}

const EMPTY_STATS: EStatsView = {
  availability: 'missing', state: '', equity: '', realized: '', unrealized: '', closedTrades: '', winRate: '',
  position: '', entryStop: '', cash: '', availableCash: '', fillCount: '', fees: '', slippage: '', lastFill: '',
  nextReview: '', drawdown: '接口未提供', valuationNote: null,
}

function moneyText(value: unknown, missing: string): string {
  if (value == null || value === '') return missing
  const amount = Number(value)
  return Number.isFinite(amount) ? `$${amount.toFixed(2)}` : '不可计算'
}

function sumMoney(fills: any[], field: string): string {
  if (!fills.length) return '$0.00'
  let total = 0
  for (const fill of fills) {
    const raw = fill?.[field]
    if (raw == null || raw === '') return '接口未提供'
    const amount = Number(raw)
    if (!Number.isFinite(amount)) return '不可计算'
    total += amount
  }
  return `$${total.toFixed(2)}`
}

function percentFraction(value: unknown): string {
  if (value == null || value === '') return '接口未提供'
  const amount = Number(value)
  return Number.isFinite(amount) ? `${(amount * 100).toFixed(1)}%` : '不可计算'
}

export function eStatsView(formal: any): EStatsView {
  if (!formal) return EMPTY_STATS
  if (formal.status === 'NOT_INITIALIZED') return {...EMPTY_STATS, availability: 'uninitialized'}
  if (formal.status !== 'READY') return {...EMPTY_STATS, availability: 'unavailable'}
  const fills = eFormalFills(formal)
  const stale = formal.valuation_status === 'UNVALUED_STALE_OR_UNAVAILABLE_QUOTE' || formal.valuation_status === 'EXECUTABLE_SIDE_MISSING'
  return {
    availability: 'ready',
    state: formal.account?.state || '未知',
    equity: moneyText(formal.equity, '未估值'),
    realized: moneyText(formal.realized_pnl, '接口未提供'),
    unrealized: moneyText(formal.unrealized_pnl, '未估值'),
    closedTrades: formal.closed_trades == null || formal.closed_trades === '' ? '接口未提供' : String(formal.closed_trades),
    winRate: percentFraction(formal.win_rate),
    position: ePositionText(formal),
    entryStop: eEntryStopText(formal),
    cash: moneyText(formal.account?.cash, '接口未提供'),
    availableCash: eAvailableCashText(formal),
    fillCount: String(fills.length),
    fees: sumMoney(fills, 'fee'),
    slippage: sumMoney(fills, 'slippage'),
    lastFill: eRecentFillText(formal),
    nextReview: formal.next_review_at == null || formal.next_review_at === '' ? '接口未提供' : String(formal.next_review_at),
    drawdown: '接口未提供',
    valuationNote: stale ? '估值：报价不可用，权益与浮动盈亏未重估' : null,
  }
}

export type EDecisionView = {
  badge?: [string, string, string]
  mode: string
  reason: string
  result: string
  warning: boolean
}

const FAILED_STATUS = new Set(['INTERRUPTED', 'OBSERVATION_FAILED', 'MODEL_FAILED', 'MODEL_TIMEOUT', 'MODEL_UNAVAILABLE', 'OBSERVATION_ERROR', 'STRATEGY_INTEGRITY_ERROR', 'UNAVAILABLE'])
const VISUAL_WARNING = new Set(['MODEL_FAILED', 'MODEL_TIMEOUT', 'OBSERVATION_ERROR', 'MODEL_RESPONSE_INVALID'])

export function observationCycleKey(closeAt: unknown): string {
  if (typeof closeAt !== 'string' || !closeAt) return ''
  const time = epochMilliseconds(closeAt)
  return Number.isNaN(time) ? '' : new Date(time).toISOString()
}

export function decisionCycleKey(decisionId: unknown): string {
  if (typeof decisionId !== 'string') return ''
  const match = /^E(?:-V1\.1)?:(.+)$/.exec(decisionId)
  if (!match) return ''
  return observationCycleKey(match[1])
}

function observationReady(payload: any): boolean {
  return Boolean(payload) && Array.isArray(payload.items) && payload.status !== 'UNAVAILABLE'
}

function fillsByCycle(formal: any): Record<string, any[]> {
  const grouped: Record<string, any[]> = {}
  if (formal?.status !== 'READY') return grouped
  for (const fill of eFormalFills(formal)) {
    const key = decisionCycleKey(fill.decision_id)
    if (key) (grouped[key] ||= []).push(fill)
  }
  return grouped
}

function fillResult(fills: any[]): string | null {
  if (!fills.length) return null
  const parts = fills.map(fill => {
    const effect = fill.position_effect === 'CLOSE' ? '已平仓' : fill.position_effect === 'OPEN' ? '已开仓' : (fill.position_effect || '成交')
    return `${effect} ${fill.side || ''} ${fill.quantity ?? ''} @ ${fill.price ?? ''}`.replace(/\s+/g, ' ').trim()
  })
  return `${parts.join('；')} · 憨巴正式账本 · 不计入 ABC`
}

function rationaleText(raw: unknown): string {
  if (typeof raw !== 'string' || !raw.trim()) return '正式账本有成交，观察详情未随本接口返回'
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && parsed.setup) return `结构 ${parsed.setup}`
  } catch { /* rationale is plain text */ }
  return raw.trim()
}

function quantView(item: any, fills: any[]): EDecisionView {
  const result = item?.result || {}
  const status = String(item?.status || result.status || 'UNKNOWN')
  const side = result.candidate?.side
  let badge: [string, string, string] | undefined
  if (status === 'NO_SETUP') badge = ['WAIT', '无结构', 'wait']
  else if (status === 'CONFLICT_SKIP') badge = ['WAIT', '冲突跳过', 'wait']
  else if (status === 'SETUP_DETECTED' && side === 'LONG') badge = ['LONG', '多头结构', 'long']
  else if (status === 'SETUP_DETECTED' && side === 'SHORT') badge = ['SHORT', '空头结构', 'short']
  else if (status === 'INTERRUPTED') badge = ['WAIT', '中断', 'wait']
  const parts: string[] = []
  if (result.candidate?.setup) parts.push([result.candidate.setup, side].filter(Boolean).join(' '))
  if (result.visual_status && result.visual_status !== 'NOT_REQUESTED') parts.push(`视觉 ${result.visual_status}`)
  if (result.risk_preflight?.reason) parts.push(`预检 ${result.risk_preflight.reason}`)
  let reason = parts.join(' · ')
  if (!reason) {
    if (status === 'NO_SETUP') reason = '本周期未形成结构'
    else if (status === 'CONFLICT_SKIP') reason = '多空结构冲突，本周期跳过'
    else if (result.reason) reason = String(result.reason)
    else reason = '本周期未提供详细理由'
  }
  return {
    badge,
    mode: `量化观察 · ${result.strategy_version || 'E-V1.1-QUANT'} · ${status}`,
    reason,
    result: fillResult(fills) || '本周期无正式成交 · 不计入 ABC',
    warning: FAILED_STATUS.has(status) || VISUAL_WARNING.has(result.visual_status),
  }
}

function visionView(item: any, fills: any[]): EDecisionView {
  const result = item?.result || {}
  const decision = result.decision || {}
  const status = String(item?.status || result.status || 'UNKNOWN')
  let badge: [string, string, string] | undefined
  if (decision.action === 'SKIP') badge = ['WAIT', '观望', 'wait']
  else if (decision.action === 'OPEN' && decision.side === 'LONG') badge = ['LONG', '开多', 'long']
  else if (decision.action === 'OPEN' && decision.side === 'SHORT') badge = ['SHORT', '开空', 'short']
  const reasons = Array.isArray(decision.reasons) ? decision.reasons.filter(Boolean).join('；') : ''
  return {
    badge,
    mode: `视觉观察 · ${result.strategy_version || 'E-V1.0'} · ${status}`,
    reason: reasons || result.reason || '本周期未提供详细理由',
    result: fillResult(fills) || '观察记录，不直接下单 · 不计入 ABC',
    warning: FAILED_STATUS.has(status),
  }
}

function fillView(fills: any[]): EDecisionView {
  const fill = fills[0] || {}
  const closing = fills.every(item => item.position_effect === 'CLOSE')
  const short = fill.position_direction === 'SHORT' || (fill.position_effect === 'OPEN' && fill.side === 'SELL')
  const badge: [string, string, string] = closing ? ['CLOSE', '平仓', 'close'] : short ? ['SHORT', '开空', 'short'] : ['LONG', '开多', 'long']
  return {
    badge,
    mode: `憨巴正式账本 · ${fill.strategy_version || '成交'}`,
    reason: rationaleText(fill.rationale),
    result: fillResult(fills) || '本周期无正式成交 · 不计入 ABC',
    warning: false,
  }
}

export function eDecisionIndex(observations: any, quantObservations: any, formal: any): {known: boolean; cycles: Record<string, EDecisionView>} {
  const quantReady = observationReady(quantObservations)
  const visionReady = observationReady(observations)
  const formalReady = formal?.status === 'READY' && Array.isArray(formal.fills)
  const fills = fillsByCycle(formal)
  const cycles: Record<string, EDecisionView> = {}
  if (quantReady) {
    for (const item of quantObservations.items) {
      const key = observationCycleKey(item?.close_at)
      if (key && !cycles[key]) cycles[key] = quantView(item, fills[key] || [])
    }
  }
  if (visionReady) {
    for (const item of observations.items) {
      const key = observationCycleKey(item?.close_at)
      if (key && !cycles[key]) cycles[key] = visionView(item, fills[key] || [])
    }
  }
  if (formalReady) {
    for (const [key, rows] of Object.entries(fills)) if (!cycles[key]) cycles[key] = fillView(rows)
  }
  return {known: quantReady && visionReady, cycles}
}
