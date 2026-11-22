import { describe,it,expect } from 'vitest'
import { barForTime, eventLabel } from './klineModel'
const bars=[{open_at_ms:1000,close_at_ms:4000},{open_at_ms:4000,close_at_ms:7000}]
describe('trusted trade mapping',()=>{
 it('uses actual candle boundaries, not fixed 15-minute buckets',()=>{
  expect(barForTime(bars,3999)).toBe(0);expect(barForTime(bars,4000)).toBe(1);expect(barForTime(bars,7000)).toBe(-1)
 })
 it('distinguishes opening and closing positions',()=>{
  expect(eventLabel({strategy_id:'D',position_effect:'PARTIAL_CLOSE',position_direction:'LONG'})).toBe('D 减仓')
  expect(eventLabel({strategy_id:'G',position_effect:'CLOSE',position_direction:'SHORT'})).toBe('G 平空')
 })
})
