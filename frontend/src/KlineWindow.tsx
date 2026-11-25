import { useEffect, useMemo, useRef, useState } from 'react'
import { CandlestickSeries, HistogramSeries, LineSeries, createChart, createSeriesMarkers, type UTCTimestamp, type SeriesMarker, type Time } from 'lightweight-charts'
import { boll, ema, macd, rsi, sma } from './klineIndicators'
import { barForTime, demoEvent, eventLabel, groups, mergeBars, strategies, type Candle, type DemoSnapshot, type Event } from './klineModel'
import { formatBeijingTick } from './time'
import './klineWindow.css'

const defaults={EMA:'9,20,50',SMA:'20',BOLL:'20,2',MACD:'12,26,9',RSI:'14',VOL:''}
type Indicator=keyof typeof defaults
const keys=Object.keys(defaults) as Indicator[]
const activeDefaults=['EMA','MACD','VOL'] as Indicator[]
const themes = {
  light: {
    background: '#ffffff', text: '#475569', grid: '#e8edf3', border: '#cbd5e1',
    crosshair: '#64748b', crosshairLabel: '#334155',
    up: '#087f5b', down: '#c43149', selected: '#9a5700',
    indicators: ['#a56608', '#2166b5', '#7842a9', '#087c70'],
    boll: '#4b5fc2', bollMiddle: '#676b91',
    macdUp: '#168773', macdDown: '#ba4355', volumeUp: '#61a390', volumeDown: '#cc7885',
  },
  dark: {
    background: '#101721', text: '#b9c8da', grid: '#233142', border: '#384a60',
    crosshair: '#8196ae', crosshairLabel: '#364b61',
    up: '#26bb8a', down: '#ee6573', selected: '#ffd87f',
    indicators: ['#ffc46a', '#71a8ff', '#b49afd', '#62d5ba'],
    boll: '#899aff', bollMiddle: '#8797bd',
    macdUp: '#38ae90', macdDown: '#d1667b', volumeUp: '#357e70', volumeDown: '#a3566b',
  },
}
type Theme = keyof typeof themes
const beijing=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',dateStyle:'short',timeStyle:'medium',hourCycle:'h23'})
const stamp=(c:Candle) => c.open_at_ms/1000 as UTCTimestamp
const usdt=new Intl.NumberFormat('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})
const amount=(value:string|null|undefined)=>value==null?'—':usdt.format(Number(value))
function saved<T,>(key:string,fallback:T):T {
  try {return JSON.parse(localStorage.getItem('btc-kline-'+key)??'null')??fallback} catch {return fallback}
}
function numbers(key:Indicator,value:string) {
  const values=value.split(',').map(x=>Number(x.trim()))
  const length=key==='MACD'?3:key==='BOLL'?2:key==='RSI'?1:values.length
  if(!value.trim()||values.length!==length||values.length>6||values.some((v,i)=>!Number.isFinite(v)||v<1||v>500||(!(key==='BOLL'&&i===1)&&!Number.isInteger(v)))||(key==='MACD'&&values[0]>=values[1])) return numbers(key,defaults[key])
  return values
}
async function read(path:string,signal?:AbortSignal) {
  const response=await fetch('/api/v1/observation/'+path,{signal,cache:'no-store',redirect:'error'})
  if(!response.ok) throw Error('HTTP '+response.status)
  return response.json()
}

export default function KlineWindow({ live = false }: { live?: boolean }) {
  const [theme, setTheme] = useState<Theme>(() => saved<string>('theme', 'light') === 'dark' ? 'dark' : 'light')
  const palette = themes[theme]
  const [interval,setIntervalValue]=useState('15m')
  const [intervalOpen,setIntervalOpen]=useState(false)
  const [bars,setBars]=useState<Candle[]>([])
  const [events,setEvents]=useState<Event[]>([])
  const [environment,setEnvironment]=useState('demo')
  const [demo,setDemo]=useState<DemoSnapshot|null>(null)
  const [connection,setConnection]=useState('正在连接')
  const [eventState,setEventState]=useState('正在读取成交')
  const [notice,setNotice]=useState('')
  const [ohlc,setOhlc]=useState('')
  const [active,setActive]=useState<Indicator[]>(()=>saved('active',activeDefaults))
  const [params,setParams]=useState(()=>saved('params',defaults))
  const [selection,setSelection]=useState(strategies)
  const [effect,setEffect]=useState('all')
  const [markersOn,setMarkersOn]=useState(true)
  const [cursor,setCursor]=useState(0)
  const [paused,setPaused]=useState(false)
  const [detail,setDetail]=useState<Event[]>([])
  const [highlight,setHighlight]=useState('')
  const host=useRef<HTMLDivElement>(null)
  const chart=useRef<ReturnType<typeof createChart>|null>(null)
  const series=useRef<any>(null)
  const markerApi=useRef<any>(null)
  const overlays=useRef<any[]>([])
  const overlaySignature=useRef('')
  const previous=useRef<Candle[]>([])
  const current=useRef<Candle[]>([])
  const buckets=useRef(new Map<number,Event[]>())
  const older=useRef<()=>void>(()=>{})
  const oldestExhausted=useRef(false)
  const inFlight=useRef(false)
  const generation=useRef(0)
  const pendingFocus=useRef<Event|null>(null)
  const cache=useRef(new Map<string,Candle[]>())
  const warmed=useRef(false)
  const markerActive=useRef(true)
  const visibleAt=useRef<number|null>(null)
  const focusRequest=useRef(0)
  const requestedStrategy=useRef<string|null|undefined>(undefined)

  const available=live?(environment==='demo'?demo?.strategies??[]:[]):strategies
  const sourceEvents=useMemo(()=>live?(environment==='demo'?demo?.items.map(demoEvent)??[]:[]):events,[live,environment,demo,events])
  const filtered=useMemo(()=>sourceEvents.filter(e=>e.verification_status==='VERIFIED'&&selection.includes(e.strategy_id)&&(effect==='all'||(effect==='open')===(e.position_effect==='OPEN'))).sort((a,b)=>Date.parse(b.occurred_at)-Date.parse(a.occurred_at)),[sourceEvents,selection,effect])

  useEffect(()=>{
    if(!live||environment!=='demo')return
    const controller=new AbortController()
    let busy=false
    const refresh=async()=>{
      if(busy)return
      busy=true
      try {
        const response=await fetch('http://127.0.0.1:5187/demo-fills',{signal:controller.signal,cache:'no-store',redirect:'error'})
        if(!response.ok)throw Error(String(response.status))
        const snapshot:DemoSnapshot=await response.json()
        if(snapshot.environment!=='demo')throw Error('环境不符')
        if(!controller.signal.aborted){
          setDemo(snapshot)
          if(snapshot.selected_strategy!==requestedStrategy.current){
            requestedStrategy.current=snapshot.selected_strategy
            setSelection(snapshot.selected_strategy?[snapshot.selected_strategy]:[...new Set([...strategies,...snapshot.strategies])])
            setCursor(0)
            setDetail([])
            setHighlight('')
          }
          setEventState(snapshot.errors.length?'部分 Demo 账本不可用':'')
        }
      } catch {
        if(!controller.signal.aborted)setEventState('Demo 成交读取暂不可用')
      } finally {busy=false}
    }
    void refresh()
    const timer=window.setInterval(refresh,5000)
    return ()=>{controller.abort();clearInterval(timer)}
  },[live,environment])

  useEffect(()=>{
    localStorage.setItem('btc-kline-active',JSON.stringify(active))
    localStorage.setItem('btc-kline-params',JSON.stringify(params))
  },[active,params])
  useEffect(()=>{
    if(!host.current)return
    const c=createChart(host.current,{autoSize:true,timeScale:{timeVisible:true,secondsVisible:false,tickMarkFormatter:(time:Time,type:number)=>typeof time==='number'?formatBeijingTick(time,type):String(time)},localization:{timeFormatter:(t:Time)=>typeof t==='number'?beijing.format(t*1000):String(t)}})
    const s=c.addSeries(CandlestickSeries,{borderVisible:false})
    chart.current=c;series.current=s;markerApi.current=createSeriesMarkers(s,[])
    c.subscribeCrosshairMove(p=>{
      const bar=p.seriesData.get(s)
      if(bar&&'open' in bar)setOhlc('O '+bar.open+'  H '+bar.high+'  L '+bar.low+'  C '+bar.close)
      else setOhlc('')
    })
    c.subscribeClick(p=>{if(markerActive.current&&typeof p.time==='number'){
      const list=buckets.current.get(p.time)
      if(list?.length){
        const selected=list.find(e=>e.event_id===p.hoveredObjectId)
        setDetail(selected?[selected]:list)
        setHighlight((selected||list[0]).event_id)
      }
    }})
    c.timeScale().subscribeVisibleLogicalRangeChange(range=>{if(range&&range.from<12)older.current()})
    return ()=>{c.remove();chart.current=null;overlays.current=[];overlaySignature.current=''}
  },[])

  useEffect(() => {
    try { localStorage.setItem('btc-kline-theme', JSON.stringify(theme)) } catch { /* Keep the in-window selection when storage is unavailable. */ }
    chart.current?.applyOptions({
      layout: {
        background: { color: palette.background },
        textColor: palette.text,
        panes: { separatorColor: palette.border, separatorHoverColor: palette.crosshair },
      },
      grid: { vertLines: { color: palette.grid }, horzLines: { color: palette.grid } },
      crosshair: {
        vertLine: { color: palette.crosshair, labelBackgroundColor: palette.crosshairLabel },
        horzLine: { color: palette.crosshair, labelBackgroundColor: palette.crosshairLabel },
      },
      timeScale: { borderColor: palette.border },
      rightPriceScale: { borderColor: palette.border },
    })
    series.current?.applyOptions({
      upColor: palette.up, downColor: palette.down,
      wickUpColor: palette.up, wickDownColor: palette.down,
    })
  }, [palette, theme])

  useEffect(()=>{
    if (live) return
    const controller=new AbortController()
    let busy=false
    const refresh=async()=>{
      if(busy)return
      busy=true
      try {
        const data=await read('events',controller.signal)
        if(!controller.signal.aborted) {
          setEvents(data.items)
          setEventState(data.errors?.length?'部分成交来源不可用':'')
        }
      } catch {if(!controller.signal.aborted)setEventState('成交读取暂不可用，保留上次数据')}
      finally {busy=false}
    }
    void refresh()
    const timer=window.setInterval(refresh,15000)
    return ()=>{controller.abort();clearInterval(timer)}
  },[live])

  useEffect(()=>{
    const controller=new AbortController()
    generation.current++
    inFlight.current=false
    warmed.current=false
    oldestExhausted.current=false
    previous.current=[]
    pendingFocus.current=null
    setBars(cache.current.get(interval)||[])
    setConnection('正在连接')
    setNotice('')
    let busy=false
    const refresh=async()=>{
      if(busy)return
      busy=true
      try {
        const data=await read('candles?interval='+interval+'&limit='+(cache.current.has(interval)?10:500),controller.signal)
        if(controller.signal.aborted)return
        if(data.source!=='BINANCE_USDM_PERP'||data.interval!==interval)throw Error('行情来源或周期不符')
        setBars(old=>{const next=mergeBars(old,data.items);cache.current.set(interval,next);return next})
        setConnection('公共行情已连接')
      } catch {if(!controller.signal.aborted)setConnection('行情暂不可用 · 保留已有数据')}
      finally {busy=false}
    }
    void refresh()
    const timer=window.setInterval(refresh,12000)
    return ()=>{controller.abort();clearInterval(timer)}
  },[interval])

  useEffect(()=>{
    if(paused||filtered.length<2)return
    const timer=window.setInterval(()=>setCursor(i=>(i+1)%filtered.length),6000)
    return ()=>clearInterval(timer)
  },[paused,filtered.length])

  function locate(index:number) {
    const scale=chart.current?.timeScale()
    if(!scale)return
    const range=scale.getVisibleLogicalRange()
    const half=Math.max(12,Math.min(80,range?(range.to-range.from)/2:40))
    scale.setVisibleLogicalRange({from:Math.max(-2,index-half),to:index+half})
  }
  async function history(end:number,event?:Event) {
    if(inFlight.current&&!event)return
    if(!event)inFlight.current=true
    const requested=generation.current
    const request=focusRequest.current
    try {
      const data=await read('candles?interval='+interval+'&limit=500&end_time='+end)
      if(requested!==generation.current||(event&&request!==focusRequest.current))return
      if(data.source!=='BINANCE_USDM_PERP'||data.interval!==interval)throw Error('来源或周期不符')
      if(event&&barForTime(data.items,Date.parse(event.occurred_at))<0) {
        setNotice('该成交对应的真实 K 线不可用')
        return
      }
      if(!event && data.items.length===0)oldestExhausted.current=true
      if(event)pendingFocus.current=event
      setBars(old=>{const next=mergeBars(old,data.items);cache.current.set(interval,next);return next})
    } catch {if(requested===generation.current&&(!event||request===focusRequest.current))setNotice('历史行情读取失败，已保留现有图表')}
    finally {if(!event&&requested===generation.current)inFlight.current=false}
  }
  older.current=()=>{if(warmed.current&&!oldestExhausted.current&&current.current.length)void history(current.current[0].open_at_ms-1)}

  useEffect(()=>{
    const c=chart.current
    if(!c||!series.current)return
    current.current=bars
    const before=previous.current
    const range=c.timeScale().getVisibleLogicalRange()
    const first=before[0]?.open_at_ms
    const olderCount=first===undefined?0:bars.findIndex(b=>b.open_at_ms===first)
    const candle=(b:Candle)=>({time:stamp(b),open:+b.open,high:+b.high,low:+b.low,close:+b.close})
    const stablePrefix=before.length>0&&bars.length>=before.length&&before.slice(0,-1).every((b,i)=>bars[i]===b)&&bars[before.length-1].open_at_ms===before.at(-1)?.open_at_ms
    if(stablePrefix)bars.slice(before.length-1).forEach(b=>series.current.update(candle(b)))
    else series.current.setData(bars.map(candle))
    previous.current=bars
    if(bars.length&&!warmed.current) {
      const anchor=visibleAt.current
      const index=anchor===null?-1:barForTime(bars,anchor)
      if(index>=0)locate(index)
      else c.timeScale().setVisibleLogicalRange({from:Math.max(0,bars.length-110),to:bars.length+4})
      visibleAt.current=null
      warmed.current=true
    } else if(range&&olderCount>0)c.timeScale().setVisibleLogicalRange({from:range.from+olderCount,to:range.to+olderCount})
    if(pendingFocus.current) {
      const index=barForTime(bars,Date.parse(pendingFocus.current.occurred_at))
      if(index>=0)locate(index)
      pendingFocus.current=null
    }
  },[bars])

  useEffect(()=>{
    const c=chart.current
    if(!c)return
    const signature=JSON.stringify([active,params])
    if(signature!==overlaySignature.current) {
      overlays.current.forEach(s=>c.removeSeries(s))
      overlays.current=[]
      overlaySignature.current=signature
    }
    let slot=0
    const addSeries=(definition:any, options:any,pane:number)=>{
      const index=slot++
      return overlays.current[index]??(overlays.current[index]=c.addSeries(definition,options,pane))
    }
    const closes=bars.map(b=>+b.close)
    const line=(values:(number|null)[], color:string,pane=0)=>{
      const s=addSeries(LineSeries,{color,lineWidth:1,priceLineVisible:false,lastValueVisible:false},pane)
      s.applyOptions({color})
      s.setData(values.flatMap((value,i)=>value===null?[]:[{time:stamp(bars[i]),value}]))
    }
    let pane=1
    const colors = palette.indicators
    if(active.includes('EMA'))numbers('EMA',params.EMA).forEach((n,i)=>line(ema(closes,n),colors[i%colors.length]))
    if(active.includes('SMA'))numbers('SMA',params.SMA).forEach((n,i)=>line(sma(closes,n),colors[(i+2)%colors.length]))
    if(active.includes('BOLL')) {
      const [n,m]=numbers('BOLL',params.BOLL)
      const bands=boll(closes,n,m)
      line(bands.map(x=>x?.upper??null),palette.boll)
      line(bands.map(x=>x?.middle??null),palette.bollMiddle)
      line(bands.map(x=>x?.lower??null),palette.boll)
    }
    if(active.includes('MACD')) {
      const [fast,slow,signal]=numbers('MACD',params.MACD)
      const data=macd(closes,fast,slow,signal)
      const hist=addSeries(HistogramSeries,{priceLineVisible:false,lastValueVisible:false},pane)
      hist.setData(data.flatMap((v,i)=>v.hist===null?[]:[{time:stamp(bars[i]),value:v.hist,color:v.hist>=0?palette.macdUp:palette.macdDown}]))
      line(data.map(x=>x.dif),colors[0],pane)
      line(data.map(x=>x.signal),colors[1],pane++)
    }
    if(active.includes('RSI'))line(rsi(closes,numbers('RSI',params.RSI)[0]),colors[2],pane++)
    if(active.includes('VOL')) {
      const vol=addSeries(HistogramSeries,{priceFormat:{type:'volume'},priceLineVisible:false,lastValueVisible:false},pane)
      vol.setData(bars.map(b=>({time:stamp(b),value:+b.volume,color:+b.close>=+b.open?palette.volumeUp:palette.volumeDown})))
    }
    c.panes().forEach((p,i)=>p.setStretchFactor(i===0?5:1))
  },[bars,active,params,palette])

  useEffect(()=>{
    markerActive.current=markersOn
    const next=new Map<number,Event[]>()
    if(markersOn)for(const e of filtered) {
      const index=barForTime(bars,Date.parse(e.occurred_at))
      if(index<0)continue
      const time=stamp(bars[index])
      next.set(time,[...(next.get(time)||[]),e])
    }
    buckets.current=next
    const markers:SeriesMarker<Time>[]=[...next.entries()].sort((a,b)=>a[0]-b[0]).flatMap(([time,items]):SeriesMarker<Time>[]=>{
      if(live)return items.map(e=>{
        const buy=e.demo?.execution_side==='BUY'
        const open=e.position_effect==='OPEN'
        return {id:e.event_id,time:time as UTCTimestamp,position:'atPriceMiddle',price:Number(e.price),
          shape:open?(buy?'arrowUp':'arrowDown'):'square',
          color:e.event_id===highlight?palette.selected:buy?palette.up:palette.down,text:eventLabel(e)}
      })
      const selected=items.find(x=>x.event_id===highlight)
      const e=selected||items[0]
      const buy=(e.position_effect==='OPEN')===(e.position_direction==='LONG')
      return [{time:time as UTCTimestamp,position:buy?'belowBar':'aboveBar',shape:e.position_effect==='PARTIAL_CLOSE'?'circle':buy?'arrowUp':'arrowDown',color:selected?palette.selected:buy?palette.up:palette.down,text:items.length>1?eventLabel(e)+' +'+(items.length-1):eventLabel(e)}]
    })
    markerApi.current?.setMarkers(markers)
  },[bars,filtered,markersOn,highlight,palette,live])

  useEffect(()=>{
    const s=series.current
    if(!live||environment!=='demo'||!s)return
    const guides=(demo?.positions??[]).filter(p=>selection.includes(p.strategy_id)).map(p=>s.createPriceLine({
      price:Number(p.price),color:p.position_side==='LONG'?palette.up:palette.down,
      lineStyle:2,lineWidth:1,axisLabelVisible:true,
      title:`${p.strategy_id} ${p.position_side==='LONG'?'做多':'做空'} ${p.quantity} BTC${p.unrealized_pnl==null?'':' · 浮盈亏 '+amount(p.unrealized_pnl)+' USDT'}`,
    }))
    return ()=>{if(chart.current)guides.forEach(line=>s.removePriceLine(line))}
  },[live,environment,demo,selection,palette])

  const latest=bars.at(-1)
  const prior=bars.at(-2)
  const change=latest&&prior?((+latest.close/+prior.close-1)*100).toFixed(2):null
  const shown=Array.from({length:Math.min(3,filtered.length)},(_,i)=>filtered[(cursor+i)%filtered.length])
  function focus(event:Event) {
    focusRequest.current++
    setDetail([event]);setHighlight(event.event_id);setNotice('')
    const time=Date.parse(event.occurred_at)
    const index=barForTime(bars,time)
    if(index>=0)locate(index)
    else void history(time,event)
  }
  function selectEnvironment(value:string) {
    focusRequest.current++
    pendingFocus.current=null
    setEnvironment(value);setDetail([]);setHighlight('');setNotice('');setCursor(0)
  }
  return <main className="kw" data-theme={theme}>
    <div className="kw-market">
      <strong>BTCUSDT</strong><span>Binance USDⓈ-M</span>
      <b>{latest?Number(latest.close).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'—'}</b>
      {change!==null&&<span title="相对上一根 K 线收盘价">{change}%</span>}
      <span role="status">{connection}</span>
      <div className="kw-dropdown" onKeyDown={e=>{if(e.key==='Escape')setIntervalOpen(false)}} onBlur={e=>{if(!e.currentTarget.contains(e.relatedTarget))setIntervalOpen(false)}}>
        <button aria-label="K线周期" aria-expanded={intervalOpen} onClick={()=>setIntervalOpen(x=>!x)}>{interval} ▾</button>
        {intervalOpen&&<div className="kw-pop kw-intervals" aria-label="周期选项">
          {groups.map(([name,values])=><div key={name}><small>{name}</small><div>{values.map(value=><button key={value} aria-pressed={interval===value} onClick={e=>{
            const visible=chart.current?.timeScale().getVisibleRange()
            visibleAt.current=typeof visible?.to==='number'?visible.to*1000:null
            if(value!==interval)setIntervalValue(value)
            e.currentTarget.closest('.kw-dropdown')?.querySelector('button')?.focus()
            setIntervalOpen(false)
          }}>{value}</button>)}</div></div>)}
        </div>}
      </div>
      {live&&<label>交易环境 <select aria-label="交易环境" value={environment} onChange={e=>selectEnvironment(e.target.value)}><option value="demo">Demo跟单</option><option value="production">正式实盘</option></select></label>}
      <details className="kw-dropdown"><summary>{live?'跟单策略':'策略'} · {available.every(s=>selection.includes(s))?'全部':available.filter(s=>selection.includes(s)).length} ▾</summary><div className="kw-pop">
        <button onClick={()=>{setSelection(live?[...new Set([...strategies,...available])]:strategies);setDetail([]);setCursor(0)}}>全选</button> <button onClick={()=>{setSelection([]);setDetail([]);setCursor(0)}}>清空</button>
        <div className="kw-strategies">{available.map(s=><label key={s}><input type="checkbox" checked={selection.includes(s)} onChange={()=>{setSelection(old=>old.includes(s)?old.filter(x=>x!==s):[...old,s]);setDetail([]);setCursor(0)}}/>{s}</label>)}</div>
        <select aria-label="成交类型" value={effect} onChange={e=>{setEffect(e.target.value);setCursor(0)}}><option value="all">全部成交</option><option value="open">只看开仓</option><option value="close">只看退出</option></select>
        <label><input type="checkbox" checked={markersOn} onChange={e=>setMarkersOn(e.target.checked)}/>成交标记</label>
      </div></details>
      <button className="kw-latest" onClick={()=>chart.current?.timeScale().scrollToRealTime()}>返回最新</button>
      <button type="button" aria-label="黑夜主题" aria-pressed={theme==='dark'} onClick={()=>setTheme(value=>value==='light'?'dark':'light')}>
        {theme==='light'?'☀ 白天':'☾ 黑夜'}
      </button>
    </div>
    <div className="kw-indicators">
      {keys.map(k=><button key={k} aria-pressed={active.includes(k)} onClick={()=>setActive(old=>old.includes(k)?old.filter(x=>x!==k):[...old,k])}>{k}{active.includes(k)&&params[k]?' '+params[k].replaceAll(',','/'):''}</button>)}
      <details className="kw-dropdown"><summary>参数设置 ▾</summary><div className="kw-pop">{keys.filter(k=>k!=='VOL').map(k=><label className="kw-param" key={k}>{k}<input aria-label={k+'参数'} value={params[k]} onChange={e=>setParams({...params,[k]:e.target.value})}/></label>)}<button onClick={()=>setParams(defaults)}>恢复默认值</button></div></details>
    </div>
    <div className="kw-chart-shell">
      <div className="kw-chart-status">{ohlc||'BTCUSDT · '+interval+' · 北京时间'} {latest&&!latest.is_closed&&' · 最新 K 线形成中'} {active.some(k=>k!=='VOL'&&bars.length<(k==='MACD'?numbers(k,params[k])[1]+numbers(k,params[k])[2]-1:Math.max(...numbers(k,params[k]))+(k==='RSI'?1:0)))&&' · 部分指标预热不足'}</div>
      <div ref={host} className="kw-chart" />
      {notice&&<div className="kw-notice" role="status">{notice}</div>}
      {detail.length>0&&<aside className="kw-detail"><button aria-label="关闭成交详情" onClick={()=>setDetail([])}>×</button>{detail.map(e=><article key={e.event_id}><b>{eventLabel(e)} · {e.position_direction}</b><p>{beijing.format(Date.parse(e.occurred_at))} · {e.quantity} BTC @ {e.price}</p>{e.demo?<><p>成交金额 {amount(e.demo.filled_notional_usdt)} USDT · 手续费 {amount(e.demo.fee_usdt)} USDT</p><p>成交已实现 {amount(e.demo.realized_pnl_usdt)} USDT · 平仓净盈亏（资金费前）{amount(e.demo.net_pnl_before_funding_usdt)} USDT</p><small>Demo 订单 {e.demo.order_id}<br/>纸盘关联 {e.demo.paper_trade_id}<br/>跟单配置 {e.demo.copy_config_id}</small></>:<><p>{e.reason_text||'原因未记录'}</p><small>成交时版本 {e.strategy_version||'未记录'} · 交易 {e.trade_id||'未记录'}<br/>来源 {e.source}{e.realized_pnl!=null?' · 已实现 '+e.realized_pnl:''}</small></>}</article>)}</aside>}
    </div>
    <section className="kw-feed" onMouseEnter={()=>setPaused(true)} onMouseLeave={()=>setPaused(false)} onFocus={()=>setPaused(true)} onBlur={e=>{if(!e.currentTarget.contains(e.relatedTarget))setPaused(false)}}>
      <div className="kw-feed-top"><strong>{live?(environment==='demo'?'Demo跟单成交动态':'正式实盘成交动态'):'策略交易动态'}</strong><span>{live?(environment==='demo'&&eventState?eventState:'交易所确认成交 · 北京时间'):eventState||'正式模拟成交 · 北京时间'} {events.some(e=>e.verification_status!=='VERIFIED')&&' · 存在 UNVERIFIED 记录'}</span>{!live&&<><button aria-label="上一条" disabled={filtered.length<2} onClick={()=>setCursor((cursor+filtered.length-1)%filtered.length)}>‹</button><button aria-label="下一条" disabled={filtered.length<2} onClick={()=>setCursor((cursor+1)%filtered.length)}>›</button></>}</div>
      {live?<div className="kw-fill-table">{filtered.length?<table><thead><tr>{['成交时间','策略','开/平','方向','价格','数量 BTC','成交金额 USDT','手续费 USDT','净盈亏 USDT（资金费前）','订单 ID'].map(label=><th key={label}>{label}</th>)}</tr></thead><tbody>{filtered.map(e=><tr key={e.event_id} aria-selected={highlight===e.event_id} onClick={()=>focus(e)}>
        <td><button onClick={ev=>{ev.stopPropagation();focus(e)}}>{beijing.format(Date.parse(e.occurred_at))}</button></td><td>{e.strategy_id}</td><td>{e.position_effect==='OPEN'?'开仓':'平仓'}</td><td>{e.position_direction==='LONG'?'做多':'做空'} · {e.demo?.execution_side==='BUY'?'买':'卖'}</td><td>{e.price}</td><td>{e.quantity}</td><td>{amount(e.demo?.filled_notional_usdt)}</td><td>{amount(e.demo?.fee_usdt)}</td><td>{amount(e.demo?.net_pnl_before_funding_usdt)}</td><td>{e.demo?.order_id}</td>
      </tr>)}</tbody></table>:<p>{environment==='production'?'暂无正式实盘成交':eventState||(sourceEvents.length?'所选策略暂无 Demo 成交':'暂无 Demo 跟单成交')}</p>}</div>
      :<div className="kw-cards">{shown.length?shown.map(e=><button className="kw-card" key={e.event_id} onClick={()=>focus(e)} aria-pressed={highlight===e.event_id}><strong>{eventLabel(e)} · {e.position_direction}</strong><span>{beijing.format(Date.parse(e.occurred_at))} · {e.quantity} BTC @ {e.price}</span><small title={e.reason_text||'原因未记录'}>{e.reason_text||'原因未记录'}{e.realized_pnl!=null?' · 已实现 '+e.realized_pnl:''}</small></button>):<p>{eventState||'暂无已确认成交，等待策略自然运行。'}</p>}</div>}
    </section>
  </main>
}
