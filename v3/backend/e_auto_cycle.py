"""E-only cycle composition, with protection before entries and detached explanation."""
import asyncio
from backend.e_protection import protect_e_accounts
from backend.e_quant_execution import run_quant_with_explanation
from backend.e_structure import process_structure_close


async def run_e_auto_cycle(*,store,bars,close,quote,cost,explain=None,shadow_change=None,strategy_dir=None,allow_short=False):
    account=store.account('E')
    if account['state']!='FLAT':
        def protect_and_manage():
            before=protect_e_accounts({'formal':store.path},quote,cost).get('formal')
            if store.account('E')['state'] not in {'OPEN_LONG','OPEN_SHORT'}:
                return {'status':'POSITION_MANAGED','protection':before}
            structure=process_structure_close(store=store,bars=bars,now=close)
            after=protect_e_accounts({'formal':store.path},quote,cost).get('formal')
            return {'status':'POSITION_MANAGED','protection':before,'structure':structure,'exit':after}
        return await asyncio.to_thread(protect_and_manage)
    return await run_quant_with_explanation(store=store,bars=bars,quote=quote,now=close,cost=cost,explain=explain,shadow_change=shadow_change,strategy_dir=strategy_dir,allow_short=allow_short)
