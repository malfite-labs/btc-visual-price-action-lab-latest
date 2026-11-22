import {render,screen,fireEvent} from '@testing-library/react'
import {it,expect} from 'vitest'
import CounterfactualView from './CounterfactualView'
it('keeps formal and counterfactual historical statistics distinct',()=>{
 const data={strategy:'C V3.0',realized_pnl:'0.25',as_of_utc:'2026-10-28T12:15:12Z',equity_events:[{time:'2026-10-28T12:00:00Z',formal:'10000.00',scenario:'10000.00',description:'信号前'}]}
 render(<CounterfactualView data={data}/>);expect(screen.getByRole('button',{name:'原始正式历史'})).toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'追溯补录情景'}));expect(screen.getByText(/反事实/)).toBeInTheDocument();expect(screen.getByText(/0.25/)).toBeInTheDocument()
})
