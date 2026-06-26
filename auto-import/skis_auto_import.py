import os
import re
import base64
import pdfplumber
import pandas as pd
from datetime import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── 設定（請依需求修改）────────────────────────────
SCOPES       = ['https://www.googleapis.com/auth/gmail.readonly']
DOWNLOAD_DIR = './skis_pdfs'
OUTPUT_DIR   = './skis_csv'
OUTPUT_FILE  = 'skis_all.csv'
SEARCH_QUERY = 'subject:(證券帳務回報通知) has:attachment newer_than:3d'
PDF_PASSWORD = ''          # 有加密填密碼，已解密留空

# ── Firebase 設定 ─────────────────────────────────
FIREBASE_USER_UID = ''     # 從瀏覽器 Console 執行 firebase.auth().currentUser.uid 取得
SERVICE_ACCOUNT   = 'firebase_service_account.json'

# ── Gmail 認證 ────────────────────────────────────
def get_gmail_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as f:
            f.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

# ── 遞迴取得所有 email parts ───────────────────────
def get_parts(payload):
    parts = []
    if 'parts' in payload:
        for p in payload['parts']:
            parts += get_parts(p)
    else:
        parts.append(payload)
    return parts

# ── 從 Gmail 下載 PDF 附件 ─────────────────────────
def download_skis_pdfs(service):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    results = service.users().messages().list(
        userId='me', q=SEARCH_QUERY, maxResults=20
    ).execute()
    messages = results.get('messages', [])
    downloaded = []

    for msg_meta in messages:
        msg = service.users().messages().get(
            userId='me', id=msg_meta['id'], format='full'
        ).execute()
        for part in get_parts(msg['payload']):
            filename = part.get('filename', '')
            if not filename.lower().endswith('.pdf'):
                continue
            att_id = part.get('body', {}).get('attachmentId')
            if not att_id:
                continue
            att = service.users().messages().attachments().get(
                userId='me', messageId=msg_meta['id'], id=att_id
            ).execute()
            data = base64.urlsafe_b64decode(att['data'])
            save_path = os.path.join(DOWNLOAD_DIR, filename)
            with open(save_path, 'wb') as f:
                f.write(data)
            print(f'✅ 下載：{filename}')
            downloaded.append(save_path)

    if not downloaded:
        print('⚠️ 沒有找到任何 PDF 附件，請確認 SEARCH_QUERY 是否正確')
    return downloaded

# ── 解析 PDF → 回傳 records list ──────────────────
def parse_skis_pdf(pdf_path, password=PDF_PASSWORD):
    records = []
    year = None

    RE_NORMAL = re.compile(
        r'^(\d{2}/\d{2})\s+'
        r'(\d{4,6})\s+'
        r'(\S+)\s+'
        r'([\d,]+)\s+'
        r'([\d.]+)\s+'
        r'(現買|現賣|融資買進|融資賣出|融券買進|融券賣出)\s+'
        r'[\d,]+\s+'
        r'([\d,]+)\s+'   # 手續費
        r'([\d,]+)\s+'   # 交易稅
        r'[\d,]+\s+[\d,]+\s+[\d,]+\s+[\d,]+\s+'
        r'TWD\s+'
        r'([\d,]+)\((收|付)\)'
    )

    with pdfplumber.open(pdf_path, password=password) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ''

            if year is None:
                m = re.search(r'(\d{4})\s*年', text)
                if m:
                    year = m.group(1)

            lines = text.split('\n')
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                yr = year or str(datetime.now().year)

                m = RE_NORMAL.match(line)
                if m:
                    date_raw = m.group(1)
                    symbol   = m.group(2)
                    name     = m.group(3)
                    qty      = m.group(4)
                    price    = m.group(5)
                    side_raw = m.group(6)
                    fee      = m.group(7)
                    tax      = m.group(8)
                    net      = m.group(9)

                    mm, dd = date_raw.split('/')
                    date_str = f"{yr}-{mm}-{dd}"
                    trade_type = '買入' if '買' in side_raw else '賣出'

                    real_fee = int(fee.replace(',', ''))
                    real_tax = int(tax.replace(',', ''))
                    real_net = abs(float(net.replace(',', '')))

                    # ── 定期定額判斷 ──────────────────────────────────────
                    is_periodic = False
                    if i + 1 < len(lines) and '[定期定額]' in lines[i + 1]:
                        is_periodic = True
                        m_pre = re.search(r'預繳金\s*=\s*([\d,]+)', lines[i + 1])
                        if m_pre:
                            real_net = float(m_pre.group(1).replace(',', ''))
                    # ─────────────────────────────────────────────────────

                    remark = f'新光帳務 定期定額 {date_str}' if is_periodic else f'新光帳務 {date_str}'

                    records.append({
                        '日期':           date_str,
                        '標的代號':       symbol.zfill(4),
                        '標的名稱':       name,
                        '市場':           '台股',   # tracker 用台股
                        '幣別':           'TWD',
                        '類型':           trade_type,
                        '股數':           int(qty.replace(',', '')),
                        '單價':           float(price.replace(',', '')),
                        '手續費':         real_fee,
                        '交易稅':         real_tax,
                        '賣出成本(每股)': '',
                        '總金額':         real_net,
                        '備註':           remark
                    })
                    label = '定期定額' if is_periodic else side_raw
                    print(f'  ✅ {date_str} {symbol} {name} {label} {qty}股 @{price} 金額={real_net}')

                i += 1

    return records

