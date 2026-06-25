#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新光證券帳務回報通知 → 投資追蹤器 自動匯入腳本
流程：Gmail 抓信 → pikepdf 解密 → pdfplumber 解析文字
      → 若文字為空自動改用 Claude Vision OCR
"""

import os, io, re, json, base64, pickle, sys
from datetime import datetime
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / 'config.json'

def load_config():
    if not CONFIG_FILE.exists():
        default = {
            "pdf_password":          "請填入PDF密碼",
            "firebase_user_uid":     "請填入你的Firebase UID",
            "anthropic_api_key":     "請填入Anthropic API Key（僅圖片型PDF才需要）",
            "days_to_fetch":         14,
            "processed_emails_file": "processed_emails.json"
        }
        CONFIG_FILE.write_text(json.dumps(default, ensure_ascii=False, indent=2))
        print("⚠️  已建立 config.json，請填入設定後再執行")
        sys.exit(1)
    cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    for k in ["pdf_password", "firebase_user_uid"]:
        if "請填入" in str(cfg.get(k, "")):
            print(f"⚠️  請在 config.json 填入 {k}")
            sys.exit(1)
    return cfg

# ── Gmail ────────────────────────────────────────────────
GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def get_gmail_service():
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    creds = None
    token_file = Path('gmail_token.pickle')
    if token_file.exists():
        with open(token_file, 'rb') as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path('gmail_credentials.json').exists():
                print("❌ 找不到 gmail_credentials.json，請參閱 README.md")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file('gmail_credentials.json', GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, 'wb') as f:
            pickle.dump(creds, f)
    return build('gmail', 'v1', credentials=creds)

def fetch_skis_emails(service, days_back):
    query = f'from:sk88.com.tw has:attachment filename:pdf newer_than:{days_back}d'
    result = service.users().messages().list(userId='me', q=query).execute()
    return result.get('messages', [])

def get_email_date(headers):
    for h in headers:
        if h['name'].lower() == 'date':
            for fmt in ['%a, %d %b %Y %H:%M:%S %z', '%d %b %Y %H:%M:%S %z',
                        '%a, %d %b %Y %H:%M:%S %Z']:
                try:
                    return datetime.strptime(h['value'].strip(), fmt)
                except:
                    pass
    return datetime.now()

def download_pdf_attachments(service, msg_id):
    msg = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    email_date = get_email_date(msg['payload'].get('headers', []))
    pdfs = []
    def walk(parts):
        for part in parts:
            if part.get('parts'):
                walk(part['parts'])
            if part.get('filename', '').lower().endswith('.pdf'):
                att_id = part['body'].get('attachmentId')
                if att_id:
                    att = service.users().messages().attachments().get(
                        userId='me', messageId=msg_id, attachmentId=att_id).execute()
                    data = base64.urlsafe_b64decode(att['data'])
                    pdfs.append((data, part['filename'], email_date))
    walk(msg['payload'].get('parts', []))
    return pdfs

# ── PDF 解密（pikepdf 保留文字層）────────────────────────
def decrypt_pdf(pdf_bytes: bytes, password: str) -> bytes:
    import pikepdf
    with pikepdf.open(io.BytesIO(pdf_bytes), password=password) as pdf:
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()

# ── 文字層解析（主要路徑）───────────────────────────────
TYPE_MAP = {
    '現買':'買入', '買進':'買入', '融資買進':'買入', '融買':'買入',
    '現賣':'賣出', '賣出':'賣出', '融資賣出':'賣出', '融賣':'賣出',
}

def clean_num(s) -> float:
    if not s:
        return 0.0
    s = re.sub(r'[,\s]', '', str(s))
    s = re.sub(r'[（(][收付].*', '', s)
    try:
        return float(s)
    except:
        return 0.0

def resolve_date(raw: str, email_date: datetime) -> str:
    parts = raw.strip().split('/')
    if len(parts) == 2:
        mm, dd = int(parts[0]), int(parts[1])
        year = email_date.year
        if mm > email_date.month:
            year -= 1
        return f"{year}-{mm:02d}-{dd:02d}"
    elif len(parts) == 3:
        y = int(parts[0])
        if y < 200:
            y += 1911
        return f"{y}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return raw

def parse_row(cells, email_date) -> dict | None:
    """解析單列資料（支援 list 或 str.split() 格式）"""
    cells = [str(c).strip() if c else '' for c in cells]

    # 找日期欄
    date_idx = None
    for i, c in enumerate(cells):
        if re.match(r'^\d{2}/\d{2}$', c) or re.match(r'^\d{3,4}/\d{2}/\d{2}$', c):
            date_idx = i
            break
    if date_idx is None:
        return None

    # 跳過合計列
    if any('合計' in c for c in cells):
        return None

    try:
        i = date_idx
        code     = cells[i+1]
        name     = cells[i+2]
        shares   = int(clean_num(cells[i+3]))
        price    = clean_num(cells[i+4])
        type_raw = cells[i+5]
        fee      = clean_num(cells[i+7]) if len(cells) > i+7 else 0
        tax      = clean_num(cells[i+8]) if len(cells) > i+8 else 0

        # 應收付金額：從尾端找第一個含數字的欄
        total = 0.0
        for c in reversed(cells):
            if re.search(r'\d{3}', c):
                total = clean_num(c)
                break

        if not code or not shares or not price:
            return None

        tx_kind = None
        for k, v in TYPE_MAP.items():
            if k in type_raw:
                tx_kind = v
                break
        if not tx_kind:
            return None

        return {
            'date':   resolve_date(cells[i], email_date),
            'code':   code,
            'name':   name,
            'market': '台股',
            'type':   tx_kind,
            'shares': shares,
            'price':  price,
            'fee':    fee,
            'tax':    tax,
            'total':  total,
            'note':   '新光證券自動匯入',
        }
    except:
        return None

def parse_by_text(pdf_bytes: bytes, email_date: datetime) -> list[dict]:
    """pdfplumber 文字層解析"""
    import pdfplumber
    transactions = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ''
            if not text.strip():
                continue

            in_tx = False
            for line in text.split('\n'):
                line = line.strip()
                if '交易資料明細' in line:
                    in_tx = True
                    continue
                if in_tx and ('庫存資料明細' in line or '其他說明' in line):
                    break
                if not in_tx:
                    continue
                # 交易列：開頭是 MM/DD 後接股票代號
                if not re.match(r'^\d{2}/\d{2}\s+\d{4}', line):
                    continue
                tx = parse_row(line.split(), email_date)
                if tx:
                    transactions.append(tx)

    return transactions

# ── Claude Vision OCR 備援路徑 ────────────────────────────
EXTRACT_PROMPT = """這是新光證券的帳務回報通知 PDF 頁面截圖。

