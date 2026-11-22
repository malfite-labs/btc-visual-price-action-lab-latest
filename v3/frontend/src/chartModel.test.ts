import { describe, expect, it } from 'vitest'
import { buildTradeMarkers, fillBucketTime, type LedgerFill } from './chartModel'

const bars = [
  { open_at: '2026-10-27T00:00:00Z', is_closed: true },
  { open_at: '2026-10-27T00:15:00Z', is_closed: true },
]

const ledgerFill = (agent: LedgerFill['agent'], side: LedgerFill['side'], effect: LedgerFill['position_effect'], direction: LedgerFill['position_direction'], time: string): LedgerFill => ({
  agent,
  side,
  position_effect: effect,
  position_direction: direction,
  occurred_at: time,
  price: '77791.54',
  fill_id: `${agent}-${effect}-${direction}`,
})

describe('ledger fill chart markers', () => {
  it('keeps UTC and naive UTC backend candle/fill timestamps on the same instant', () => {
    expect(fillBucketTime('2026-10-28T16:30:59Z')).toBe(fillBucketTime('2026-10-28T16:30:59'))
    expect(fillBucketTime('2026-10-28T16:30:59Z')).toBe(Math.floor(Date.parse('2026-10-28T16:30:00Z') / 900000) * 900)
  })
  it('maps A red, B green and C orange fills to the matching closed candle and preserves side/effect', () => {
    const fills = [
      ledgerFill('A', 'BUY', 'OPEN', 'LONG', '2026-10-27T00:07:00Z'),
      ledgerFill('B', 'SELL', 'OPEN', 'SHORT', '2026-10-27T00:18:00Z'),
      ledgerFill('C', 'SELL', 'CLOSE', 'LONG', '2026-10-27T00:29:00Z'),
    ]
    const result = buildTradeMarkers(bars, fills)
    expect(result.map(x => [x.time, x.color, x.text, x.position])).toEqual([
      [1793059200, '#e2555b', 'A开多', 'atPriceMiddle'],
      [1793060100, '#26b37a', 'B开空', 'atPriceMiddle'],
      [1793060100, '#ed992f', 'C平多', 'atPriceMiddle'],
    ])
  })

  it('omits open candles, out-of-window fills and rows without ledger identity', () => {
    const result = buildTradeMarkers(
      [...bars, { open_at: '2026-10-27T00:30:00Z', is_closed: false }],
      [
        ledgerFill('A', 'BUY', 'OPEN', 'LONG', '2026-10-26T23:59:00Z'),
        ledgerFill('A', 'BUY', 'OPEN', 'LONG', '2026-10-27T00:30:00Z'),
        { ...ledgerFill('A', 'BUY', 'OPEN', 'LONG', '2026-10-27T00:07:00Z'), fill_id: '' },
      ],
    )
    expect(result).toEqual([])
  })
})

it('maps the real C short pair and later long to the actual candle and execution price',()=>{
 const bars=['2026-11-01T11:45:00Z','2026-11-01T13:30:00Z','2026-11-01T14:15:00Z'].map(open_at=>({open_at,is_closed:true}))
 const rows:any[]=[
  {agent:'C',side:'SELL',position_effect:'OPEN',position_direction:'SHORT',occurred_at:'2026-11-01T11:45:12Z',fill_id:'short-open',trade_id:'short',quantity:'0.65062019',price:'77791.54'},
  {agent:'C',side:'BUY',position_effect:'CLOSE',position_direction:'SHORT',occurred_at:'2026-11-01T13:33:12Z',fill_id:'short-close',trade_id:'short',quantity:'0.65062019',price:'77941.82'},
  {agent:'C',side:'BUY',position_effect:'OPEN',position_direction:'LONG',occurred_at:'2026-11-01T14:15:30Z',fill_id:'long-open',trade_id:'long',quantity:'0.64046200',price:'77906.68'},
 ]
 expect(buildTradeMarkers(bars,rows).map(x=>[x.text,x.time,x.position,(x as any).price])).toEqual([
  ['C开空',Date.parse('2026-11-01T11:45:00Z')/1000,'atPriceMiddle',77791.54],
  ['C平空',Date.parse('2026-11-01T13:30:00Z')/1000,'atPriceMiddle',77941.82],
  ['C开多',Date.parse('2026-11-01T14:15:00Z')/1000,'atPriceMiddle',77906.68],
 ])
})

