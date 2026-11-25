export const groups = [
  ['分钟', ['1m','3m','5m','15m','30m']],
  ['小时', ['1h','2h','4h','6h','8h','12h']],
  ['日及以上', ['1d','3d','1w','1M']],
] as const
export const strategies = ['A','B','C','D','E','G', ...Array.from({length:10}, (_,i) => 'PA-'+String(i+1).padStart(2,'0'))]
export type Bounds = {open_at_ms:number; close_at_ms:number}
export type Candle = Bounds & {open:string;high:string;low:string;close:string;volume:string;is_closed:boolean}
export type DemoFill = {
  strategy_id:string;copy_config_id:string;paper_trade_id:string;order_id:string;fill_timestamp:string
  action:'OPEN'|'CLOSE';position_side:'LONG'|'SHORT';execution_side:'BUY'|'SELL'
  filled_price:string;filled_quantity:string;filled_notional_usdt:string;fee_usdt:string|null
  realized_pnl_usdt:string|null;net_pnl_before_funding_usdt:string|null
}
export type DemoPosition = {strategy_id:string;copy_config_id:string;position_side:'LONG'|'SHORT';quantity:string;price:string;unrealized_pnl:string|null}
export type DemoSnapshot = {environment:'demo';strategies:string[];items:DemoFill[];positions:DemoPosition[];errors:string[];selected_strategy?:string|null}
export type Event = {event_id:string;strategy_id:string;strategy_version?:string;trade_id?:string;occurred_at:string;position_effect:string;position_direction:string;quantity:string;price:string;reason_text?:string;realized_pnl?:string;verification_status:string;source:string;demo?:DemoFill}

export function demoEvent(fill:DemoFill):Event {
  return {event_id:`${fill.copy_config_id}:${fill.order_id}:${fill.fill_timestamp}`,
    strategy_id:fill.strategy_id,trade_id:fill.paper_trade_id,occurred_at:fill.fill_timestamp,
    position_effect:fill.action,position_direction:fill.position_side,quantity:fill.filled_quantity,
    price:fill.filled_price,verification_status:'VERIFIED',source:'BINANCE_DEMO_USDM',demo:fill}
}

export function barForTime(bars: Bounds[], time: number): number {
  let left=0, right=bars.length
  while(left<right) {
    const mid=(left+right) >>> 1
    if(bars[mid].open_at_ms<=time) left=mid+1
    else right=mid
  }
  const index=left-1
  return index>=0 && time<bars[index].close_at_ms && time>=bars[index].open_at_ms ? index : -1
}

export function eventLabel(event: Pick<Event,'strategy_id'|'position_effect'|'position_direction'>) {
  const side=event.position_direction==='LONG'?'多':'空'
  const label=event.position_effect==='PARTIAL_CLOSE'?'减仓':(event.position_effect==='OPEN'?'开':'平')+side
  return event.strategy_id+' '+label
}

export function mergeBars(previous: Candle[], incoming: Candle[]) {
  const combined=new Map(previous.map(c=>[c.open_at_ms,c]))
  incoming.forEach(c=>{
    const old=combined.get(c.open_at_ms)
    if(!old?.is_closed || c.is_closed) combined.set(c.open_at_ms,c)
  })
  return [...combined.values()].sort((a,b)=>a.open_at_ms-b.open_at_ms)
}
