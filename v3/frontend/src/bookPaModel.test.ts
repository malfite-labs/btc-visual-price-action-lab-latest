import { describe, expect, it } from 'vitest'
import { researchAnnotations } from './bookPaModel'

describe('book research annotations',()=>{
  it('only shows anchored research annotations and never invents a price',()=>{
    const bars=[{open_at:'2026-10-30T00:00:00Z',is_closed:true,open:'100',high:'102',low:'99',close:'101'}]
    const result={trade_authorized:false, zones:[{kind:'support',price:'99',candle_open_at:bars[0].open_at}],
      candidates:[{kind:'candidate_long',price:'100',candle_open_at:bars[0].open_at},
      {kind:'candidate_short',price:'999',candle_open_at:bars[0].open_at}]}
    const out=researchAnnotations(bars,result)
    expect(out.lines).toHaveLength(2)
    expect(out.markers).toHaveLength(1)
    expect(out.lines.every(x=>x.price !== 999)).toBe(true)
    expect(researchAnnotations(bars,{...result,trade_authorized:true}).lines).toHaveLength(0)
  })
  it('groups the same price into one compact label while retaining hover details',()=>{
    const bar={open_at:'2026-10-30T00:00:00Z',is_closed:true,open:'100',high:'102',low:'99',close:'101'}
    const result={trade_authorized:false,zones:[{kind:'support',price:'99',candle_open_at:bar.open_at,knowledge_ids:['KB-S03']}],candidates:[{kind:'candidate_long',price:'99.0',candle_open_at:bar.open_at,knowledge_ids:['STR-01']}]}
    const out=researchAnnotations([bar],result)
    expect(out.lines).toHaveLength(1)
    expect(out.lines[0].title).toBe('支撑位')
    expect(out.lines[0].details).toEqual(expect.arrayContaining([expect.stringContaining('支撑位'),expect.stringContaining('看多'),expect.stringContaining('STR-01')]))
    expect(out.markers[0].text).toBe('看多')
  })
})
