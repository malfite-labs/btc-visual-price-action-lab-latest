import importlib.util, pathlib
from fastapi.testclient import TestClient
spec=importlib.util.spec_from_file_location('remote_proxy',pathlib.Path(__file__).parents[1]/'proxy.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
import pytest
@pytest.fixture(autouse=True)
def use_ephemeral_health_port(monkeypatch):
 monkeypatch.setattr(p,'HEALTH_PORT',0)
def test_no_access_configuration_fails_closed(monkeypatch):
 for k in ('BTC_ACCESS_TEAM','BTC_ACCESS_AUDIENCE','BTC_ACCESS_EMAIL'):monkeypatch.delenv(k,raising=False)
 c=TestClient(p.app)
 assert c.get('/').status_code==503
 assert c.get('/api/v1/agents').status_code==503
 assert c.post('/api/v1/strategies/C/import',json={}).status_code in (403,405,503)

def test_missing_access_token_is_rejected_without_blocking_auth_worker(monkeypatch):
 monkeypatch.setattr(p,'config',lambda:('test.cloudflareaccess.com','aud','admin@example.com'))
 def validator_must_not_run(req): raise AssertionError('No-token health/access request must not enter JWT validation worker')
 monkeypatch.setattr(p,'authenticated',validator_must_not_run)
 c=TestClient(p.app)
 assert c.get('/').status_code==403

def test_local_health_listener_is_loopback_only_and_not_public_route(monkeypatch):
 import urllib.request
 monkeypatch.setattr(p,'config',lambda:('test.cloudflareaccess.com','aud','admin@example.com'))
 with TestClient(p.app) as c:
  server=p.app.state.local_health_server
  assert {sock.getsockname()[0] for sock in server.sockets}=={'127.0.0.1'}
  with urllib.request.urlopen(f"http://127.0.0.1:{p.app.state.local_health_port}/health",timeout=2) as response:
   assert response.status==200
   assert response.read()==b'btc-readonly-proxy-ok'
  assert c.get('/_local/health').status_code==403

def test_research_json_is_served_from_proxy_static_root(monkeypatch,tmp_path):
 monkeypatch.setattr(p,'config',lambda:('test.cloudflareaccess.com','aud','admin@example.com'))
 monkeypatch.setattr(p,'authenticated',lambda req:True)
 research=tmp_path/'research';research.mkdir();(research/'watchdog-status.json').write_text('{"state":"test"}',encoding='utf-8')
 monkeypatch.setattr(p,'STATIC',tmp_path.resolve())
 with TestClient(p.app) as c:
  r=c.get('/research/watchdog-status.json',headers={'cf-access-jwt-assertion':'test-token'})
  assert r.status_code==200
  assert r.json()=={'state':'test'}

def test_current_static_build_serves_latest_index_and_assets(monkeypatch):
 import re
 monkeypatch.setattr(p,'config',lambda:('test.cloudflareaccess.com','aud','admin@example.com'))
 monkeypatch.setattr(p,'authenticated',lambda req:True)
 with TestClient(p.app) as c:
  response=c.get('/',headers={'cf-access-jwt-assertion':'test-token'})
  assert response.status_code==200
  html=response.text
  assert 'index-BEewiTT-.js' in html
  for asset in re.findall(r'(?:src|href)="([^"]+)"',html):
   served=c.get(asset,headers={'cf-access-jwt-assertion':'test-token'})
   assert served.status_code==200
  assert 'Asia/Shanghai' in (p.STATIC/'assets'/'index-BEewiTT-.js').read_text(encoding='utf-8')
def test_forbids_writes_even_with_valid_identity(monkeypatch):
 monkeypatch.setattr(p,'config',lambda:('test.cloudflareaccess.com','aud','admin@example.com'))
 monkeypatch.setattr(p,'authenticated',lambda req:True)
 c=TestClient(p.app)
 headers={'cf-access-jwt-assertion':'test-token'}
 assert c.post('/api/v1/strategies/C/import',headers=headers,json={}).status_code==405
 assert c.delete('/api/v1/agents/A/account',headers=headers).status_code==405
 assert c.get('/api/v1/unknown',headers=headers).status_code==404

def test_signed_jwt_identity_and_audience(monkeypatch):
 import time,jwt
 from types import SimpleNamespace
 from cryptography.hazmat.primitives.asymmetric import rsa
 private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 monkeypatch.setattr(p,'config',lambda:('lab.cloudflareaccess.com','test-aud','admin@example.com'))
 monkeypatch.setattr(p,'signing_keys',lambda team:SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private.public_key())))
 claims={'iss':'https://lab.cloudflareaccess.com','aud':'test-aud','iat':int(time.time()),'exp':int(time.time())+60,'email':'admin@example.com'}
 def req(d):return SimpleNamespace(headers={'cf-access-jwt-assertion':jwt.encode(d,private,algorithm='RS256')})
 assert p.authenticated(req(claims)) is True
 assert p.authenticated(req({**claims,'email':'outsider@example.com'})) is False
 assert p.authenticated(req({**claims,'aud':'wrong'})) is False
 assert p.authenticated(req({**claims,'exp':int(time.time())-1})) is False
 assert p.authenticated(SimpleNamespace(headers={})) is False

def test_websocket_rejects_without_access_config(monkeypatch):
 import pytest
 from starlette.websockets import WebSocketDisconnect
 monkeypatch.setattr(p,'config',lambda:None)
 with TestClient(p.app) as c:
  with pytest.raises(WebSocketDisconnect):
   with c.websocket_connect('/api/v1/events/stream'):pass

