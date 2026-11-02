import { get } from './api'

export const dashboardResources = {
  status: '/system/status', candles: '/market/candles?limit=672', agents: '/agents',
  fills: '/fills', trades: '/trades', strategies: '/strategies', evolutions: '/evolutions', journal: '/journal',
} as const

export type ResourceHealth = { state: 'OK' | 'ERROR'; lastSuccessAt?: string; lastFailureAt?: string; code?: string }
export type DashboardResult = { values: Partial<Record<keyof typeof dashboardResources, any>>; health: Record<string, ResourceHealth> }

export async function refreshDashboard(
  previous: Record<string, ResourceHealth>,
  fetcher: (path: string) => Promise<any> = get,
): Promise<DashboardResult> {
  const values: DashboardResult['values'] = {}
  const health = { ...previous }
  await Promise.all(Object.entries(dashboardResources).map(async ([name, path]) => {
    try {
      const value = await fetcher(path)
      values[name as keyof typeof dashboardResources] = value
      health[name] = { ...health[name], state: 'OK', lastSuccessAt: new Date().toISOString(), code: undefined }
    } catch (error) {
      const code = typeof error === 'object' && error && 'code' in error ? String(error.code) : 'UNKNOWN_ERROR'
      health[name] = { ...health[name], state: 'ERROR', lastFailureAt: new Date().toISOString(), code }
    }
  }))
  return { values, health }
}