# ── Firebase 匯入 ─────────────────────────────────
_db = None

def get_db():
    global _db
    if _db:
        return _db
    import firebase_admin
    from firebase_admin import credentials, firestore
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(SERVICE_ACCOUNT))
    _db = firestore.client()
    return _db

def find_or_create_stock(uid, code, name):
    from firebase_admin import firestore
    db = get_db()
    stocks = db.collection('users').document(uid).collection('stocks')
    for doc in stocks.stream():
        if doc.to_dict().get('code', '').upper() == code.upper():
            return doc.id, False
    _, ref = stocks.add({
        'code': code, 'name': name, 'market': '台股',
        'createdAt': firestore.SERVER_TIMESTAMP,
    })
    print(f'    ✨ 新建標的：{code} {name}')
    return ref.id, True

def count_existing(uid, stock_id, date, tx_type, shares, price):
    """計算 Firebase 中已有幾筆條件相同的交易（日期+類型+股數+單價）"""
    db = get_db()
    txns = (db.collection('users').document(uid)
              .collection('stocks').document(stock_id)
              .collection('txns'))
    count = 0
    for doc in txns.where('date', '==', date).where('type', '==', tx_type).stream():
        d = doc.to_dict()
        if (abs(float(d.get('shares', 0)) - float(shares)) < 0.001 and
                abs(float(d.get('price',  0)) - float(price))  < 0.001):
            count += 1
    return count

def import_to_firebase(uid, records, auto_run=False):
    from firebase_admin import firestore
    db = get_db()
    imported = skipped = 0

    # 統計本批次每個 key 出現幾次，用來處理同批次內的重複
    # key = (code, date, type, shares, price)
    batch_counts = {}
    for r in records:
        key = (r['標的代號'], r['日期'], r['類型'], r['股數'], r['單價'])
        batch_counts[key] = batch_counts.get(key, 0) + 1

    # 記錄本批次已處理幾筆（用來搭配 count_existing 判斷）
    batch_done = {}

    for r in records:
        code    = r['標的代號']
        name    = r['標的名稱']
        date    = r['日期']
        tx_type = r['類型']
        shares  = r['股數']
        price   = r['單價']
        sid, _  = find_or_create_stock(uid, code, name)

        key = (code, date, tx_type, shares, price)
        batch_done[key] = batch_done.get(key, 0)

        # Firebase 已有幾筆 + 本批次已寫幾筆 >= 本批次總共幾筆 → 跳過
        already_in_db = count_existing(uid, sid, date, tx_type, shares, price)
        if already_in_db + batch_done[key] >= batch_counts[key] + already_in_db:
            # 換個角度：若 db 已有 N 筆，本批次有 M 筆，只要再寫 M-N 筆
            pass  # 由下方邏輯處理

        # db 已有的數量 > 本批次目前這是第幾筆 → 可能重複
        if already_in_db > batch_done[key]:
            print(f'    ⚠️  疑似重複：{date} {code} {tx_type} {shares}股 @{price}')
            print(f'       Firebase 已有 {already_in_db} 筆相同條件的交易')
            if auto_run:
                print(f'       ⏩ 自動模式：跳過')
                skipped += 1
                batch_done[key] += 1
                continue
            ans = input('       仍要匯入這筆嗎？[y/N] ').strip().lower()
            if ans != 'y':
                print(f'       ⏩ 跳過')
                skipped += 1
                batch_done[key] += 1
                continue
            print(f'       ✅ 強制匯入')

        (db.collection('users').document(uid)
           .collection('stocks').document(sid)
           .collection('txns')).add({
            'type':   tx_type,
            'date':   date,
            'shares': shares,
            'price':  price,
            'fee':    r['手續費'],
            'tax':    r['交易稅'],
            'total':  r['總金額'],
            'note':   r['備註'],
            'createdAt': firestore.SERVER_TIMESTAMP,
        })
        print(f'    🔥 寫入 Firebase：{date} {code} {name} {tx_type} {shares}股 @{price}')
        imported += 1
        batch_done[key] += 1

    return imported, skipped

