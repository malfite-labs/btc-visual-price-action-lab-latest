import type { Time, SeriesMarker, UTCTimestamp } from 'lightweight-charts'
import { epochMilliseconds } from './time'

export type ClosedBar = { open_at: string; is_closed: boolean }
export type LedgerFill = {
  fill_id: string
  agent: 'A' | 'B' | 'C' | 'E'
  side: 'BUY' | 'SELL'
  position_effect: 'OPEN' | 'CLOSE'
  position_direction: 'LONG' | 'SHORT'
  occurred_at: string
  trade_id?: string | null
  quantity?: string | number
  price?: string | number
}

const colors: Record<LedgerFill['agent'], string> = {
  A: '#e2555b',
  B: '#26b37a',
  C: '#ed992f',
  E: '#a970ed',
}

export function fillBucketTime(value: string): UTCTimestamp {
  return Math.floor(epochMilliseconds(value) / 900_000) * 900 as UTCTimestamp
}

export function buildTradeMarkers(
  bars: ClosedBar[],
  fills: LedgerFill[],
): SeriesMarker<Time>[] {
  const closedTimes = new Set(
    bars.filter(bar => bar.is_closed).map(bar => Math.floor(epochMilliseconds(bar.open_at) / 1000)),
  )
  const seen = new Set<string>()
  const positions = new Map<string, number>()
  const marked: { fill: LedgerFill; partial: boolean }[] = []
  for (const fill of [...fills].sort((a,b) => epochMilliseconds(a.occurred_at)-epochMilliseconds(b.occurred_at))) {
    if (!fill.fill_id || seen.has(fill.fill_id) || !['OPEN','CLOSE'].includes(fill.position_effect)) continue
    seen.add(fill.fill_id)
    const key = fill.trade_id ? `${fill.agent}:${fill.trade_id}` : ''
    const quantity = Math.round(Number(fill.quantity) * 1e8)
    let partial = false
    if (key && Number.isSafeInteger(quantity) && quantity > 0) {
      if (fill.position_effect === 'OPEN') positions.set(key, (positions.get(key) || 0) + quantity)
      else if (positions.has(key)) { const remaining = (positions.get(key) || 0) - quantity; partial = remaining > 0; positions.set(key, remaining) }
    }
    if (closedTimes.has(fillBucketTime(fill.occurred_at)) && Number.isFinite(Number(fill.price)) && Number(fill.price)>0) marked.push({fill,partial})
  }
  return marked.map(({fill,partial}) => ({
      id: fill.fill_id,
      time: fillBucketTime(fill.occurred_at),
      position: 'atPriceMiddle',
      price: Number(fill.price),
      color: colors[fill.agent],
      shape: (fill.position_effect === 'OPEN') === (fill.position_direction === 'LONG') ? 'arrowUp' : 'arrowDown',
      size: 0.8,
      text: `${fill.agent}${fill.position_effect === 'CLOSE' ? partial ? '减' : '平' : '开'}${fill.position_direction === 'LONG' ? '多' : '空'}${partial ? ` ${fill.quantity} BTC` : ''}`,
    }))
}

