from scripts.evolution_v23_guard import fingerprint, behavior_diff, cache_eligible

def test_fingerprint_normalizes_conditions_and_numeric():
 a={'long':[{'x':1},{'y':2}], 'short':[{'a':3}],'stop_bars':4,'target_r':2,'max_hold':12}
 b={'long':[{'y':2},{'x':1.0}], 'short':[{'a':3}],'stop_bars':4,'target_r':2.0,'max_hold':12}
 assert fingerprint(a)==fingerprint(b)
def test_cache_rejects_unverified_conditions():
 assert cache_eligible({'rule':'x','data':'y','cost':'z','window':'a','engine':'b','agent':'A','mode':'train'},{'rule':'x','data':'y','cost':'z','window':'a','engine':'b','agent':'A','mode':'train'})
 assert not cache_eligible({'rule':'x','data':'y'},{'rule':'x','data':'y'})
def test_no_effect_needs_all_behavior_equal():
 x={'signals':[{'agent':'A','time':'t','direction':'LONG','stop':'3'}],'trades':[{'agent':'A','signal_time':'t','entry_time':'t2','direction':'LONG','stop':'3','exit_bar_close':'t3','exit_reason':'STOP'}]}
 assert behavior_diff(x,x,'A')['classification']=='NO_EFFECT'
 y={'signals':x['signals'],'trades':[dict(x['trades'][0],exit_reason='TARGET')]}
 assert behavior_diff(x,y,'A')['classification']=='EFFECTIVE'

def test_cache_requires_agent_and_mode_too():
 a={'rule':'x','data':'y','cost':'z','window':'a','engine':'b','agent':'A','mode':'train'}
 b=dict(a,agent='B')
 assert not cache_eligible(a,b)

def test_cache_fails_closed_on_tampered_result(tmp_path):
 import json,hashlib
 from scripts.evolution_v23_guard import find_cache
 key={'rule':'x','data':'y','cost':'z','window':'a','engine':'b','agent':'A','mode':'validation-v30'}
 r=tmp_path/'results-validation-v30.json';l=tmp_path/'ledger-validation-v30.sqlite3';r.write_text('result');l.write_text('ledger')
 digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
 (tmp_path/'v23-cache.json').write_text(json.dumps({'key':key,'result_sha256':digest(r),'ledger_sha256':digest(l)}))
 assert find_cache(tmp_path,key)==tmp_path
 r.write_text('changed')
 assert find_cache(tmp_path,key) is None
