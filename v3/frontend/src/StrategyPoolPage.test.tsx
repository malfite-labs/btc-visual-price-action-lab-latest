import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import StrategyPoolPage from './StrategyPoolPage'

afterEach(()=>{cleanup();vi.unstubAllGlobals()})

describe('strategy pool desktop workflow',()=>{
 it('shows registration as paused and keeps enablement pending until a real cycle',async()=>{
  let enabled=false
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
   const url=String(input)
   const data=url.endsWith('/strategy-pool')?{items:[{id:'demo',status:enabled?'ENABLED_NOT_RUNNING':'REGISTERED',enabled,active_version:enabled?'1.0':null,cash:'10000',realized:'0',equity:'10000',unrealized:'0',fees:'0',position_direction:'FLAT',position_amount:'0',action:'NO_CYCLE'}]}:
    url.endsWith('/versions')?{items:[{version:'1.0',uploaded_at:'2026-11-11T00:00:00Z',sha256:'abc'}]}:
    url.endsWith('/fills')?{items:[]}:{ok:true,status:enabled?'ENABLED_NOT_RUNNING':'ENABLED_NOT_RUNNING',items:[]}
   if(init?.method==='POST')enabled=true
   return {ok:true,status:200,json:async()=>data} as Response
  }))
  render(<StrategyPoolPage/>)
  expect(await screen.findByText('已登记')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:/demo/}))
  expect(await screen.findByRole('button',{name:'明确启用'})).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'明确启用'}))
  expect((await screen.findAllByText(/已启用待周期/)).length).toBeGreaterThan(0)
  await waitFor(()=>expect(screen.getByText(/等待实际自然周期处理/)).toBeInTheDocument())
  expect(screen.queryByText('运行中')).not.toBeInTheDocument()
 })
})
