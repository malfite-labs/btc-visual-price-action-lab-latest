import {useEffect,useState} from 'react'
import { formatBeijingDateTime, epochMilliseconds } from './time'
export default function RecoveryStatus({data:provided}:{data?:any}) {
 const [data,setData]=useState<any>(provided)
 useEffect(()=>{if(provided)return;const read=()=>fetch('/research/watchdog-status.json',{cache:'no-store'}).then(r=>r.ok?r.json():null).then(setData).catch(()=>setData(null));read();const id=setInterval(read,10000);return()=>clearInterval(id)},[provided])
 const age= data?.updated_utc ? Date.now()-epochMilliseconds(data.updated_utc) : Infinity
 const live=Number.isFinite(age)&&age>=0&&age<90000
 const recovering=live && ['STOP_OLD_INSTANCE','RECOVERY_PENDING','RECOVERY_FAILED'].includes(data.state)
 const state=(value:any,expected:string)=>!live?'未知（快照已过期）':value===null||value===undefined?'未知':value===expected?'正常':'异常'
 return <section className="card"><b>系统运行状态</b><p>后端：{recovering?'恢复中':state(data?.backend===true?'OK':data?.backend===false?'FAIL':null,'OK')} · 行情：{live?(data?.state==='MARKET_RECOVERY_MANAGED'?'行情流保护中':data?.feed==='LIVE'?'实时':'中断'):'未知（快照已过期）'} · ABC调度器：{state(data?.scheduler,'ENABLED_INTERNAL_PAPER')} · 影子盘：{state(data?.shadow,'RUNNING')} · 自动恢复：{live?(data?.automatic?'已启用':'未启用'):'未知'}</p><p>最近恢复：{data?.last_attempt_epoch?formatBeijingDateTime(data.last_attempt_epoch):'无'} · {data?.last_recovery_result||'无记录'} · 更新时间 {live?formatBeijingDateTime(data.updated_utc):'过期'}</p>{live&&data?.pending_exit&&<p className="notice">模拟持仓待止损退出，正在等待服务和行情恢复</p>}{!live&&<p>看门狗状态未更新，自动恢复状态不可确认；不代表后端、行情或调度器当前异常。</p>}</section>
}
