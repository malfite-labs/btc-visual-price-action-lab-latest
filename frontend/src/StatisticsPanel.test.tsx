import {render,screen,fireEvent} from '@testing-library/react'
import {it,expect,vi} from 'vitest'
import {StrictMode} from 'react'
import StatisticsPanel from './StatisticsPanel'
it('does not duplicate a pending statistics request in StrictMode',()=>{
 const fetcher=vi.fn(()=>new Promise<Response>(()=>{}));vi.stubGlobal('fetch',fetcher)
 const page=render(<StrictMode><StatisticsPanel/></StrictMode>)
 expect(fetcher).toHaveBeenCalledTimes(1)
 page.unmount();vi.unstubAllGlobals()
})
it('separates realized and floating, pending stop and 40 trade shadow progress',()=>{
 const stats={updated_at:'2026-10-28T14:00:00Z',agents:Object.fromEntries(['A','B','C'].map(a=>[a,{equity:'10000',realized_net_pnl:'-10',unrealized_pnl:'3',closed_trades:1,max_drawdown_pct:'0.1',fees:'2',slippage:'1',state:a==='B'?'STOP_EXIT_PENDING':'FLAT',shadow:{closed_since_review:3,version:'V3'},position:a==='B'?{direction:'LONG',quantity:'1',entry_price:'100',stop_price:'99'}:null}]))}
 render(<StatisticsPanel initial={stats}/>);expect(screen.getAllByText(/3\/40/)).toHaveLength(3);expect(screen.getByText(/STOP_EXIT_PENDING/)).toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'B 统计详情'}));expect(screen.getByText(/未实现浮动/)).toBeInTheDocument()
})
