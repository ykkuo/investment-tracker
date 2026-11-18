# 投資損益追蹤 App

## 專案概述
- 個人投資追蹤工具，擁有者 YK，台灣、繁體中文，UI 文字一律繁中
- 單一檔案前端：`index.html`（vanilla JS + Firebase compat SDK 10.7.1 + Chart.js 4.4.0）
- 後端：Firebase Auth（Google 登入）+ Firestore，部署在 GitHub Pages
- 追蹤台股、美股、基金；台幣/美金雙幣別
- Firestore 結構：users/{uid}/stocks/{sid}、users/{uid}/stocks/{sid}/txns/{txid}
- 交易類型：買入 / 賣出 / 股息 / 再投入；賣出可有 sellCost、sellLots（批次）

## 絕對規則
1. 保持單一 HTML 檔，不拆檔、不加 build 工具
2. 用精準的小幅修改，不要整檔重寫
3. 改之前先確認 `index.html` 內版本號（右上 header 的 vXXaX）是最新基底
4. 不可破壞既有 Firestore 資料結構（向下相容）
5. 任何會改寫歷史資料的功能（如拆股），要有確認提示並提醒先匯出備份

## 版本號規則 v<N><字母><M>
- 大功能：數字+1，字母重設 a，子號重設 1（例 v56a5 → v57a1）
- 小功能：字母往後，子號重設 1（例 v56a5 → v56b1）
- Bug 修正：子號+1（例 v56a5 → v56a6）
- 不確定規模時自行判斷，並用一句話說明理由
- 每次修改要同步更新畫面上的版本標籤

## 每次交付要附 changelog
版本號＋套用的規則、逐項 bullet、不相關的修正分開列、需要 YK 注意的事項標出；保持簡短。

## 合作方式
- 大功能或結構調整，先給 YK 看方案/文案，確認後再實作
- 複雜解析任務，先問關鍵的結構問題再設計
- 用繁體中文回覆

## 已知的坑（務必遵守）
- 浮點殘值：賣出後與全部交易跑完後，股數 |x|<1e-6 視為 0（`calc()` 與 `getAvailableLots()` 都要）
- Yahoo Finance 歷史價格會對拆股做回溯調整，所以 app 內股數必須是拆股後基準；已有「拆股調整」功能，不要手動逐筆改
- 報價走 CORS proxy。2026-11 實測：只有 r.jina.ai 可用且**每分鐘限 20 次**；allorigins、codetabs 掛掉（522），corsproxy.io 需 API key，cors.lol 限流
  - 因此報價必須「批次」：台股 MIS 一次 40 檔、Yahoo spark 一次 20 檔（含 USDTWD=X），整體 2～4 個請求；不要改回逐檔並行
  - `fetchTextWithFallback` 有斷路器：429／逾時後該 proxy 60 秒內跳過；jina 的 5xx 是目標網站錯誤，不觸發
  - MIS 成交價 z 常是 "-"，用 `misRowPrice()` 依序退回 trade.z → pz → 買賣中間價 → 昨收
  - 淨值圖的歷史股價（`fetchHistPrices`）仍是逐檔並行，持股多時會撞到限制
  - 只抓「持有中」標的的現價（`needsLiveQuote`），已出清的沿用舊報價
  - 遇到 429 限流時，65 秒後自動重抓失敗的那幾檔（只重試一次），header-sub 會顯示「proxy 限流」
- 已出清的標的要保留，歷史績效才完整
- 基金沒有歷史淨值，淨值由使用者手動輸入；USD 基金用 st.currency==='USD' 判斷
- 即時頁面用即時匯率，歷史圖表用當時歷史匯率，兩者合計不一致是正常的

## 自動匯入（auto-import/）
- 實際運作的是 `skis_auto_import.py`（新光證券 PDF → Firebase），由 `.github/workflows/daily_import.yml` 平日台灣 16:30 跑
- PDF_PASSWORD / FIREBASE_UID 由 workflow 以環境變數帶入，腳本用 `os.environ` 讀取（不再用 regex 改寫原始碼）
- 套件清單以 `auto-import/requirements.txt` 為準，workflow 直接 `pip install -r`
- repo 是公開的：`--auto` 模式只印統計數字，不可印出逐筆交易；不上傳 artifact
- 模式：`--dry-run` 只預覽、`--auto` 排程模式（疑似重複一律跳過）
- 交易日只有 MM/DD，年份用「收信日期」推算（交易月份 > 收信月份 → 去年），不依賴 PDF 內的年份格式
- 重複判斷：同一封信內相同交易算多筆；同一筆交易出現在不同信件只算一次（取各信件出現次數的最大值）
- 下載的 PDF 檔名前面加信件 ID，避免不同信件附件同名互相覆蓋
- 通知走 Telegram Bot；機密全在 GitHub Secrets，絕不寫進 repo
- 規劃中：基富通、中租的基金 PDF 匯入（尚未實作，遇到密碼 PDF、USD 基金以台幣扣款等問題）

## 待辦
- 報價備援：目前沒有免費 proxy 可當第二備援；可考慮 GitHub Actions 定時寫入報價或自架 Cloudflare Worker
- 淨值圖歷史股價改批次（避免撞 jina 每分鐘 20 次限制）
- 基金自動匯入解析器
- 支出追蹤 App（獨立專案，獨立 Firebase）