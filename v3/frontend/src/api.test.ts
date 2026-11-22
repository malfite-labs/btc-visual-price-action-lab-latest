import { afterEach, describe, expect, it, vi } from 'vitest'
import { API, get, getWebSocketUrl } from './api'

afterEach(() => vi.unstubAllGlobals())

describe('same-origin API and clear failure classification', () => {
  it('uses the current origin for API and secure WebSocket URLs', () => {
    expect(API).toBe(`${window.location.origin}/api/v1`)
    expect(getWebSocketUrl()).toBe(`ws://${window.location.host}/api/v1/events/stream`)
  })

  it('reports HTTP 502 as an upstream failure', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 502, redirected: false, url: `${window.location.origin}/api/v1/agents` })))
    await expect(get('/agents')).rejects.toMatchObject({ code: 'HTTP_502' })
  })

  it('recognizes a Cloudflare Access login redirect instead of reporting 502', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, redirected: true, url: 'https://team.cloudflareaccess.com/cdn-cgi/access/login' })))
    await expect(get('/agents')).rejects.toMatchObject({ code: 'ACCESS_SESSION_EXPIRED' })
  })

  it('distinguishes network and malformed JSON failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch')))
    await expect(get('/agents')).rejects.toMatchObject({ code: 'NETWORK_ERROR' })
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, redirected: false, url: `${API}/agents`, json: async () => { throw new SyntaxError('bad json') } })))
    await expect(get('/agents')).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })
})

it('reads BOOK-PA from the existing read-only backend endpoint', async () => {
  const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ status: 'NO_RESEARCH_RESULTS' }) }))
  vi.stubGlobal('fetch', fetchMock)
  const { getBookPaResearch } = await import('./api')
  expect(await getBookPaResearch()).toEqual({ status: 'NO_RESEARCH_RESULTS' })
  expect(fetchMock).toHaveBeenCalledWith(`${API}/research/book-pa/latest`, { cache: 'no-store', redirect: 'follow' })
})
