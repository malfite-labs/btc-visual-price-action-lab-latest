import { describe, expect, it } from 'vitest'
import { refreshDashboard } from './dashboardRefresh'

describe('independent dashboard resource health', () => {
  it('retains successful modules when one noncritical request fails and records both timestamps', async () => {
    const result = await refreshDashboard({}, async path => {
      if (path === '/agents') throw Object.assign(new Error('bad gateway'), { code: 'HTTP_502' })
      if (path === '/system/status') return { feed_health: 'LIVE' }
      if (path.startsWith('/market/candles')) return { items: [{ close: 1 }] }
      return []
    })
    expect(result.values.status).toEqual({ feed_health: 'LIVE' })
    expect(result.values.candles?.items).toHaveLength(1)
    expect(result.health.status.state).toBe('OK')
    expect(result.health.agents).toMatchObject({ state: 'ERROR', code: 'HTTP_502' })
    expect(result.health.status.lastSuccessAt).toBeTruthy()
    expect(result.health.agents.lastFailureAt).toBeTruthy()
  })

  it('clears the current error after that module recovers while preserving failure history', async () => {
    const failed = await refreshDashboard({}, async path => { if (path === '/system/status') throw Object.assign(new Error('offline'), { code: 'HTTP_502' }); return {} })
    const recovered = await refreshDashboard(failed.health, async () => ({ feed_health: 'LIVE' }))
    expect(recovered.health.status.state).toBe('OK')
    expect(recovered.health.status.lastFailureAt).toBe(failed.health.status.lastFailureAt)
    expect(recovered.health.status.lastSuccessAt).toBeTruthy()
  })
})
