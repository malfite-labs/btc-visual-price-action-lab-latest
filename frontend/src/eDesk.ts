import {formatBeijingDateTime} from './time'

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
