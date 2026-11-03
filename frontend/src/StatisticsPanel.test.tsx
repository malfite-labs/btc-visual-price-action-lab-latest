import {cleanup, render,screen,fireEvent} from '@testing-library/react'
import {afterEach, it,expect,vi} from 'vitest'
import {StrictMode} from 'react'
import StatisticsPanel from './StatisticsPanel'
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
it('does not duplicate a pending statistics request in StrictMode',()=>{
 const fetcher=vi.fn(()=>new Promise<Response>(()=>{}));vi.stubGlobal('fetch',fetcher)
 const page=render(<StrictMode><StatisticsPanel/></StrictMode>)
 expect(fetcher).toHaveBeenCalledTimes(1)
 page.unmount();vi.unstubAllGlobals()
})
it('separates realized and floating, pending stop and 40 trade shadow progress',()=>{
 const stats={updated_at:'2026-10-28T14:00:00Z',agents:Object.fromEntries(['A','B','C'].map(a=>[a,{equity:'10000',realized_net_pnl:'-10',unrealized_pnl:'3',closed_trades:1,max_drawdown_pct:'0.1',fees:'2',slippage:'1',state:a==='B'?'STOP_EXIT_PENDING':'FLAT',shadow:{closed_since_review:3,version:'V3'},position:a==='B'?{direction:'LONG',quantity:'1',entry_price:'100',stop_price:'99'}:null}]))}
 render(<StatisticsPanel initial={stats}/>);expect(screen.getAllByText(/3\/40/)).toHaveLength(3);expect(screen.getByText(/STOP_EXIT_PENDING/)).toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'B 统计详情'}));expect(screen.getByText(/未实现浮动/)).toBeInTheDocument()
 expect(screen.getByRole('heading',{name:'实时统计'})).toBeInTheDocument()
 expect(screen.queryByRole('heading',{name:'ABC 实时统计'})).not.toBeInTheDocument()
 expect(screen.getByLabelText('憨巴正式账本统计')).toHaveTextContent('憨巴正式账本暂不可用')
})
it('shows a formal E card beside ABC and leaves the shadow book out of it',()=>{
 const stats={updated_at:'2026-10-28T14:00:00Z',agents:{A:{equity:'10000',realized_net_pnl:'0',state:'FLAT',shadow:{closed_since_review:1}}}}
 const formal={status:'READY',equity:'10100.5',realized_pnl:'15',unrealized_pnl:'2.5',closed_trades:2,win_rate:0.5,next_review_at:40,account:{state:'OPEN_LONG',cash:'9900',position_json:JSON.stringify({direction:'LONG',quantity:'0.02',entry_price:'100',stop_price:'90'})},fills:[{fill_id:'e1',side:'BUY',quantity:'0.02',price:'100',fee:'0.40',slippage:'0.10',occurred_at:'2026-10-29T00:16:00Z',position_effect:'OPEN'}]}
 render(<StatisticsPanel initial={stats} eAccounts={{formal,shadow:{status:'READY',equity:'99999',realized_pnl:'888',account:{state:'OPEN_SHORT'}}}}/>)
 const card=screen.getByLabelText('憨巴正式账本统计')
 expect(card).toHaveTextContent('E · OPEN_LONG')
 expect(card).toHaveTextContent('憨巴正式账本 · 不计入 ABC')
 expect(card).toHaveTextContent('$10100.50')
 expect(card).toHaveTextContent('$15.00')
 expect(card).toHaveTextContent('闭合 2 笔')
 expect(card).toHaveTextContent('50.0%')
 expect(card).not.toHaveTextContent('99999')
 expect(card).not.toHaveTextContent('影子前推')
 expect(screen.getByText(/1\/40/)).toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'E 统计详情'}))
 expect(card).toHaveTextContent('LONG / 0.02')
 expect(card).toHaveTextContent('100 / 90')
 expect(card).toHaveTextContent('$0.40')
 expect(card).toHaveTextContent('$0.10')
 expect(card).toHaveTextContent('已实现回撤：接口未提供')
 expect(card).toHaveTextContent('不含影子前推')
})
it('still shows the E card when the formal book is uninitialized',()=>{
 const stats={updated_at:'2026-10-28T14:00:00Z',agents:{A:{equity:'10000',realized_net_pnl:'0',state:'FLAT',shadow:{closed_since_review:0}}}}
 render(<StatisticsPanel initial={stats} eAccounts={{formal:{status:'NOT_INITIALIZED'},shadow:{status:'READY',equity:'99999'}}}/>)
 const card=screen.getByLabelText('憨巴正式账本统计')
 expect(card).toHaveTextContent('正式账本未初始化')
 expect(card).not.toHaveTextContent('99999')
})
it('polls only ABC statistics when the E card is already fed by accounts',()=>{
 const fetcher=vi.fn((_input: RequestInfo | URL)=>new Promise<Response>(()=>{}));vi.stubGlobal('fetch',fetcher)
 const page=render(<StatisticsPanel eAccounts={{formal:{status:'READY',equity:'100',realized_pnl:'1',unrealized_pnl:'0',closed_trades:0,win_rate:null,account:{state:'FLAT',cash:'100',position_json:null},fills:[]}}}/>)
 expect(fetcher).toHaveBeenCalledTimes(1)
 expect(String(fetcher.mock.calls[0][0])).toContain('/statistics/abc')
 expect(String(fetcher.mock.calls[0][0])).not.toContain('/e/')
 expect(screen.getByLabelText('憨巴正式账本统计')).toHaveTextContent('$100.00')
 expect(screen.getByText('A/B/C 统计暂不可用，E 仍单独显示。')).toBeInTheDocument()
 page.unmount();vi.unstubAllGlobals()
})
