import {describe,expect,it} from 'vitest'
import {quoteIsFresh,shadowReviewText,fillModelText} from './statusPresentation'

describe('status presentation',()=>{
 it('does not keep an old quote fresh just because its cached age was low',()=>{
  const now=Date.parse('2026-11-01T14:00:00Z')
  expect(quoteIsFresh({event_at:'2026-11-01T13:58:00Z',age_ms:100},'PASS',now)).toBe(false)
  expect(quoteIsFresh({event_at:'2026-11-01T13:59:50Z',age_ms:100},'PASS',now)).toBe(true)
 })
 it('distinguishes a completed review from an active candidate',()=>{
  expect(shadowReviewText([40,'COMPLETED'])).toContain('第 40 笔复盘 COMPLETED')
  expect(shadowReviewText([40,'COMPLETED'])).toContain('启用状态未提供')
 })
 it('does not claim depth is enforced for full quote-side fills',()=>{
  expect(fillModelText('QUOTE_SIDE_FULL_FILL_V1')).toContain('不按盘口数量限制')
  expect(fillModelText(undefined)).toContain('未提供')
 })
})
