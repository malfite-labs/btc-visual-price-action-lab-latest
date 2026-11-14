export type ChartInterval = '15m' | '4h' | '1d' | '1w'

const FIFTEEN_MINUTES = 15 * 60_000
const DAY = 24 * 60 * 60_000
const WEEK = 7 * DAY

export const CHART_INTERVALS: { value: ChartInterval; label: string; sourceBars: number; lookbackDays: number }[] = [
  { value: '15m', label: '15分钟', sourceBars: 1, lookbackDays: 2 },
  { value: '4h', label: '4小时', sourceBars: 16, lookbackDays: 25 },
  { value: '1d', label: '1天', sourceBars: 96, lookbackDays: 150 },
  { value: '1w', label: '1周', sourceBars: 672, lookbackDays: 365 },
]

export function intervalBucketStart(timestamp: number, interval: ChartInterval): number {
  if (interval === '15m') return Math.floor(timestamp / FIFTEEN_MINUTES) * FIFTEEN_MINUTES
  if (interval === '4h') return Math.floor(timestamp / (4 * 60 * 60_000)) * (4 * 60 * 60_000)
  if (interval === '1d') return Math.floor(timestamp / DAY) * DAY
  // UTC week starts Monday. Unix epoch starts on Thursday.
  return Math.floor((timestamp + 3 * DAY) / WEEK) * WEEK - 3 * DAY
}

export function aggregateCandles(source: any[], interval: ChartInterval): any[] {
  if (interval === '15m') return source
  const definition = CHART_INTERVALS.find(item => item.value === interval)!
  const bucketDuration = interval === '4h' ? 4 * 60 * 60_000 : interval === '1d' ? DAY : WEEK
  const groups = new Map<number, any[]>()
  for (const candle of source) {
    const openedAt = Date.parse(candle.open_at)
    if (!Number.isFinite(openedAt)) continue
    const bucket = intervalBucketStart(openedAt, interval)
    const group = groups.get(bucket) || []
    group.push(candle)
    groups.set(bucket, group)
  }
  const complete: any[] = []
  for (const [bucket, group] of groups) {
    group.sort((a, b) => Date.parse(a.open_at) - Date.parse(b.open_at))
    if (group.length !== definition.sourceBars || Date.parse(group[0].open_at) !== bucket) continue
    let contiguous = true
    for (let index = 0; index < group.length; index++) {
      if (Date.parse(group[index].open_at) !== bucket + index * FIFTEEN_MINUTES) { contiguous = false; break }
    }
    if (!contiguous || bucket + bucketDuration > Date.now()) continue
    const open = Number(group[0].open), close = Number(group[group.length - 1].close)
    const high = Math.max(...group.map(bar => Number(bar.high))), low = Math.min(...group.map(bar => Number(bar.low)))
    if (![open, close, high, low].every(Number.isFinite)) continue
    complete.push({ ...group[group.length - 1], open_at: new Date(bucket).toISOString(), open, high, low, close, is_closed: true, interval })
  }
  return complete.sort((a, b) => Date.parse(a.open_at) - Date.parse(b.open_at))
}
