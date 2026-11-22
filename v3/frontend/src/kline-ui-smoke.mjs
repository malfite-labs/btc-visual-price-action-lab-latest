import { chromium } from 'playwright-core'
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'

// Run against a Vite preview on 5186. All API calls are fulfilled locally.
const browser=await chromium.launch({channel:'msedge',headless:true})
try {
  const page=await browser.newPage({viewport:{width:1440,height:900}})
  const output=process.env.KLINE_SMOKE_SCREENSHOTS
  if(output) await mkdir(output,{recursive:true})
  async function capture(name) {
    if(output) await page.screenshot({path:join(output,name+'.png')})
  }
  const errors=[]
  page.on('pageerror',error=>errors.push(error.message))
  const time=Date.UTC(2026,10,22,5)
  await page.route('**/api/v1/**',route=>{
    const url=new URL(route.request().url())
    if(url.pathname.endsWith('/events'))return route.fulfill({json:{items:[
      {event_id:'fixture-1',strategy_id:'D',trade_id:'fixture',occurred_at:new Date(time+60000).toISOString(),position_effect:'PARTIAL_CLOSE',position_direction:'LONG',quantity:'0.1',price:'61000',reason_text:'隔离演示数据',verification_status:'VERIFIED',source:'TEST_ONLY'},
      {event_id:'fixture-2',strategy_id:'G',trade_id:'fixture2',occurred_at:new Date(time-900000).toISOString(),position_effect:'OPEN',position_direction:'SHORT',quantity:'0.1',price:'61020',reason_text:'隔离演示数据',verification_status:'VERIFIED',source:'TEST_ONLY'},
    ],errors:[]}})
    const interval=url.searchParams.get('interval')
    const step=interval==='1h'?3600000:900000
    const end=Number(url.searchParams.get('end_time')||time+100*step)
    const bound=Math.floor(end/step)*step
    const items=Array.from({length:300},(_,i)=>{
      const start=bound-(299-i)*step
      const open=61000+i*4+Math.sin(i/4)*110
      return {open_at_ms:start,close_at_ms:start+step,open:String(open),close:String(open+12),low:String(open-19),high:String(open+20),volume:'12',is_closed:i<299}
    })
    return route.fulfill({json:{source:'BINANCE_USDM_PERP',interval,items}})
  })
  try {
    await page.goto('http://127.0.0.1:5186/kline')
    await page.getByText('公共行情已连接').waitFor({timeout:10000})
    await page.evaluate(()=>{
      const badge=document.createElement('div')
      badge.textContent='隔离演示数据 · Windows Edge 自动化'
      badge.style.cssText='position:fixed;right:110px;top:51px;z-index:100;background:#593e1c;color:#ffe0a2;padding:5px 10px;font:13px Segoe UI'
      document.body.append(badge)
    })
    await capture('01-default')
    await page.getByRole('button',{name:/D 减仓/}).click()
    await page.locator('.kw-detail').getByText('隔离演示数据',{exact:true}).waitFor()
    await capture('02-trade-detail')
    await page.getByLabel('K线周期').click()
    await capture('05-intervals')
    await page.getByRole('button',{name:'1h',exact:true}).click()
    await page.getByText('公共行情已连接').waitFor({timeout:10000})
    await page.getByRole('button',{name:'BOLL',exact:true}).click()
    assert.equal(await page.getByRole('button',{name:/BOLL 20/}).getAttribute('aria-pressed'),'true')
    await page.getByRole('button',{name:/MACD/}).click()
    await page.getByRole('button',{name:'MACD',exact:true}).click()
    await capture('03-indicators')
    await page.locator('.kw-market summary').click()
    await capture('04-strategy-filter')
    assert.equal(await page.locator('.kw-strategies input').count(),16)
    await page.locator('.kw-market summary').click()
    await page.setViewportSize({width:1000,height:650})
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),1000)
    await page.getByRole('button',{name:'下一条'}).click()
    assert.match(await page.locator('.kw-card').first().innerText(),/G 开空/)
    assert.deepEqual(errors,[])
    console.log('ISOLATED_BROWSER_UI_PASS')
  } catch(error) {
    console.error('Page errors:',errors)
    console.error((await page.locator('body').innerText()).slice(0,1000))
    throw error
  }
} finally {
  await browser.close()
}