# ── 主程式 ────────────────────────────────────────
def main():
    import sys
    dry_run  = '--dry-run' in sys.argv
    auto_run = '--auto'    in sys.argv  # 自動排程模式：疑似重複一律跳過，不詢問

    if dry_run:
        print('=' * 55)
        print('  🔍 預覽模式（不會寫入 Firebase）')
        print('=' * 55)

    service = get_gmail_service()
    pdfs = download_skis_pdfs(service)

    all_records = []

    for pdf_path in pdfs:
        print(f'\n📄 解析：{pdf_path}')
        records = parse_skis_pdf(pdf_path)

        if not records:
            print('  ⚠️ 沒有找到交易明細，請確認 PDF 密碼或格式')
            continue

        all_records.extend(records)
        print(f'  ✅ 本檔解析 {len(records)} 筆，累計 {len(all_records)} 筆')

    if not all_records:
        print('\n⚠️ 全部 PDF 均無交易明細')
        return

    # ── 預覽表格 ──────────────────────────────────
    print(f'\n{"─"*65}')
    print(f'  {"日期":<12} {"代號":<6} {"名稱":<10} {"類型":<4} {"股數":>6} {"單價":>8} {"手續費":>6} {"總金額":>10}')
    print(f'  {"─"*63}')
    for r in all_records:
        print(f'  {r["日期"]:<12} {r["標的代號"]:<6} {r["標的名稱"]:<10} '
              f'{r["類型"]:<4} {r["股數"]:>6} {r["單價"]:>8.2f} '
              f'{r["手續費"]:>6} {r["總金額"]:>10.0f}')
    print(f'{"─"*65}')
    print(f'  共 {len(all_records)} 筆')

    if dry_run:
        print('\n✅ 預覽完成，資料未寫入。')
        print('   確認無誤後執行正式匯入：')
        print('   python skis_auto_import.py')
        return

    # ── 備份 CSV ──────────────────────────────────
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_csv = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    df = pd.DataFrame(all_records, columns=[
        '日期', '標的代號', '標的名稱', '市場', '幣別', '類型',
        '股數', '單價', '手續費', '交易稅', '賣出成本(每股)', '總金額', '備註'
    ])
    df.sort_values('日期', inplace=True)
    df.to_csv(out_csv, index=False, encoding='utf-8-sig')
    print(f'\n📄 CSV 備份：{out_csv}')

    # ── 寫入 Firebase ─────────────────────────────
    if not FIREBASE_USER_UID:
        print('\n⚠️  FIREBASE_USER_UID 未設定，跳過 Firebase 匯入')
        print('   請在瀏覽器 Console 執行 firebase.auth().currentUser.uid 取得 UID')
        return

    if not os.path.exists(SERVICE_ACCOUNT):
        print(f'\n⚠️  找不到 {SERVICE_ACCOUNT}，跳過 Firebase 匯入')
        return

    print(f'\n🔥 寫入 Firebase（UID: {FIREBASE_USER_UID[:8]}…）')
    imp, skip = import_to_firebase(FIREBASE_USER_UID, all_records, auto_run=auto_run)
    print(f'\n🎉 完成！匯入 {imp} 筆，跳過重複 {skip} 筆')

if __name__ == '__main__':
    main()
