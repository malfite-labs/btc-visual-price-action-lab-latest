def test_pending_exit_blocks_new_cycles():
 from backend.recovery_guard import recovery_blocked
 class L:
  def account(self,a):return {'state':{'A':'FLAT','B':'STOP_EXIT_PENDING','C':'OPEN_SHORT'}[a]}
 assert recovery_blocked(L())
 class Ready:
  def account(self,a):return {'state':'OPEN_SHORT' if a=='C' else 'FLAT'}
 assert not recovery_blocked(Ready())
def test_b_pending_isolated_from_healthy_a_c():
 from backend.recovery_guard import recovery_blocked
 class L:
  def account(self,a):
   if a=='B':return {'state':'STOP_EXIT_PENDING','position_json':'{"trade_id":"b","quantity":"1","direction":"LONG"}','pending_stop_json':'{"event_key":"stop"}'}
   return {'state':'FLAT','position_json':None,'pending_stop_json':None}
 assert not recovery_blocked(L())

def test_other_account_meta_mismatch_fails_closed():
 from backend.recovery_guard import recovery_blocked
 class L:
  def account(self,a):
   if a=='C':return {'state':'OPEN_SHORT','position_json':'{"trade_id":"c","quantity":"1","stop_price":"101"}'}
   return {'state':'FLAT'}
 class R:
  def _meta(self,a):return ('decision','different-trade')
 assert recovery_blocked(L(),R())
