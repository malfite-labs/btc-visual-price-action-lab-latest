import {render,screen} from '@testing-library/react'
import {describe,it,expect} from 'vitest'
import EPanel from './EPanel'

describe('E paper accounts',()=>{
 it('distinguishes missing formal and shadow accounts without inventing performance',()=>{
  render(<EPanel data={{formal:{status:'NOT_INITIALIZED'},shadow:{status:'NOT_INITIALIZED'}}}/>)
  expect(screen.getByText('正式盘：未初始化')).toBeInTheDocument()
  expect(screen.getByText('影子盘：未初始化')).toBeInTheDocument()
 })
 it('shows persisted formal account and independent shadow progress',()=>{
  render(<EPanel data={{formal:{status:'READY',equity:'10100',realized_pnl:'100',closed_trades:2,win_rate:0.5,account:{state:'FLAT'}},shadow:{status:'READY',closed_trades:10,next_review_at:40,account:{state:'FLAT'}}}}/>)
  expect(screen.getByText(/正式权益：10100/)).toBeInTheDocument()
  expect(screen.getByText(/闭合：10.*下次复盘：40/)).toBeInTheDocument()
 })
})

it('shows persisted candidate status for the shadow account',()=>{
 render(<EPanel data={{formal:{status:'NOT_INITIALIZED'},shadow:{status:'READY',closed_trades:40,next_review_at:80,account:{state:'FLAT'},candidate:{status:'RUNNING',batch:40}}} as any}/>)
 expect(screen.getByText(/候选版本：第40批 RUNNING/)).toBeInTheDocument()
})

it('shows the latest persisted E observation and rationale',()=>{
 render(<EPanel data={null} observations={{items:[{close_at:'2026-01-04T00:00:00Z',status:'OBSERVE_ONLY',result:{decision:{action:'SKIP',setup:'CHOP',reasons:['结构不清']}}}]}} />)
 expect(screen.getByText(/SKIP.*CHOP/)).toBeInTheDocument()
 expect(screen.getByText(/结构不清/)).toBeInTheDocument()
})

it('shows V1.1 quantitative candidates separately from V1.0 observations',()=>{
 render(<EPanel data={null} quantObservations={{items:[{close_at:'2026-01-04T00:00:00Z',status:'SETUP_DETECTED',result:{candidate:{setup:'E2'},visual_status:'MODEL_UNCERTAIN',order_eligible:false}}]}} />)
 expect(screen.getByText(/V1.1.*E2.*MODEL_UNCERTAIN/)).toBeInTheDocument()
 expect(screen.getByText(/观察记录：不直接下单/)).toBeInTheDocument()
})

it('shows the reason when E observation is blocked by its call cap',()=>{
 render(<EPanel data={null} observations={{items:[{close_at:'2026-01-04T00:00:00Z',status:'MODEL_FAILED',result:{reason:'VISION_CALL_COUNT_EXHAUSTED'}}]}} />)
 expect(screen.getByText(/VISION_CALL_COUNT_EXHAUSTED/)).toBeInTheDocument()
})

it('does not label all E accounts as the old strategy version',()=>{
 const {container}=render(<EPanel data={null}/> )
 expect(container.querySelector('h2')).toHaveTextContent('憨巴 E · 模拟账户')
})

it('shows configured runtime and disabled short and shadow states',()=>{
 const {container}=render(<EPanel data={{runtime:{mode:'AUTO_PAPER',setups:{E1:true,E2:true,E3:true,E4:false},shadow_execution:'NOT_CONNECTED'}} as any}/> )
 expect(container).toHaveTextContent('AUTO_PAPER')
 expect(container).toHaveTextContent('E4：未启用')
 expect(container).toHaveTextContent('影子执行：未接入')
})


