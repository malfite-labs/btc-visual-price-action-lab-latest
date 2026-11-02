import type { SeriesMarker, Time, UTCTimestamp } from 'lightweight-charts'
import { epochMilliseconds } from './time'

export function researchAnnotations(bars:any[], result:any) {
  const lines:{price:number; title:string; color:string; details:string[]}[]=[]
  const markers:SeriesMarker<Time>[]=[]
  if (!result || result.trade_authorized !== false) return {lines,markers}
  const barMap=new Map(bars.filter(x=>x.is_closed).map(x=>[epochMilliseconds(x.open_at),x]))
  const labels:Record<string,string>={support:'支撑位',resistance:'阻力位',candidate_long:'看多',candidate_short:'看空',invalidation:'失效位'}
  for (const a of [...(Array.isArray(result.zones)?result.zones:[]),...(Array.isArray(result.candidates)?result.candidates:[])]) {
    const time=epochMilliseconds(a.candle_open_at), bar=barMap.get(time), price=Number(a.price)
    if (!bar || !Number.isFinite(price) || price<=0 || !labels[a.kind] || ![bar.open,bar.high,bar.low,bar.close].some(v=>Number(v)===price)) continue
    const color=a.kind==='invalidation'?'#edb465':'#a897ff'
    const detail=[labels[a.kind], Array.isArray(a.knowledge_ids) ? a.knowledge_ids.join('、') : '', a.candle_open_at || ''].filter(Boolean).join(' · ')
    const existing=lines.find(line=>line.price===price)
    if(existing) existing.details.push(detail)
    else lines.push({price,title:labels[a.kind],color,details:[detail]})
    if(a.kind==='candidate_long'||a.kind==='candidate_short') markers.push({time:Math.floor(time/1000) as UTCTimestamp,
      position:a.kind==='candidate_long'?'belowBar':'aboveBar', color,shape:'circle',text:labels[a.kind]})
  }
  return {lines,markers}
}
