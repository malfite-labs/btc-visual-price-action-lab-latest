import {render,screen} from '@testing-library/react'
import {it,expect} from 'vitest'
import RecoveryStatus from './RecoveryStatus'
it('shows pending stop without claiming the position is closed',()=>{
 render(<RecoveryStatus data={{automatic:true,state:'RECOVER_PENDING_EXITS',backend:true,feed:'LIVE',scheduler:'ENABLED_INTERNAL_PAPER',shadow:'RUNNING',pending_exit:true,updated_utc:new Date().toISOString()}}/>);expect(screen.getByText(/模拟持仓待止损退出/)).toBeInTheDocument()
})
it('does not label a stale watchdog snapshot as current system failure',()=>{
 render(<RecoveryStatus data={{automatic:true,backend:false,feed:'DISCONNECTED',scheduler:'DISABLED',shadow:'BLOCKED',updated_utc:new Date(Date.now()-180000).toISOString()}}/>);expect(screen.getByText(/后端：未知（快照已过期）/)).toBeInTheDocument();expect(screen.queryByText(/后端：异常/)).not.toBeInTheDocument();expect(screen.getByText(/不代表后端、行情或调度器当前异常/)).toBeInTheDocument()
})
