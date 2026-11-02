import { chromium } from '../frontend/node_modules/playwright-core/index.mjs';

const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true, args: ['--disable-gpu'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1200 }, deviceScaleFactor: 1 });
const failures = [];
page.on('requestfailed', req => failures.push(`${req.url()} ${req.failure()?.errorText || ''}`));
page.on('pageerror', error => failures.push(error.name));
await page.goto('http://127.0.0.1:5174', { waitUntil: 'domcontentloaded', timeout: 20000 });
await page.waitForTimeout(12000);
await page.screenshot({ path: 'docs/reports/evidence/workbench-live.png', fullPage: true });
const text = await page.locator('body').innerText();
const names = ['AI账户','交易日记','策略管理','策略进化','系统状态'];
for (const [i, name] of names.entries()) {
  await page.getByRole('button', { name, exact: true }).click();
  await page.waitForTimeout(500);
  await page.screenshot({ path: `docs/reports/evidence/${['accounts','journal','strategies','evolution','system'][i]}-live.png`, fullPage: true });
}
console.log(JSON.stringify({ title: await page.title(), showsChartData: /根已确认数据/.test(text) && !/0 根已确认数据/.test(text), showsSpot: text.includes('BTC/USD'), liveDisabled: text.includes('LIVE TRADING DISABLED'), backendError: text.includes('后端连接异常'), screenshots: 6, failures }));
await browser.close();
