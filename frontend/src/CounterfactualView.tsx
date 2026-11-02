import {useEffect,useState} from 'react'
import {formatBeijingDateTime} from './time'
export default function CounterfactualView({data:provided}:{data?:any}) {
 const [data,setData]=useState<any>(provided),[view,setView]=useState<'formal'|'scenario'>('formal')
 useEffect(()=>{if(!provided)fetch('/research/c-counterfactual-v30.json').then(r=>r.ok?r.json():null).then(setData).catch(()=>setData(null))},[provided])
 if(!data || !Array.isArray(data.equity_events))return null
 return <section className="card"><h2>C 历史权益与补录情景</h2><p>仅展示 2026-10-28 20:00 至 20:15 的历史事件；非当前账户权益。</p><p><button onClick={()=>setView('formal')}>原始正式历史</button>{' '}<button onClick={()=>setView('scenario')}>追溯补录情景</button></p>{view==='formal'?<p>原始正式历史：两次均被拒绝，未产生 C 成交，期间已实现权益 $10,000.00。</p>:<><p>反事实 · C V3.0 · 模拟已实现净盈亏 ${data.realized_pnl} · 已平仓；不能计入正式或其他版本成绩。</p><p>采用独立影子盘当时记录的报价预检及成交结果；正式盘盘口未保存，无法还原正式盘本会成交的价格。盘中浮动权益没有完整记录。</p></>}<table><thead><tr><th>时间</th><th>事件</th><th>{view==='formal'?'正式已记录权益':'情景现金或平仓权益'}</th></tr></thead><tbody>{data.equity_events.map((x:any)=><tr key={x.time}><td>{formatBeijingDateTime(x.time)}</td><td>{x.description}</td><td>${view==='formal'?x.formal:x.scenario}</td></tr>)}</tbody></table></section>
}
