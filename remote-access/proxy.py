"""Local-only, fail-closed Cloudflare Access-verified read-only BTC gateway."""
import asyncio, os, re, time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode
import httpx, jwt, websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response

app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
HERE=Path(__file__).resolve().parent
STATIC=(HERE/'dist-current').resolve()
HEALTH_PORT=int(os.environ.get('BTC_PROXY_HEALTH_PORT','18775'))
API='http://127.0.0.1:8011'
READ_PATHS={
 '/api/v1/health','/api/v1/system/status','/api/v1/market/candles',
 '/api/v1/market/sources','/api/v1/market/quote','/api/v1/market/orderbook','/api/v1/market/candles/history',
 '/api/v1/market/snapshot','/api/v1/market/snapshot/image','/api/v1/agents',
 '/api/v1/fills','/api/v1/trades','/api/v1/journal','/api/v1/strategies','/api/v1/statistics/abc',
 '/api/v1/evolutions','/api/v1/audit/events',
 '/api/v1/e/accounts','/api/v1/e/observations','/api/v1/e/quant-observations',
}
READ_PATTERNS=(re.compile(r'^/api/v1/agents/[ABC]/(?:account|decisions)$'),re.compile(r'^/api/v1/strategies/[ABC]/(?:versions|diff)$'),re.compile(r'^/api/v1/trades/[a-zA-Z0-9_-]+$'),re.compile(r'^/api/v1/evolutions/[a-zA-Z0-9_-]+/comparison$'))

async def _local_health_handler(reader, writer):
 try:
  request_line=await asyncio.wait_for(reader.readline(),timeout=1)
  status=b'200 OK' if request_line==b'GET /health HTTP/1.1\r\n' else b'404 Not Found'
  body=b'btc-readonly-proxy-ok' if status.startswith(b'200') else b'not found'
  response=b'HTTP/1.1 '+status+b'\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body
  writer.write(response);await writer.drain()
 except (asyncio.TimeoutError,ConnectionError,OSError):
  pass
 finally:
  writer.close()
  try:await writer.wait_closed()
  except (ConnectionError,OSError):pass

@asynccontextmanager
async def _lifespan(app):
 server=await asyncio.start_server(_local_health_handler,'127.0.0.1',HEALTH_PORT)
 app.state.local_health_server=server
 app.state.local_health_port=server.sockets[0].getsockname()[1]
 try:yield
 finally:
  server.close();await server.wait_closed()

app.router.lifespan_context=_lifespan
def config():
 team=os.environ.get('BTC_ACCESS_TEAM','').strip().lower();aud=os.environ.get('BTC_ACCESS_AUDIENCE','').strip();email=os.environ.get('BTC_ACCESS_EMAIL','').strip().casefold()
 if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}\.cloudflareaccess\.com',team) or not aud or not re.fullmatch(r'[^\s@]+@[^\s@]+',email):return None
 return team,aud,email
@lru_cache(maxsize=8)
def signing_keys(team):
 return jwt.PyJWKClient('https://'+team+'/cdn-cgi/access/certs',timeout=5)
def authenticated(request):
 cfg=config()
 if not cfg:return False
 token=request.headers.get('cf-access-jwt-assertion','')
 if not token:return False
 team,aud,email=cfg
 try:
  key=signing_keys(team).get_signing_key_from_jwt(token).key
  claims=jwt.decode(token,key,algorithms=['RS256'],audience=aud,issuer='https://'+team,options={'require':['exp','iat','iss','aud','email']})
  return isinstance(claims.get('email'),str) and claims['email'].casefold()==email
 except Exception:return False
@app.middleware('http')
async def gate(request,call_next):
 if config() is None:return Response('Access not configured',status_code=503,headers={'Cache-Control':'no-store'})
 if not request.headers.get('cf-access-jwt-assertion'):
  return Response('Forbidden',status_code=403,headers={'Cache-Control':'no-store'})
 if not await asyncio.to_thread(authenticated,request):return Response('Forbidden',status_code=403,headers={'Cache-Control':'no-store'})
 if request.method not in ('GET','HEAD'):return Response('Read-only',status_code=405,headers={'Cache-Control':'no-store','Allow':'GET, HEAD'})
 response=await call_next(request);response.headers['Cache-Control']='no-store';response.headers['X-Content-Type-Options']='nosniff';return response
@app.websocket('/api/v1/events/stream')
async def stream(ws:WebSocket):
 if config() is None or not await asyncio.to_thread(authenticated,ws):await ws.close(code=1008);return
 await ws.accept()
 try:
  async with websockets.connect('ws://127.0.0.1:8011/api/v1/events/stream',proxy=None,open_timeout=5) as upstream:
   # Disconnect at Access token expiration; never forward client frames upstream.
   token=ws.headers['cf-access-jwt-assertion'];expiry=jwt.decode(token,options={'verify_signature':False})['exp'];remaining=max(0,float(expiry)-time.time())
   async with asyncio.timeout(remaining):
    async for message in upstream:await ws.send_text(message) if isinstance(message,str) else await ws.send_bytes(message)
 except (WebSocketDisconnect,ConnectionError,TimeoutError,OSError):pass
 finally:
  try:await ws.close()
  except RuntimeError:pass
def safe_query(route,params):
 if route=='/api/v1/market/candles/history':
  return [(k,v) for k,v in params if k.casefold()!='backfill']+[('backfill','false')]
 return list(params)
@app.api_route('/{path:path}',methods=['GET','HEAD'])
async def read(path:str,request:Request):
 route='/'+path
 if route.startswith('/api/'):
  if route not in READ_PATHS and not any(x.fullmatch(route) for x in READ_PATTERNS):return Response(status_code=404)
  upstream=API+route
 else:
  requested=(STATIC/(path or 'index.html')).resolve()
  if not requested.is_relative_to(STATIC) or not requested.is_file():return Response(status_code=404)
  return FileResponse(requested)
 try:
  async with httpx.AsyncClient(timeout=10,trust_env=False,follow_redirects=False) as client:
   resp=await client.get(upstream,params=safe_query(route,request.query_params.multi_items()))
  return Response(resp.content,status_code=resp.status_code,headers={'Content-Type':resp.headers.get('content-type','application/octet-stream')})
 except httpx.HTTPError:return Response('Local service unavailable',status_code=502)