it('shows partial and final closes distinctly and deduplicates fill identity',()=>{
 const a:any={...ledgerFill('B','BUY','OPEN','LONG','2026-10-27T00:07:00Z'),fill_id:'open',trade_id:'t',quantity:'2'}
 const p:any={...ledgerFill('B','SELL','CLOSE','LONG','2026-10-27T00:18:00Z'),fill_id:'partial',trade_id:'t',quantity:'0.5'}
 const f:any={...ledgerFill('B','SELL','CLOSE','LONG','2026-10-27T00:19:00Z'),fill_id:'final',trade_id:'t',quantity:'1.5'}
 expect(buildTradeMarkers(bars,[a,p,p,f]).map(x=>x.text)).toEqual(['B开多','B减多 0.5 BTC','B平多'])
})

it('keeps distinct fills on the same candle at their actual prices',()=>{
 const a:any={...ledgerFill('B','BUY','OPEN','LONG','2026-10-27T00:07:00Z'),fill_id:'b',price:'77845.91'}
 const b:any={...ledgerFill('C','SELL','OPEN','SHORT','2026-10-27T00:07:01Z'),fill_id:'c',price:'77791.54'}
 const markers=buildTradeMarkers(bars,[a,b])
 expect(markers.map(x=>[x.id,(x as any).price,x.text])).toEqual([['b',77845.91,'B开多'],['c',77791.54,'C开空']])
})

it('maps the three recorded B fills in chronological order',()=>{
 const observedBars=['2026-10-28T11:30:00Z','2026-10-28T12:00:00Z','2026-10-28T12:30:00Z'].map(open_at=>({open_at,is_closed:true}))
 const rows:any[]=[{agent:'B',fill_id:'afd3a525-3190-469f-bbdc-a232c8bb7906',trade_id:'8486de74-18e6-4851-8866-264177fb8d58',side:'BUY',position_effect:'OPEN',position_direction:'LONG',quantity:'0.12277120',occurred_at:'2026-10-28T11:30:57.820147Z'}, {agent:'B',fill_id:'4ab9666c-4e49-4b50-94ca-9e59c7cc981a',trade_id:'8486de74-18e6-4851-8866-264177fb8d58',side:'SELL',position_effect:'CLOSE',position_direction:'LONG',quantity:'0.12277120',occurred_at:'2026-10-28T12:12:05.261813Z'}, {agent:'B',fill_id:'81a23ad5-f386-4561-b3e3-29e0456e6335',trade_id:'a45e07fc-fa89-43f1-9b38-5691d08919b2',side:'BUY',position_effect:'OPEN',position_direction:'LONG',quantity:'0.12277443',occurred_at:'2026-10-28T12:30:26.458863Z'}]
 expect(buildTradeMarkers(observedBars,rows.map(row=>({...row,price:'81000'}))).map(x=>x.text)).toEqual(['B开多','B平多','B开多'])
})

it('uses actual fill id and execution side for directional marker selection',()=>{
 const open:any={...ledgerFill('B','BUY','OPEN','LONG','2026-10-27T00:07:00Z'),fill_id:'real-open',trade_id:'trade',quantity:'1'}
 const close:any={...ledgerFill('B','SELL','CLOSE','LONG','2026-10-27T00:18:00Z'),fill_id:'real-close',trade_id:'trade',quantity:'1'}
 const m=buildTradeMarkers(bars,[open,close]);expect(m.map(x=>[x.id,x.shape,x.text])).toEqual([['real-open','arrowUp','B开多'],['real-close','arrowDown','B平多']])
})

it('renders E formal fills in purple at their actual execution price',()=>{
 const fill={agent:'E',side:'BUY',position_effect:'OPEN',position_direction:'LONG',occurred_at:'2026-10-27T00:07:00Z',price:'123',fill_id:'e-open'} as any
 expect(buildTradeMarkers(bars,[fill])).toMatchObject([{color:'#a970ed',price:123,text:'E开多'}])
})
