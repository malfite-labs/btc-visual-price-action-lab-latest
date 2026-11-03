import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup } from '@testing-library/react'
import DecisionLog from './DecisionLog'

afterEach(cleanup)

describe('decision log', () => {
  it('groups real decisions, converts times and does not call WAIT flat when holding', () => {
    const items = [{agent:'A',candle_close:'2026-10-27T15:15:00Z',action:'WAIT',outcome:'VALID',rationale:'No confirmed reversal.',fills:[]}]
    render(<DecisionLog items={items} agents={[{agent:'A',account:{state:'OPEN_LONG'}}]} cycleEvents={{}} />)
    expect(screen.getByText(/2026-10-27 23:15/)).toBeInTheDocument()
    expect(screen.getByText('WAIT · 观望')).toBeInTheDocument()
    expect(screen.getByText('观望，继续持仓（当前多仓）')).toBeInTheDocument()
    expect(screen.getByText('No confirmed reversal.')).toBeInTheDocument()
    expect(screen.getAllByText('尚无决策记录')).toHaveLength(2)
  })
  it('distinguishes a rejected decision and an actual fill', () => {
    const t='2026-10-27T15:15:00Z'
    const items=[{agent:'A',candle_close:t,action:'OPEN_LONG',outcome:'VALID',rationale:'',fills:[]},
      {agent:'B',candle_close:t,action:'OPEN_SHORT',outcome:'VALID',rationale:'short',fills:[{position_effect:'OPEN'}]}]
    const cycleEvents = {[new Date(t).toISOString()]:{A:{execution:{accepted:false,reason:'QUOTE_OR_BOOK_STALE'}}}}
    render(<DecisionLog items={items} agents={[]} cycleEvents={cycleEvents} />)
    expect(screen.getByText('LONG · 开多')).toBeInTheDocument()
    expect(screen.getByText(/QUOTE_OR_BOOK_STALE/)).toBeInTheDocument()
    expect(screen.getByText('SHORT · 开空')).toBeInTheDocument()
    expect(screen.getByText('已开仓')).toBeInTheDocument()
    expect(screen.getByText('本周期未提供详细理由')).toBeInTheDocument()
  })
  it('shows ten latest periods and lets the user expand history', () => {
    const start = Date.parse('2026-10-27T15:15:00Z')
    const items=Array.from({length:11}, (_,i)=>({agent:'C',candle_close:new Date(start-i*900000).toISOString(),
      action:'CLOSE_SHORT',outcome:'VALID',rationale:'Exit.',fills:[{position_effect:'CLOSE'}]}))
    const {container} = render(<DecisionLog items={items} agents={[]} cycleEvents={{}} />)
    expect(container.querySelectorAll('.decision-cycle')).toHaveLength(10)
    expect(screen.getAllByText('CLOSE · 平仓')).toHaveLength(10)
    fireEvent.click(screen.getByRole('button',{name:'查看更多历史周期'}))
    expect(container.querySelectorAll('.decision-cycle')).toHaveLength(11)
  })
})

it('labels rule decisions and preserves visual history', () => {
 const t='2026-10-28T10:00:00Z'
 const items=[{agent:'A',candle_close:t,action:'WAIT',mode:'RULE_DRIVEN',md_version:'V2.5',outcome:'VALID',rationale:'突破条件未满足',fills:[]}, {agent:'B',candle_close:t,action:'WAIT',mode:'REAL_PROVIDER',md_version:'v1.0.0',outcome:'VALID',rationale:'旧视觉记录',fills:[]}]
 render(<DecisionLog items={items} agents={[]} cycleEvents={{}} />)
 expect(screen.getByText('RULE_DRIVEN · V2.5')).toBeInTheDocument()
 expect(screen.getByText('REAL_PROVIDER · v1.0.0')).toBeInTheDocument()
 expect(screen.getByText('突破条件未满足')).toBeInTheDocument()
})

