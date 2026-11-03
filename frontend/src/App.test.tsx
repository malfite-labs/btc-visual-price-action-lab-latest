import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
vi.mock('./Chart', () => ({ default: () => React.createElement('div', null, 'chart') }))
import App from './App'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('workbench safeguards', () => {
  it('does not start overlapping dashboard requests or refetch on quote events', async () => {
    let socket: any
    class FakeSocket {
      onmessage: any
      constructor() { socket = this }
      close() {}
    }
    vi.stubGlobal('WebSocket', FakeSocket)
    const counts: Record<string, number> = {}
    let release!: (value: any) => void
    const slow = new Promise(resolve => { release = resolve })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL): Promise<any> => {
      const url = String(input)
      if (url.includes('/api/v1/')) counts[url] = (counts[url] || 0) + 1
      if (url.includes('/system/status')) await slow
      return { ok: true, status: 200, redirected: false, url, json: async () => url.includes('/system/status') ? { rest_quote: 'PASS' } : url.includes('/agents') ? [] : { items: [] } }
    }))
    vi.useFakeTimers()
    render(<App />)
    await act(async () => {
      for (let i = 0; i < 20; i++) socket.onmessage({ data: JSON.stringify({ type: 'quote', quote: { bid: '10', ask: '12', age_ms: 0 } }) })
      vi.advanceTimersByTime(5000)
      await Promise.resolve()
    })
    expect(Object.entries(counts).find(([url]) => url.includes('/system/status'))?.[1]).toBe(1)
    expect(screen.getByText(/买一 10 \/ 卖一 12/)).toBeInTheDocument()
    await act(async () => { release(null); await Promise.resolve() })
  })
  it('opens the separate BOOK-PA view and returns to the workbench', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({}) })))
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'BOOK-PA 研究' }))
    expect(screen.getByRole('heading', { name: 'BOOK-PA 研究' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '交易工作台' }))
    expect(screen.getByRole('heading', { name: '市场工作台' })).toBeInTheDocument()
  })
  it('renders an explicit research spot label and disables live trading', () => {
    render(<App />)
    expect(screen.getByText(/LIVE TRADING DISABLED/)).toBeInTheDocument()
    expect(screen.getByText(/ALPACA · BTC\/USD 现货 · USD研究计价/)).toBeInTheDocument()
  })

  it('keeps all six work views navigable when the test backend has empty accounts and data', async () => {
    const payloads: Record<string, unknown> = {
      '/system/status': {
        feed_health: 'STALE', rest: 'PASS', rest_quote: 'UNAVAILABLE', websocket: 'RECOVERING',
        vision: { state: 'IMAGE_CAPABILITY_UNVERIFIED', model_name: 'vision-model-test', endpoint_configured: true, credential_present: true, image_capability_verified: false, timeout_seconds: 45, max_concurrency: 3, max_output_tokens: 512, max_requests_per_day: 12, max_call_cost_usd: '0.05', daily_budget_usd: '0.10', input_price_per_million_usd: '0.30', output_price_per_million_usd: '1.20' },
        paper_automation: 'DISABLED_NOT_ARMED',
        stop_monitor: 'QUOTE_EVENT_INTERNAL_PAPER_STOP_MONITOR',
        cost_model: { fee_bps_each_side: '5', maker_reference_rate: '0.0002', adverse_slippage_bps: '2', quote_book_max_age_ms: 60000 },
      },
      '/market/candles': { items: [] },
      '/agents': [],
      '/fills': { items: [] },
      '/trades': { items: [] },
      '/strategies': { items: [] },
      '/evolutions': { items: [] },
      '/journal': { items: [], status: 'NO_DECISIONS_MODEL_NOT_CONFIGURED' },
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL): Promise<any> => {
      const url = String(input)
      const entry = Object.entries(payloads).find(([path]) => url.includes(path))
      return { ok: true, json: async () => entry?.[1] ?? {} }
    }))
    render(<App />)
    await waitFor(() => expect(screen.getByText(/BTC\/USD · 15m/)).toBeInTheDocument())
    for (const [label, heading] of [
      ['AI账户', '四个独立模拟账户'],
      ['交易日记', /交易记录与 AI 日记/],
      ['策略管理', '策略 MD 管理'],
      ['策略进化', '策略进化中心'],
      ['系统状态', '系统状态与配置'],
    ]) {
      fireEvent.click(screen.getByRole('button', { name: label }))
      await waitFor(() => expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument())
      if (label === '系统状态') {
        expect(await screen.findByText(/IMAGE_CAPABILITY_UNVERIFIED/)).toBeInTheDocument()
        expect(screen.getByText(/vision-model-test/)).toBeInTheDocument()
        expect(screen.getByText(/45s · 并发：3/)).toBeInTheDocument()
        expect(screen.getByText(/调用上限：12\/UTC日 · 最大输出：512 tokens/)).toBeInTheDocument()
        expect(screen.getByText(/输入\/输出高峰费率：\$0.30 \/ \$1.20 每百万token/)).toBeInTheDocument()
        expect(screen.getAllByText(/DISABLED_NOT_ARMED/)).toHaveLength(2)
        expect(screen.getByText(/QUOTE_EVENT_INTERNAL_PAPER_STOP_MONITOR/)).toBeInTheDocument()
        expect(screen.getByText(/市价 taker）：5 bp/)).toBeInTheDocument()
        expect(screen.getByText(/Maker 参考费率.*0.0002/)).toBeInTheDocument()
        expect(screen.getByText(/不利滑点：2 bp/)).toBeInTheDocument()
        expect(screen.getByText(/报价与盘口最大年龄：60000 ms/)).toBeInTheDocument()
        expect(screen.getByText(/资金费：暂不计入/)).toBeInTheDocument()
      }
    }
  })

  it('does not convert a noncritical endpoint failure into a global backend outage', async () => {
    const payloads: Record<string, unknown> = {
      '/system/status': { feed_health: 'LIVE', rest: 'PASS', rest_quote: 'PASS', quote: { bid: 10, ask: 12, age_ms: 100 }, candles_count: 1 },
      '/market/candles': { items: [{ open_at: '2026-10-29T00:00:00Z', close_at: '2026-10-29T00:15:00Z', is_closed: true, open: 10, high: 12, low: 9, close: 11 }] },
      '/fills': { items: [] }, '/trades': { items: [] }, '/strategies': { items: [] }, '/evolutions': { items: [] }, '/journal': { items: [] },
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/agents')) return { ok: false, status: 502, redirected: false, url }
      if (url.includes('/research/watchdog-status.json')) return { ok: false, status: 404, json: async (): Promise<any> => null }
      const entry = Object.entries(payloads).find(([path]) => url.includes(path))
      return { ok: true, status: 200, redirected: false, url, json: async (): Promise<any> => entry?.[1] ?? {} }
    }))
    render(<App />)
    await waitFor(() => expect(screen.getByText(/BTC\/USD · 15m/)).toBeInTheDocument())
    expect(screen.queryByText(/后端连接异常：API_502/)).not.toBeInTheDocument()
    expect(screen.getByText(/最后收盘/)).toHaveTextContent('2026-10-29 08:15:00')
    expect(screen.getAllByText(/北京时间 UTC\+8/).length).toBeGreaterThan(0)
  })

  it('waits for the current refresh before starting the next one', async () => {
    vi.useFakeTimers()
    let finishFirst!: (value: any) => void
    const first = new Promise<any>(resolve => { finishFirst = resolve })
    let statusCalls = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL): Promise<any> => {
      const url = String(input)
      if (url.includes('/system/status')) {
        statusCalls += 1
        const payload = statusCalls === 1 ? first : Promise.resolve({ feed_health: 'NEW_RESPONSE' })
        return { ok: true, status: 200, redirected: false, url, json: async (): Promise<any> => payload }
      }
      if (url.includes('/research/watchdog-status.json')) return { ok: false, status: 404, json: async (): Promise<any> => null }
      return { ok: true, status: 200, redirected: false, url, json: async (): Promise<any> => url.includes('/journal') ? { items: [] } : url.includes('/market/candles') || url.includes('/fills') || url.includes('/trades') || url.includes('/strategies') || url.includes('/evolutions') ? { items: [] } : [] }
    }))
    render(<App />)
    await act(async () => { vi.advanceTimersByTime(5000); await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    expect(statusCalls).toBe(1)
    await act(async () => { finishFirst({ feed_health: 'OLD_RESPONSE' }); await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    await act(async () => { vi.advanceTimersByTime(5000); await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    expect(screen.getByText('NEW_RESPONSE')).toBeInTheDocument()
  })

  it('shows Hanba E as a fourth account, legend entry and formal fill', async () => {
    const formal = {
      status: 'READY', equity: '10100.5', unrealized_pnl: '12.5',
      account: {state: 'OPEN_LONG', position_json: JSON.stringify({direction: 'LONG', quantity: '0.02', entry_price: '100', stop_price: '90'})},
      fills: [{fill_id: 'e1', agent: 'E', side: 'BUY', quantity: '0.02', price: '100', fee: '0.1', slippage: '0', occurred_at: '2026-10-29T00:16:00Z', position_effect: 'OPEN'}],
    }
    const payloads: Record<string, unknown> = {
      '/system/status': {feed_health: 'LIVE', rest: 'PASS', rest_quote: 'PASS', quote: {bid: 10, ask: 12, age_ms: 10}, candles_count: 1},
      '/market/candles': {items: [{open_at: '2026-10-29T00:00:00Z', close_at: '2026-10-29T00:15:00Z', is_closed: true, open: 10, high: 12, low: 9, close: 11}]},
      '/agents': [{agent: 'A', color: '#e2555b', status: 'RULE_DRIVEN', account: {state: 'FLAT', metrics: {equity: 10000, unrealized_pnl: 0, available_cash: 10000}}, strategy: {current_version: 'v1.0.0'}}],
      '/fills': {items: [{fill_id: 'a1', agent: 'A', side: 'BUY', quantity: '1', price: '10', fee: '0', slippage: '0', occurred_at: '2026-10-29T00:16:00Z'}]},
      '/trades': {items: []},
      '/strategies': {items: [{agent: 'E', status: 'AUTO_PAPER', current_version: 'E-V1.0', archived_version: 'E-V1.0', candidate_version: 'E-V1.1-QUANT', candidate_status: 'AUTO_PAPER', runtime_mode: 'AUTO_PAPER', driving_version: 'E-V1.1-QUANT', core_lock: 'PROTECTED', versions: []}]},
      '/evolutions': {items: []},
      '/journal': {items: []},
      '/e/accounts': {runtime: {mode: 'AUTO_PAPER'}, formal, shadow: {status: 'READY', account: {state: 'FLAT'}}},
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const entry = Object.entries(payloads).find(([path]) => url.includes(path))
      return {ok: true, status: 200, redirected: false, url, json: async () => entry?.[1] ?? {}}
    }))
    render(<App />)
    await waitFor(() => expect(screen.getByText('LONG / 0.02')).toBeInTheDocument())
    expect(screen.getByText(/● E 憨巴/)).toBeInTheDocument()
    expect(screen.getAllByLabelText('憨巴E账户').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', {name: '交易日记'}))
    await waitFor(() => expect(screen.getByRole('option', {name: 'E'})).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('交易员'), {target: {value: 'E'}})
    expect(screen.getByText('憨巴正式')).toBeInTheDocument()
    expect(screen.queryByText('a1')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', {name: '策略管理'}))
    expect(await screen.findByText('E策略 · AUTO_PAPER')).toBeInTheDocument()
    expect(screen.getByText('驱动版本：E-V1.1-QUANT')).toBeInTheDocument()
    expect(screen.queryByText(/CANDIDATE_NOT_ACTIVE/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', {name: 'AI账户'}))
    expect(screen.getByRole('heading', {name: '四个独立模拟账户'})).toBeInTheDocument()
    expect(screen.getAllByLabelText('憨巴E账户').length).toBeGreaterThan(0)
  })
})
