export const BEIJING_TIME_ZONE = 'Asia/Shanghai'

export type DateInput = string | number | Date

function toDate(value: DateInput): Date {
  if (value instanceof Date) return value
  if (typeof value === 'number') return new Date(value < 1e12 ? value * 1000 : value)
  // Backend naive ISO timestamps represent UTC; never let the browser's zone interpret them.
  const normalized = /^\d{4}-\d{2}-\d{2}[T ]/.test(value) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(value)
    ? `${value.replace(' ', 'T')}Z`
    : value
  return new Date(normalized)
}

function parts(value: DateInput) {
  const date = toDate(value)
  if (Number.isNaN(date.getTime())) return null
  const result = new Intl.DateTimeFormat('en-GB', {
    timeZone: BEIJING_TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(date)
  return Object.fromEntries(result.map(part => [part.type, part.value]))
}

export function formatBeijingDateTime(value: DateInput): string {
  const p = parts(value)
  return p ? `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}` : '时间无效'
}

export function formatBeijingTick(value: DateInput, type: number): string {
  const p = parts(value)
  if (!p) return '时间无效'
  if (type === 0) return p.year
  if (type === 1) return `${p.year}-${p.month}`
  if (type === 2) return `${p.month}-${p.day}`
  return `${p.hour}:${p.minute}${type === 4 ? `:${p.second}` : ''}`
}

export function formatBeijing(value: DateInput, options: { dateStyle?: 'omit' | 'short'; seconds?: boolean } = {}): string {
  const p = parts(value)
  if (!p) return '时间无效'
  const clock = `${p.hour}:${p.minute}${options.seconds ? `:${p.second}` : ''}`
  return options.dateStyle === 'omit' ? clock : `${p.year}-${p.month}-${p.day} ${clock}`
}

export function epochMilliseconds(value: DateInput): number {
  return toDate(value).getTime()
}
