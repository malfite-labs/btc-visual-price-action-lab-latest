export const API = import.meta.env.VITE_API_BASE || `${window.location.origin}/api/v1`

export async function getBookPaResearch() {
  return get('/research/book-pa/latest')
}

export class ApiError extends Error {
  constructor(public code: string, message = code) { super(message); this.name = 'ApiError' }
}

export function getWebSocketUrl(): string {
  const url = new URL('/api/v1/events/stream', window.location.href)
  url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

export async function get(path: string) {
  let res: Response
  try { res = await fetch(`${API}${path}`, { cache: 'no-store', redirect: 'follow' }) }
  catch { throw new ApiError('NETWORK_ERROR', '网络连接失败') }
  if (res.redirected && (/\/cdn-cgi\/access\/login/i.test(res.url) || /cloudflareaccess\.com/i.test(new URL(res.url).hostname))) {
    throw new ApiError('ACCESS_SESSION_EXPIRED', 'Cloudflare Access 会话已过期，请重新登录')
  }
  if (!res.ok) throw new ApiError(`HTTP_${res.status}`, `HTTP ${res.status}`)
  try { return await res.json() }
  catch { throw new ApiError('INVALID_RESPONSE', '服务响应格式无效') }
}