請找出「交易資料明細」表格，將每一筆交易擷取為 JSON 陣列。
若此頁沒有交易資料，回傳空陣列 []。

每筆格式：
{"date":"MM/DD","code":"4位數代號","name":"名稱","shares":股數整數,
 "price":單價,"type":"現買或現賣","fee":手續費,"tax":交易稅,"total":應收付金額數字}

只回傳 JSON 陣列，不要加任何說明。"""

def pdf_to_images(pdf_bytes: bytes) -> list[bytes]:
    from pdf2image import convert_from_bytes
    result = []
    for img in convert_from_bytes(pdf_bytes, dpi=200, fmt='png'):
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        result.append(buf.getvalue())
    return result

def ocr_with_claude(image_bytes: bytes, api_key: str) -> list[dict]:
    import urllib.request
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(image_bytes).decode()}},
            {"type": "text", "text": EXTRACT_PROMPT}
        ]}]
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    raw = re.sub(r'^```json\s*|^```\s*|```$', '', data['content'][0]['text'].strip(),
                 flags=re.MULTILINE).strip()
    return json.loads(raw)

def normalize_vision_tx(raw: dict, email_date: datetime) -> dict | None:
    try:
        tx_kind = next((v for k, v in TYPE_MAP.items() if k in str(raw.get('type',''))), None)
        if not tx_kind:
            return None
        return {
            'date':   resolve_date(str(raw['date']), email_date),
            'code':   str(raw['code']).strip(),
            'name':   str(raw['name']).strip(),
            'market': '台股',
            'type':   tx_kind,
            'shares': int(raw['shares']),
            'price':  float(raw['price']),
            'fee':    float(raw.get('fee', 0)),
            'tax':    float(raw.get('tax', 0)),
            'total':  float(raw['total']),
            'note':   '新光證券自動匯入',
        }
    except Exception as e:
        print(f"  ⚠️  正規化失敗：{raw} → {e}")
        return None

def parse_by_vision(pdf_bytes: bytes, email_date: datetime, api_key: str) -> list[dict]:
    """Claude Vision 備援路徑"""
    print("  🖼  文字層為空，改用 Claude Vision OCR…")
    images = pdf_to_images(pdf_bytes)
    transactions = []
    for i, img in enumerate(images, 1):
        print(f"  📸 第 {i} 頁 OCR…", end=' ', flush=True)
        try:
            rows = ocr_with_claude(img, api_key)
            print(f"找到 {len(rows)} 筆")
            for raw in rows:
                tx = normalize_vision_tx(raw, email_date)
                if tx:
                    transactions.append(tx)
        except Exception as e:
            print(f"失敗：{e}")
    return transactions

def parse_skis_pdf(pdf_bytes: bytes, email_date: datetime, api_key: str = '') -> list[dict]:
    """主解析函式：優先文字層，失敗再走 Vision"""
    txs = parse_by_text(pdf_bytes, email_date)
    if txs:
        return txs
    if api_key and '請填入' not in api_key:
        return parse_by_vision(pdf_bytes, email_date, api_key)
    print("  ⚠️  文字層為空且未設定 anthropic_api_key，無法 OCR")
    return []

# ── Firebase ─────────────────────────────────────────────
_db = None
def get_db():
    global _db
    if _db:
        return _db
    import firebase_admin
    from firebase_admin import credentials, firestore
    sa_file = Path('firebase_service_account.json')
    if not sa_file.exists():
        print("❌ 找不到 firebase_service_account.json，請參閱 README.md")
        sys.exit(1)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(str(sa_file)))
    _db = firestore.client()
    return _db

def stocks_ref(uid):
    return get_db().collection('users').document(uid).collection('stocks')

def txns_ref(uid, sid):
    return stocks_ref(uid).document(sid).collection('txns')

def find_or_create_stock(uid, code, name, market='台股'):
    from firebase_admin import firestore
    for doc in stocks_ref(uid).stream():
        if doc.to_dict().get('code', '').upper() == code.upper():
            return doc.id, False
    _, ref = stocks_ref(uid).add({'code': code, 'name': name, 'market': market,
                                   'createdAt': firestore.SERVER_TIMESTAMP})
    return ref.id, True

def is_duplicate(uid, sid, date, tx_type, shares):
    for doc in txns_ref(uid, sid).where('date','==',date).where('type','==',tx_type).stream():
        if abs(float(doc.to_dict().get('shares', 0)) - float(shares)) < 0.001:
            return True
    return False

def import_transactions(uid, transactions):
    from firebase_admin import firestore
    imported = skipped = 0
    for tx in transactions:
        sid, is_new = find_or_create_stock(uid, tx['code'], tx['name'], tx['market'])
        label = f"{tx['date']} {tx['code']} {tx['name']} {tx['type']} {tx['shares']}股"
        if not is_new and is_duplicate(uid, sid, tx['date'], tx['type'], tx['shares']):
            print(f"  ⏩ 重複跳過：{label}")
            skipped += 1
            continue
        txns_ref(uid, sid).add({
            'type': tx['type'], 'date': tx['date'], 'shares': tx['shares'],
            'price': tx['price'], 'fee': tx.get('fee', 0), 'tax': tx.get('tax', 0),
            'total': tx['total'], 'note': tx.get('note', ''),
            'createdAt': firestore.SERVER_TIMESTAMP,
        })
        print(f"  ✅ 匯入：{label} @${tx['price']:.2f}  總額:{tx['total']:.0f}")
        imported += 1
    return imported, skipped

# ── 主程式 ───────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  新光證券帳務通知 → 投資追蹤器 自動匯入")
    print("=" * 55)

    config = load_config()
    processed_file = Path(config['processed_emails_file'])
    processed_ids = json.loads(processed_file.read_text()) if processed_file.exists() else []

    print("\n📬 連接 Gmail…")
    gmail = get_gmail_service()

    days = config['days_to_fetch']
    print(f"🔍 搜尋近 {days} 天的新光證券帳務通知…")
    messages = fetch_skis_emails(gmail, days)

    if not messages:
        print("📭 沒有符合條件的信件，結束。")
        return

    print(f"📩 找到 {len(messages)} 封信件\n")
    total_imported = 0

    for msg in messages:
        mid = msg['id']
        if mid in processed_ids:
            print(f"⏩ 信件 {mid[:12]}… 已處理，跳過")
            continue

        print(f"─── 處理信件 {mid[:12]}… ───")
        pdfs = download_pdf_attachments(gmail, mid)

        if not pdfs:
            print("  ⚠️  無 PDF 附件")
            processed_ids.append(mid)
            continue

        for pdf_bytes, fname, email_date in pdfs:
            print(f"  📄 {fname}  ({email_date.strftime('%Y-%m-%d')})")
            try:
                decrypted = decrypt_pdf(pdf_bytes, config['pdf_password'])
            except Exception as e:
                print(f"  ❌ 解密失敗：{e}")
                continue

            txs = parse_skis_pdf(decrypted, email_date, config.get('anthropic_api_key', ''))

            if not txs:
                print("  ℹ️  沒有交易資料（非交易日通知）")
            else:
                print(f"  📊 解析到 {len(txs)} 筆，開始匯入…")
                imp, skip = import_transactions(config['firebase_user_uid'], txs)
                total_imported += imp

        processed_ids.append(mid)
        processed_file.write_text(json.dumps(processed_ids, indent=2))

    print(f"\n{'='*55}")
    print(f"  🎉 完成！本次共匯入 {total_imported} 筆交易")
    print(f"{'='*55}\n")

if __name__ == '__main__':
    main()
