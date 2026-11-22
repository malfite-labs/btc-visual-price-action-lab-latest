import { useEffect, useMemo, useRef, useState } from 'react'
import { CandlestickSeries, ColorType, createChart, createSeriesMarkers, type CandlestickData, type UTCTimestamp } from 'lightweight-charts'
import { buildTradeMarkers, fillBucketTime, type LedgerFill } from './chartModel'
import { get, getBookPaResearch } from './api'
import { epochMilliseconds, formatBeijing, formatBeijingTick } from './time'
import { researchAnnotations } from './bookPaModel'
import { aggregateCandles, CHART_INTERVALS, intervalBucketStart, type ChartInterval } from './chartIntervals'

const colors:Record<string,string>={A:'#e2555b',B:'#26b37a',C:'#ed992f',E:'#a970ed'}
const label=(f:any)=>`${f.agent} ${f.position_effect==='OPEN'?'开':'平'}${f.position_direction==='LONG'?'多':'空'}`
const beijing=(s:string)=>formatBeijing(s,{seconds:true})
export default function Chart({items,fills,focusFill}:{items:any[];fills:any[];focusFill?:any}) {
 const host=useRef<HTMLDivElement>(null), chartRef=useRef<any>(null), seriesRef=useRef<any>(null), markersRef=useRef<any>(null), fillRef=useRef<any[]>([]), fitted=useRef(false), pendingFocus=useRef<any>(null)
 const researchLinesRef=useRef<ReturnType<typeof researchAnnotations>['lines']>([]), historyLoads=useRef(new Map<string,Promise<boolean>>()), allBarsRef=useRef<any[]>([]), olderLoading=useRef(false), olderLoader=useRef<()=>void>(()=>{}), lastView=useRef('')
 const [chartInterval,setChartInterval]=useState<ChartInterval>('15m'),[visibleCount,setVisibleCount]=useState(150),[selected,setSelected]=useState<any>(null),[sameBar,setSameBar]=useState<any[]>([]),[message,setMessage]=useState(''),[archived,setArchived]=useState<any[]>([]),[history,setHistory]=useState<any[]>([]),[date,setDate]=useState('')
 const [loadingFocus,setLoadingFocus]=useState(false)
 const [bookEnabled,setBookEnabled]=useState(false),[bookResearch,setBookResearch]=useState<any>(null),[bookStatus,setBookStatus]=useState('NO_RESEARCH_RESULTS')
 useEffect(()=>{if(!bookEnabled)return;let cancelled=false;const refresh=async()=>{try{const value=await getBookPaResearch();if(!cancelled){setBookStatus(value.status);setBookResearch(value.item||null)}}catch{if(!cancelled){setBookStatus('RESEARCH_API_UNAVAILABLE');setBookResearch(null)}}};void refresh();const timer=setInterval(refresh,30000);return()=>{cancelled=true;clearInterval(timer)}},[bookEnabled])
 const attempted=useRef(new Set<string>())
 useEffect(()=>{
  const element=host.current
  if(!element)return
  const move=(event:MouseEvent)=>{
   const bounds=element.getBoundingClientRect()
   if(event.clientX<bounds.right-150){element.title='';return}
   const y=event.clientY-bounds.top
   const match=researchLinesRef.current.find(line=>{
    const coordinate=seriesRef.current?.priceToCoordinate(line.price)
    return coordinate!=null&&Math.abs(coordinate-y)<=8
   })
   element.title=match?match.details.join('\n'):''
  }
  const leave=()=>{element.title=''}
  element.addEventListener('mousemove',move);element.addEventListener('mouseleave',leave)
  return ()=>{element.removeEventListener('mousemove',move);element.removeEventListener('mouseleave',leave)}
 },[])
 useEffect(()=>{fetch('/research/formal-trade-candles.json').then(r=>r.ok?r.json():null).then(data=>{if(data?.source==='ALPACA_US_BTCUSD_SPOT'&&data?.currency==='USD_RESEARCH'&&Array.isArray(data.items))setArchived(data.items.filter((x:any)=>x.is_closed&&x.source==='ALPACA_US_BTCUSD_SPOT'))}).catch(()=>{})},[])
 const allBars=useMemo(()=>{const merged=new Map<number,any>(),oldestAllowed=Date.now()-365*86400000;for(const x of [...archived,...history,...items]){const t=epochMilliseconds(x.open_at);if(x.is_closed&&Number.isFinite(t)&&t>=oldestAllowed)merged.set(t,x)}return [...merged.entries()].sort(([a],[b])=>a-b).map(([,x])=>x)},[items,archived,history])
 allBarsRef.current=allBars
 const intervalBars=useMemo(()=>aggregateCandles(allBars,chartInterval),[allBars,chartInterval])
 const bars=intervalBars
 const validFills=useMemo(()=>{const seen=new Set<string>();return fills.filter(f=>{if(!f.fill_id||seen.has(f.fill_id)||!['A','B','C','E'].includes(f.agent)||!['BUY','SELL'].includes(f.side)||!['OPEN','CLOSE'].includes(f.position_effect)||!['LONG','SHORT'].includes(f.position_direction))return false;seen.add(f.fill_id);return true})},[fills])
 const archiveDays=useRef(new Set<number>())
 useEffect(()=>{
  const available=new Set(allBars.map(x=>Math.floor(epochMilliseconds(x.open_at)/1000)))
  const today=Math.floor(Date.now()/86400000)*86400000,oldestAllowed=Date.now()-365*86400000
  const days=new Set(validFills.map(f=>fillBucketTime(f.occurred_at)*1000).filter(t=>Number.isFinite(t)&&t>=oldestAllowed&&t<today&&!available.has(t/1000)).map(t=>Math.floor(t/86400000)*86400000))
  for(const day of days){
   if(archiveDays.current.has(day))continue
   archiveDays.current.add(day)
   const q=new URLSearchParams({start:new Date(day).toISOString(),end:new Date(day+86400000).toISOString(),backfill:'false'})
   void get(`/market/candles/history?${q}`).then(data=>{if(data.source==='ALPACA_US_BTCUSD_SPOT'&&Array.isArray(data.items))setHistory(previous=>[...previous,...data.items])}).catch(()=>setMessage('部分成交对应的历史归档行情读取失败。'))
  }
 },[allBars,validFills])
 fillRef.current=validFills
 useEffect(()=>{
  if(!host.current)return
  const chart=createChart(host.current,{height:490,layout:{background:{type:ColorType.Solid,color:'#10161f'},textColor:'#aeb9c8'},grid:{vertLines:{color:'#202a37'},horzLines:{color:'#202a37'}},timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#27313e',tickMarkFormatter:(time:any,type:any)=>typeof time==='number'?formatBeijingTick(time,type):String(time)},localization:{timeFormatter:(time:any)=>typeof time==='number'?formatBeijing(time,{seconds:true}):String(time)},rightPriceScale:{borderColor:'#27313e'},crosshair:{mode:0}})
  const series=chart.addSeries(CandlestickSeries,{upColor:'#26b37a',downColor:'#e2555b',borderVisible:false,wickUpColor:'#26b37a',wickDownColor:'#e2555b'})
  chartRef.current=chart;seriesRef.current=series;markersRef.current=createSeriesMarkers(series,[])
  chart.subscribeClick(param=>{const id=param.hoveredObjectId;const exact=fillRef.current.find(f=>f.fill_id===id);if(exact){setSelected(exact);setSameBar(fillRef.current.filter(f=>fillBucketTime(f.occurred_at)===fillBucketTime(exact.occurred_at)));return}if(typeof param.time!=='number')return;const matches=fillRef.current.filter(f=>fillBucketTime(f.occurred_at)===param.time);setSameBar(matches);if(matches.length===1)setSelected(matches[0])})
  const observer=new ResizeObserver(()=>host.current&&chart.applyOptions({width:host.current.clientWidth}));observer.observe(host.current)
  const onRange=(range:any)=>{if(range&&range.from<=12)olderLoader.current()};chart.timeScale().subscribeVisibleLogicalRangeChange(onRange)
  return ()=>{observer.disconnect();chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);chart.remove();chartRef.current=null;seriesRef.current=null;markersRef.current=null;fitted.current=false}
 },[])
 useEffect(()=>{
  if(!seriesRef.current)return
  const clean=bars.map(x=>({time:Math.floor(epochMilliseconds(x.open_at)/1000) as UTCTimestamp,open:Number(x.open),high:Number(x.high),low:Number(x.low),close:Number(x.close)})) as CandlestickData<UTCTimestamp>[]
  const research=researchAnnotations(bars,bookEnabled?bookResearch:null)
  researchLinesRef.current=research.lines
  const markers=[...buildTradeMarkers(bars,validFills as LedgerFill[]),...research.markers].sort((a,b)=>Number(a.time)-Number(b.time))
  const timeScale=chartRef.current.timeScale(),previousRange=timeScale.getVisibleRange(),viewKey=`${chartInterval}:${visibleCount}`
  seriesRef.current.setData(clean);markersRef.current.setMarkers(markers)
  if(clean.length){if(!fitted.current||lastView.current!==viewKey){timeScale.setVisibleLogicalRange({from:Math.max(0,clean.length-visibleCount),to:clean.length-1});fitted.current=true;lastView.current=viewKey}else if(previousRange){try{timeScale.setVisibleRange(previousRange)}catch{}}}
  const lines=research.lines.map(line=>seriesRef.current.createPriceLine({price:line.price,color:line.color,lineWidth:1,lineStyle:2,axisLabelVisible:true,title:line.title}))
  return ()=>{researchLinesRef.current=[];if(host.current)host.current.title='';if(seriesRef.current)for(const line of lines)seriesRef.current.removePriceLine(line)}
 },[bars,validFills,bookResearch,bookEnabled,chartInterval,visibleCount])
 async function loadHistory(start:number,end:number){
  const latest=Math.floor(Date.now()/900000)*900000,oldestAllowed=latest-365*86400000;start=Math.max(oldestAllowed,Math.ceil(start/900000)*900000);end=Math.min(end,latest)
  if(!Number.isFinite(start)||start>=end){setMessage('该范围没有可用的已收盘行情；图表最多浏览最近一年。');return false}
  let any=false,missing=false
  try{for(let cursor=start;cursor<end;){const finish=Math.min(end,cursor+30*86400000),key=`${cursor}:${finish}`;let request=historyLoads.current.get(key);if(!request){const q=new URLSearchParams({start:new Date(cursor).toISOString(),end:new Date(finish).toISOString(),backfill:'true'});request=get(`/market/candles/history?${q}`).then(data=>{if(!Array.isArray(data.items)||data.source!=='ALPACA_US_BTCUSD_SPOT'||data.currency!=='USD_RESEARCH'||data.backfill_error)throw new Error('INVALID_HISTORY');setHistory(previous=>[...previous,...data.items]);if(data.missing)missing=true;return data.items.length>0}).catch(()=>{historyLoads.current.delete(key);throw new Error('HISTORY_QUERY_FAILED')});historyLoads.current.set(key,request)}any=await request||any;cursor=finish}setMessage(missing?'所选范围存在缺失的真实 K 线；缺失区间不会插值。':'');return any}catch{setMessage('历史行情查询失败；未虚构 K 线。');return false}
 }
 function loadOlder(){
  if(olderLoading.current)return;const source=allBarsRef.current,first=source[0];if(!first)return
  const oldest=epochMilliseconds(first.open_at),end=Math.floor(oldest/900000)*900000,earliest=Math.floor(Date.now()/900000)*900000-365*86400000
  if(end<=earliest)return
  olderLoading.current=true;void loadHistory(Math.max(earliest,end-30*86400000),end).then(ok=>{if(!ok&&end>earliest)setMessage('更早的历史行情暂不可用；已保留当前图表数据。')}).finally(()=>{olderLoading.current=false})
 }
 olderLoader.current=loadOlder
 function chooseInterval(value:ChartInterval){
  setChartInterval(value);const definition=CHART_INTERVALS.find(item=>item.value===value)!,end=Math.floor(Date.now()/900000)*900000
  void loadHistory(end-definition.lookbackDays*86400000,end)
 }
 function locate(f:any){
  if(!f)return;setSelected(f);setSameBar([])
  const target=intervalBucketStart(fillBucketTime(f.occurred_at)*1000,chartInterval)/1000;const index=bars.findIndex(x=>Math.floor(epochMilliseconds(x.open_at)/1000)===target)
  if(index<0){if(attempted.current.has(f.fill_id)){if(!loadingFocus){pendingFocus.current=null;setMessage('该成交对应的历史 K 线不可用，不会绘制在其他时间。')}return}pendingFocus.current=f;setMessage('正在查询该成交附近的真实历史 K 线。');attempted.current.add(f.fill_id);setLoadingFocus(true);const start=target*1000,duration=CHART_INTERVALS.find(item=>item.value===chartInterval)!.sourceBars*900000;void loadHistory(start,start+duration).then(ok=>{if(!ok){pendingFocus.current=null;setMessage('该成交对应的历史 K 线不可用，不会绘制在其他时间。')}}).finally(()=>setLoadingFocus(false));return}
  pendingFocus.current=null;setMessage('')
  requestAnimationFrame(()=>{const from=Math.max(0,index-Math.floor(visibleCount/2)),to=Math.min(bars.length-1,index+Math.ceil(visibleCount/2));chartRef.current?.timeScale().setVisibleLogicalRange({from,to})})
 }
 useEffect(()=>{if(focusFill)locate(focusFill)},[focusFill])
 useEffect(()=>{if(pendingFocus.current&&!loadingFocus)locate(pendingFocus.current)},[allBars,loadingFocus])
 const ordered=[...validFills].sort((a,b)=>epochMilliseconds(b.occurred_at)-epochMilliseconds(a.occurred_at))
 return <><div className="filters"><span>图表周期：</span>{CHART_INTERVALS.map(option=><button key={option.value} aria-pressed={chartInterval===option.value} className={chartInterval===option.value?'chart-control-active':''} onClick={()=>chooseInterval(option.value)}>{option.label}</button>)}<label>显示根数<select value={visibleCount} onChange={e=>setVisibleCount(Number(e.target.value))}><option value={100}>100 根</option><option value={150}>150 根</option><option value={200}>200 根</option></select></label><span>拖动可浏览最近一年 · 当前 {bars.length} 根完整K线</span><label>历史日期（北京）<input type="date" value={date} onChange={e=>setDate(e.target.value)}/></label><button onClick={()=>{const start=Date.parse(`${date}T00:00:00+08:00`);void loadHistory(start,start+86400000).then(ok=>{if(ok)setTimeout(()=>chartRef.current?.timeScale().setVisibleRange({from:Math.floor(start/1000),to:Math.floor((start+86400000)/1000)}),100)})}}>查询当日</button><button onClick={()=>locate(ordered[0])} disabled={!ordered.length}>定位最近成交</button><label><input type="checkbox" checked={bookEnabled} onChange={e=>setBookEnabled(e.target.checked)}/> BOOK-PA研究图层</label></div><div className="chart" ref={host}/>{bookEnabled&&<div className="notice" role="status"><b>BOOK-PA-V0.1 · 仅研究，不参与交易</b>{bookStatus==='READY'&&bookResearch?<><p>分析截止：{bookResearch.evidence?.last_closed_candle_at?beijing(bookResearch.evidence.last_closed_candle_at):'未记录'} · 结构：{bookResearch.market_structure} · 强度：{bookResearch.trend_strength}</p><p>{bookResearch.explanation}</p><p>知识编号：{(bookResearch.knowledge_ids||[]).join('、')||'未记录'} · 模型结论未经验证</p></>:<p>{bookStatus==='NO_RESEARCH_RESULTS'?'尚无研究结果': '研究接口暂不可用；原交易图表继续显示。'}</p>}</div>}{message&&<p role="status">{message}</p>}
 {sameBar.length>1&&<div><b>该 K 线包含多笔成交，请选择：</b>{sameBar.map(f=><button key={f.fill_id} style={{color:colors[f.agent]}} onClick={()=>setSelected(f)}>{label(f)} · {beijing(f.occurred_at)} · {f.fill_id.slice(0,8)}</button>)}</div>}
 {selected&&<div className="notice" aria-label="成交详情"><b>{label(selected)} · 正式模拟成交</b><p>北京时间：{beijing(selected.occurred_at)} · 成交价：{selected.price} USD · 数量：{selected.quantity} BTC</p><p>决策依据：{selected.rationale||'未记录'}</p><p>交易 ID：{selected.trade_id||'未记录'} · 成交 ID：{selected.fill_id}</p><p>成交版本：{selected.strategy_version||'未记录'}{!selected.strategy_version&&selected.trade_id&&` · 同交易开仓版本：${validFills.find(f=>f.agent===selected.agent&&f.trade_id===selected.trade_id&&f.position_effect==='OPEN')?.strategy_version||'未记录'}`}</p><button onClick={()=>setSelected(null)}>关闭成交详情</button></div>}
 <details><summary>按成交定位（{ordered.length} 条正式成交）</summary>{ordered.map(f=><div key={f.fill_id}><button style={{color:colors[f.agent]}} onClick={()=>locate(f)}>{label(f)} · {beijing(f.occurred_at)} · {f.price} · {f.quantity} BTC · {f.fill_id}</button></div>)}</details></>
}
