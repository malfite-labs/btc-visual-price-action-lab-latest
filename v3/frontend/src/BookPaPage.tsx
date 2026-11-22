import { useEffect, useState } from 'react'
import Chart from './Chart'
import { getBookPaResearch } from './api'
import { formatBeijingDateTime } from './time'

export default function BookPaPage({ bars, fills }: { bars: any[], fills: any[] }) {
  const [state, setState] = useState('LOADING')
  const [result, setResult] = useState<any>(null)
  useEffect(() => {
    let cancelled = false
    const refresh = async () => {
      try {
        const data = await getBookPaResearch()
        if (!cancelled) { setState(data.status); setResult(data.item ?? null) }
      } catch {
        if (!cancelled) { setState('OFFLINE'); setResult(null) }
      }
    }
    void refresh()
    const timer = setInterval(refresh, 30000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [])
  const ready = state === 'READY' && result?.trade_authorized === false
  const unavailable = state === 'OFFLINE' || state === 'RESEARCH_DATA_UNAVAILABLE'
  return <>
    <section className="title"><div><h1>BOOK-PA 研究</h1><p>独立视觉研究 · 只读 · 不参与交易</p></div>
      <span className="pill">{unavailable ? '研究服务不可用' : state === 'LOADING' ? '正在连接研究服务' : '研究服务已连接'}</span></section>
    {unavailable && <div className="notice">研究接口或研究数据不可用，原交易工作台不受影响。</div>}
    {state === 'NO_RESEARCH_RESULTS' && <div className="notice">暂无可展示的研究结果。</div>}
    {ready && <section className="grid2">
      <article className="card"><h2>最近一次真实分析</h2>
        <p>历史分析 · K线截止：{result.evidence?.last_closed_candle_at ? formatBeijingDateTime(result.evidence.last_closed_candle_at) : '未记录'}（北京时间）</p>
        <p>模型调用时间：{result.evidence?.analysis_at ? formatBeijingDateTime(result.evidence.analysis_at) : '未记录'} · 模型：{result.model_id || '未记录'}</p>
        <p>市场结构：{result.market_structure} · 趋势强度：{result.trend_strength}</p><p>{result.explanation}</p>
        <p>知识库依据：{(result.knowledge_ids || []).join('、') || '未记录'}</p>
      </article>
      <article className="card"><h2>形态与关键位置</h2>
        {(result.patterns || []).length ? result.patterns.map((p: any, i: number) => <p key={i}>{p.description} · {p.knowledge_id}<br />确认：{p.confirmation || '未记录'} · 失效：{p.invalidation || '未记录'}</p>) : <p>未识别已记录形态</p>}
        {[...(result.zones || []), ...(result.candidates || [])].map((z: any, i: number) => <p key={i}>{({support:'支撑',resistance:'阻力',candidate_long:'候选买',candidate_short:'候选卖',invalidation:'失效位'} as Record<string,string>)[z.kind] || z.kind}：{z.price}</p>)}
      </article>
    </section>}
    <section className="card chartcard" style={{marginTop: 16}}><div className="cardhead"><b>BTC/USD · 15分钟研究图</b><span>使用图内开关显示研究图层</span></div>{bars.length ? <Chart items={bars} fills={fills} /> : <div className="empty">暂无K线数据</div>}</section>
  </>
}
