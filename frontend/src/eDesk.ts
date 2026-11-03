import {formatBeijingDateTime} from './time'

export const E_COLOR = '#a970ed'

export const chartLegend = [
  {agent: 'A', color: '#e2555b', label: '趋势结构'},
  {agent: 'B', color: '#26b37a', label: '关键位反转'},
  {agent: 'C', color: '#ed992f', label: '用户策略'},
  {agent: 'E', color: E_COLOR, label: '憨巴'},
] as const

const money = (x: unknown) => Number(x || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})

export function ePositionText(formal: any): string {
  if (!formal) return '接口不可用'
  if (formal.status === 'NOT_INITIALIZED') return '未初始化'
  if (formal.status !== 'READY') return '接口不可用'
  const raw = formal.account?.position_json
  if (raw == null || raw === '' || raw === 'null') return formal.account?.state && formal.account.state !== 'FLAT' ? String(formal.account.state) : '空仓'
  try {
    const position = JSON.parse(raw)
    if (!position) return '空仓'
    return `${position.direction} / ${position.quantity}`
  } catch {
    return '持仓数据不可用'
  }
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
