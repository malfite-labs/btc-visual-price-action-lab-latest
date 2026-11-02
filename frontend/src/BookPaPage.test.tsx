import React from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
vi.mock('./Chart', () => ({ default: () => <div>chart component</div> }))
import BookPaPage from './BookPaPage'
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
it('displays the actual research response and its historical cutoff', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ok:true, json:async()=>({status:'READY',item:{trade_authorized:false,model_id:'deepseek-flash',market_structure:'down',trend_strength:'weak',explanation:'reason',evidence:{last_closed_candle_at:'2026-10-30T07:45:00Z'},knowledge_ids:['KB-S02'],patterns:[{knowledge_id:'KB-S02',description:'pattern'}],zones:[{kind:'support',price:'100'}]}})})))
  render(<BookPaPage bars={[]} fills={[]} />)
  expect(await screen.findByText('研究服务已连接')).toBeInTheDocument()
  expect(screen.getByText(/2026-10-30 15:45:00/)).toBeInTheDocument()
  expect(screen.getAllByText(/KB-S02/).length).toBeGreaterThan(0)
  expect(screen.getByText(/pattern/)).toBeInTheDocument()
  expect(screen.getByText(/100/)).toBeInTheDocument()
})
it('reports disconnected service without rendering fake data', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
  render(<BookPaPage bars={[]} fills={[]} />)
  expect(await screen.findByText('研究服务不可用')).toBeInTheDocument()
  expect(screen.queryByText('reason')).not.toBeInTheDocument()
})
it('distinguishes an unavailable research store from an empty one', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ok:true, json:async()=>({status:'RESEARCH_DATA_UNAVAILABLE',item:null})})))
  render(<BookPaPage bars={[]} fills={[]} />)
  expect(await screen.findByText('研究服务不可用')).toBeInTheDocument()
  expect(screen.queryByText('暂无可展示的研究结果。')).not.toBeInTheDocument()
})
