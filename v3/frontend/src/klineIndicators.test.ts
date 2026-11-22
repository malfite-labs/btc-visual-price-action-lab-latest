import { describe,it,expect } from 'vitest'
import { sma,ema,boll,rsi,macd } from './klineIndicators'

describe('observation indicators',()=>{
 it('does not fabricate warmup, and computes SMA and EMA seed',()=>{
  expect(sma([1,2,3,4],3)).toEqual([null,null,2,3])
  expect(ema([1,2,3,4],3)).toEqual([null,null,2,3])
 })
 it('computes population bollinger bands and flat RSI',()=>{
  expect(boll([2,2,2],3,2)[2]).toEqual({middle:2,upper:2,lower:2})
  expect(rsi([2,2,2,2],3)).toEqual([null,null,null,50])
 })
 it('MACD has no signal before sufficient observations',()=>{
  const x=macd(Array.from({length:40},(_,i)=>i+1),12,26,9)
  expect(x[24].dif).toBeNull();expect(x[32].signal).toBeNull();expect(x[33].signal).not.toBeNull()
 })
})
