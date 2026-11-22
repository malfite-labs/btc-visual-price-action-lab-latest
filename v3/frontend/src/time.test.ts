import { describe, expect, it } from 'vitest'
import { formatBeijing, formatBeijingDateTime, formatBeijingTick } from './time'

describe('Beijing time formatting', () => {
  it('formats UTC midnight as 08:00', () => {
    expect(formatBeijingDateTime('2026-01-02T00:00:00Z')).toBe('2026-01-02 08:00:00')
  })

  it('crosses to the next Beijing day for UTC 16:30', () => {
    expect(formatBeijingDateTime('2026-01-02T16:30:00Z')).toBe('2026-01-03 00:30:00')
  })

  it('accepts epoch seconds and milliseconds without changing the instant', () => {
    expect(formatBeijingDateTime(1767313800)).toBe('2026-01-02 08:30:00')
    expect(formatBeijingDateTime(1767313800000)).toBe('2026-01-02 08:30:00')
  })

  it('formats chart tick times with the same UTC+8 conversion', () => {
    expect(formatBeijing(1767313800, { dateStyle: 'omit' })).toBe('08:30')
  })
  it('keeps intraday ticks compact and displays Beijing dates across midnight',()=>{
    const t=Date.parse('2026-11-01T16:15:00Z')/1000
    expect(formatBeijingTick(t,3)).toBe('00:15')
    expect(formatBeijingTick(t,2)).toBe('11-02')
  })
})
