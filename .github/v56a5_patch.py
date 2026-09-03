from pathlib import Path
import re

p = Path('index.html')
s = p.read_text(encoding='utf-8')

if 'v56a4' not in s:
    raise SystemExit('v56a4 marker not found')
s = s.replace('v56a4', 'v56a5', 1)

marker = "var priceCache = {};  // {stockId: {price, at}}"
if marker in s and 'var priceFetchStatus = {};' not in s:
    s = s.replace(marker, marker + "\nvar priceFetchStatus = {}; // {stockId: true=本次抓到新報價, false=沿用舊價}", 1)

start = s.index('async function fetchYahooJson(targetUrl) {')
end = s.index('\nasync function fetchPrice(', start)
helper = '''async function fetchTextWithFallback(targetUrl) {
  const sources = [
    'https://r.jina.ai/http://' + targetUrl.replace(/^https?:\\/\\//, ''),
    PROXY + encodeURIComponent(targetUrl)
  ];
  for (const url of sources) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 12000);
    try {
      const res = await fetch(url, { signal: ctrl.signal, cache: 'no-store' });
      if (!res.ok) continue;
      const text = await res.text();
      if (text && text.trim()) return text;
    } catch(e) {
    } finally {
      clearTimeout(timer);
    }
  }
  return null;
}

async function fetchJsonDocument(targetUrl) {
  const text = await fetchTextWithFallback(targetUrl);
  if (!text) return null;
  const trimmed = text.trim();
  try { return JSON.parse(trimmed); } catch(e) {}
  const starts = [trimmed.indexOf('{'), trimmed.indexOf('[')].filter(i => i >= 0);
  if (!starts.length) return null;
  const start = Math.min(...starts);
  const endObj = trimmed.lastIndexOf('}');
  const endArr = trimmed.lastIndexOf(']');
  const end = Math.max(endObj, endArr);
  if (end < start) return null;
  try { return JSON.parse(trimmed.slice(start, end + 1)); } catch(e) { return null; }
}

async function fetchYahooJson(targetUrl) {
  const json = await fetchJsonDocument(targetUrl);
  if (json?.chart?.result?.[0]) return json;
  return null;
}
'''
s = s[:start] + helper + s[end:]

fp_start = s.index('async function fetchPrice(sid, code, market) {')
fp_end = s.index('\nasync function fetchUsdRate()', fp_start)
fp = s[fp_start:fp_end]
fp = fp.replace("async function fetchPrice(sid, code, market) {\n", "async function fetchPrice(sid, code, market) {\n  priceFetchStatus[sid] = false;\n", 1)
fp = fp.replace("        await savePriceDb(sid, price, prevClose);\n        return price;", "        await savePriceDb(sid, price, prevClose);\n        priceFetchStatus[sid] = true;\n        return price;", 1)
s = s[:fp_start] + fp + s[fp_end:]

div_start = s.index('async function fetchDividendCalendar() {')
div_end = s.index('\nfunction nextDividendFor(', div_start)
div_block = s[div_start:div_end]
old_fetch = """    const url = PROXY + encodeURIComponent('https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL');
    const ctrl = new AbortController();
    const timer = setTimeout(function(){ctrl.abort();}, 15000);
    const res = await fetch(url, { signal: ctrl.signal });
    clearTimeout(timer);
    if (res.ok) {
      const json = await res.json();
      (json||[]).forEach(function(row){"""
new_fetch = """    const json = await fetchJsonDocument('https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL');
    if (Array.isArray(json)) {
      (json||[]).forEach(function(row){"""
if old_fetch not in div_block:
    raise SystemExit('Dividend fetch block not found')
div_block = div_block.replace(old_fetch, new_fetch, 1)
s = s[:div_start] + div_block + s[div_end:]

fa_start = s.index('async function fetchAllPrices() {')
fa_end = s.index('\n// ── 歷史淨值', fa_start)
new_fa = '''async function fetchAllPrices() {
  document.getElementById('header-sub').textContent = '更新價格中…';
  await fetchUsdRate();
  priceFetchStatus = {};
  const quoteStocks = stocks.filter(st => st.market!=='基金');
  const promises = quoteStocks.map(st => fetchPrice(st.id, st.code, st.market));
  await Promise.all(promises);
  saveLocalCache();
  const updated = quoteStocks.filter(st => priceFetchStatus[st.id]).length;
  const failed = quoteStocks.filter(st => !priceFetchStatus[st.id]);
  const now = new Date().toLocaleTimeString('zh-TW');
  if (!quoteStocks.length) {
    document.getElementById('header-sub').textContent = '沒有需要自動更新的股票';
  } else if (!failed.length) {
    document.getElementById('header-sub').textContent = '價格已更新 ' + updated + '/' + quoteStocks.length + ' ・ ' + now + ' (報價可能延遲)';
  } else {
    const failedCodes = failed.slice(0,4).map(st => st.code).join('、') + (failed.length>4?'…':'');
    document.getElementById('header-sub').textContent = '更新 ' + updated + '/' + quoteStocks.length + '；' + failed.length + ' 支沿用舊價：' + failedCodes;
  }
  renderPortfolio();
}
'''
s = s[:fa_start] + new_fa + s[fa_end:]

hp_start = s.index('async function fetchHistPrices(')
hp_end = s.index('\nasync function fetchHistFx(', hp_start)
hp = s[hp_start:hp_end]
old_hp = """      const url = PROXY + encodeURIComponent(YF + ticker + '?interval=1d&period1='+period1+'&period2='+period2);
      const ctrl = new AbortController();
      const timer = setTimeout(function(){ctrl.abort();}, 15000);
      const res = await fetch(url, { signal: ctrl.signal });
      clearTimeout(timer);
      if (!res.ok) continue;
      const json = await res.json();"""
new_hp = """      const json = await fetchYahooJson(YF + ticker + '?interval=1d&period1='+period1+'&period2='+period2);
      if (!json) continue;"""
if old_hp not in hp:
    raise SystemExit('Historical price block not found')
hp = hp.replace(old_hp, new_hp, 1)
s = s[:hp_start] + hp + s[hp_end:]

fx_start = s.index('async function fetchHistFx(')
fx_end = s.index('\n// 在已排序', fx_start)
fx = s[fx_start:fx_end]
old_fx = """    const url = PROXY + encodeURIComponent(YF + 'USDTWD=X?interval=1d&period1='+period1+'&period2='+period2);
    const ctrl = new AbortController();
    const timer = setTimeout(function(){ctrl.abort();}, 15000);
    const res = await fetch(url, { signal: ctrl.signal });
    clearTimeout(timer);
    if (res.ok) {
      const json = await res.json();"""
new_fx = """    const json = await fetchYahooJson(YF + 'USDTWD=X?interval=1d&period1='+period1+'&period2='+period2);
    if (json) {"""
if old_fx not in fx:
    raise SystemExit('Historical FX block not found')
fx = fx.replace(old_fx, new_fx, 1)
s = s[:fx_start] + fx + s[fx_end:]

p.write_text(s, encoding='utf-8')
print('patched index.html to v56a5')