it('explains historic combined rejection without inventing a cause', () => {
 const items=[{agent:'C',candle_close:'2026-10-28T12:15:00Z',action:'OPEN_SHORT',outcome:'RISK_REJECTED',raw_response:{execution:{accepted:false,reason:'TARGET_OR_OBSTACLE_REJECTED'}}}]
 render(<DecisionLog items={items} agents={[]} cycleEvents={{}} />)
 expect(screen.getByText(/历史拒单详情不可用/)).toBeInTheDocument()
})

it('keeps an E column and labels a cycle that has no E record', () => {
 const items=[{agent:'A',candle_close:'2026-10-27T15:15:00Z',action:'WAIT',outcome:'VALID',rationale:'等待',fills:[]}]
 render(<DecisionLog items={items} agents={[]} cycleEvents={{}} eObservations={{status:'NOT_STARTED',items:[]}} eQuantObservations={{status:'NOT_STARTED',items:[]}} />)
 const log=screen.getByLabelText('交易员实时决策日志')
 expect(log).toHaveTextContent('E · 憨巴')
 expect(log).toHaveTextContent('本周期尚无憨巴记录')
 expect(screen.getAllByText('尚无决策记录')).toHaveLength(2)
 expect(log).not.toHaveTextContent('憨巴记录暂不可用')
})

it('shows a quant observation and its formal fill without folding E into the ABC journal', () => {
 const close='2026-10-27T15:15:00Z'
 render(<DecisionLog items={[{agent:'A',candle_close:close,action:'WAIT',outcome:'VALID',rationale:'等待',fills:[]}]} agents={[]} cycleEvents={{}}
  eObservations={{status:'OBSERVE_ONLY',items:[{close_at:close,status:'OBSERVE_ONLY',result:{decision:{action:'SKIP',reasons:['结构不清']}}}]}}
  eQuantObservations={{status:'SIGNAL_ONLY',items:[{close_at:'2026-10-27T15:15:00+00:00',status:'SETUP_DETECTED',result:{strategy_version:'E-V1.1-QUANT',candidate:{setup:'E2',side:'LONG'},visual_status:'MODEL_UNCERTAIN',risk_preflight:{reason:'RR_BELOW_2'}}}]}}
  eFormal={{status:'READY',fills:[{fill_id:'e1',decision_id:'E-V1.1:2026-10-27T15:15:00+00:00',position_effect:'OPEN',side:'BUY',quantity:'0.02',price:'100'}]}} />)
 const log=screen.getByLabelText('交易员实时决策日志')
 expect(log).toHaveTextContent('LONG · 多头结构')
 expect(log).toHaveTextContent('E2 LONG')
 expect(log).toHaveTextContent('视觉 MODEL_UNCERTAIN')
 expect(log).toHaveTextContent('已开仓 BUY 0.02 @ 100')
 expect(log).toHaveTextContent('憨巴正式账本 · 不计入 ABC')
 expect(log).not.toHaveTextContent('结构不清')
 expect(screen.getByText('A · 趋势结构交易员')).toBeInTheDocument()
})

it('shows an E-only observation cycle and a separate empty label when the feeds have not answered', () => {
 render(<DecisionLog items={[]} agents={[]} cycleEvents={{}}
  eObservations={{status:'OBSERVE_ONLY',items:[{close_at:'2026-10-27T16:15:00Z',status:'OBSERVE_ONLY',result:{decision:{action:'OPEN',side:'SHORT',reasons:['空头结构']}}}]}}
  eQuantObservations={{status:'NOT_STARTED',items:[]}} />)
 expect(screen.getByText('SHORT · 开空')).toBeInTheDocument()
 expect(screen.getByText('空头结构')).toBeInTheDocument()
 expect(screen.getAllByText('尚无决策记录')).toHaveLength(3)
 render(<DecisionLog items={[{agent:'B',candle_close:'2026-10-27T15:15:00Z',action:'HOLD',outcome:'VALID',rationale:'持有',fills:[]}]} agents={[]} cycleEvents={{}} />)
 expect(screen.getByText('憨巴记录暂不可用')).toBeInTheDocument()
})
