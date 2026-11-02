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