def test_readonly_gateway_with_real_local_backend(monkeypatch):
 monkeypatch.setattr(p,'config',lambda:('lab.cloudflareaccess.com','aud','admin@example.com'))
 monkeypatch.setattr(p,'authenticated',lambda req:True)
 with TestClient(p.app) as c:
  headers={'cf-access-jwt-assertion':'test-token'}
  assert c.get('/',headers=headers).status_code==200
  assert c.get('/api/v1/health',headers=headers).json()['status']=='ok'
  assert c.get('/api/v1/agents',headers=headers).status_code==200
  assert c.get('/api/v1/statistics/abc',headers=headers).status_code==200
  assert c.post('/api/v1/strategies/C/import',headers=headers,json={'content':'x'}).status_code==405

def test_readonly_websocket_relay_against_real_local_backend(monkeypatch):
 import time,jwt
 monkeypatch.setattr(p,'config',lambda:('lab.cloudflareaccess.com','aud','admin@example.com'))
 monkeypatch.setattr(p,'authenticated',lambda req:True)
 token=jwt.encode({'exp':int(time.time())+5},'testing-only',algorithm='HS256')
 with TestClient(p.app) as c:
  with c.websocket_connect('/api/v1/events/stream',headers={'cf-access-jwt-assertion':token}) as ws:
   assert ws.receive_json()['type']=='status'

def test_signed_access_allows_only_approved_email_and_read_only(monkeypatch):
 import time,jwt
 from types import SimpleNamespace
 from cryptography.hazmat.primitives.asymmetric import rsa
 key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 monkeypatch.setattr(p,'config',lambda:('lab.cloudflareaccess.com','aud','admin@example.com'))
 monkeypatch.setattr(p,'signing_keys',lambda team:SimpleNamespace(get_signing_key_from_jwt=lambda token:SimpleNamespace(key=key.public_key())))
 claims={'iss':'https://lab.cloudflareaccess.com','aud':'aud','iat':int(time.time()),'exp':int(time.time())+60,'email':'admin@example.com'}
 token=jwt.encode(claims,key,algorithm='RS256');bad=jwt.encode({**claims,'email':'other@example.com'},key,algorithm='RS256')
 c=TestClient(p.app);headers={'cf-access-jwt-assertion':token}
 assert c.get('/',headers=headers).status_code==200
 assert c.get('/api/v1/agents',headers=headers).status_code==200
 assert c.get('/api/v1/agents',headers={'cf-access-jwt-assertion':bad}).status_code==403
 assert c.post('/api/v1/strategies/C/import',headers=headers,json={'content':'x'}).status_code==405

def test_history_backfill_is_forced_read_only(monkeypatch):
 monkeypatch.setattr(p,'config',lambda:('lab.cloudflareaccess.com','aud','admin@example.com'))
 monkeypatch.setattr(p,'authenticated',lambda req:True)
 assert p.safe_query('/api/v1/market/candles/history', [('start','2026-10-20T00:00:00Z'),('backfill','true'),('backfill','1')]) == [('start','2026-10-20T00:00:00Z'),('backfill','false')]
 c=TestClient(p.app)
 r=c.get('/api/v1/market/candles/history',headers={'cf-access-jwt-assertion':'test-token'},params={'start':'2026-10-20T00:00:00Z','end':'2026-10-20T01:00:00Z','backfill':'true'})
 assert r.status_code==200
def test_e_read_gets_pass_whitelist_other_e_paths_and_writes_stay_denied(monkeypatch):
 import httpx
 monkeypatch.setattr(p,'config',lambda:('test.cloudflareaccess.com','aud','admin@example.com'))
 monkeypatch.setattr(p,'authenticated',lambda req:True)
 seen=[]
 class FakeClient:
  def __init__(self,**kw):pass
  async def __aenter__(self):return self
  async def __aexit__(self,*a):pass
  async def get(self,url,params=None):
   seen.append(url)
   return httpx.Response(200,content=b'{}',headers={'content-type':'application/json'},request=httpx.Request('GET',url))
 monkeypatch.setattr(p.httpx,'AsyncClient',FakeClient)
 allowed=('/api/v1/e/accounts','/api/v1/e/observations','/api/v1/e/quant-observations')
 assert set(allowed)<=p.READ_PATHS
 assert not any(pattern.fullmatch(route) for route in allowed for pattern in p.READ_PATTERNS)
 c=TestClient(p.app)
 headers={'cf-access-jwt-assertion':'test-token'}
 for path in allowed:
  seen.clear();response=c.get(path,headers=headers)
  assert response.status_code==200 and seen==[p.API+path]
 denied=('/api/v1/e/accounts/extra','/api/v1/e/secret','/api/v1/e/observations/','/api/v1/e/quant-observations/raw','/api/v1/e')
 for path in denied:
  seen.clear()
  assert c.get(path,headers=headers).status_code==404 and seen==[]
 for method in (c.post,c.put,c.delete):
  seen.clear()
  assert method('/api/v1/e/accounts',headers=headers).status_code==405 and seen==[]
 assert c.get('/api/v1/e/accounts').status_code==403
 assert c.get('/api/v1/statistics/abc').status_code==403
 monkeypatch.setattr(p,'authenticated',lambda req:False)
 assert c.get('/api/v1/e/observations',headers=headers).status_code==403 and seen==[]
 monkeypatch.setattr(p,'config',lambda:None)
 assert c.get('/api/v1/e/quant-observations',headers=headers).status_code==503
