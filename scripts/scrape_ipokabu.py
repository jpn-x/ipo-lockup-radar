#!/usr/bin/env python3
"""
ipokabu.net から直近6ヶ月のIPO銘柄ロックアップ情報を自動取得して
data/stocks.json を更新するスクリプト

依存: requests, beautifulsoup4, lxml
"""
import json, re, time, sys
from datetime import date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE      = "https://ipokabu.net"
DATA_FILE = Path(__file__).parent.parent / "data" / "stocks.json"
HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; ipo-lockup-radar/1.0)"}
MONTHS_JP = {"jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
              "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12"}

def get(url, delay=1.5):
    time.sleep(delay)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "lxml")

# ── 対象月リスト（直近6ヶ月）──
def target_months(n=14):  # 約1年分
    """直近n ヶ月分の (year, month) リストを返す"""
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(n):
        months.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return months

# ── 月別銘柄一覧を取得 ──
def fetch_month_tickers(year, month):
    month_names = {v: k for k, v in MONTHS_JP.items()}
    mon_str = month_names[f"{month:02d}"]
    url = f"{BASE}/data/{year}/{mon_str}"
    try:
        soup = get(url)
    except Exception as e:
        print(f"  月別ページ取得失敗 {url}: {e}")
        return []

    # 銘柄コードは「数字3-4桁 + 任意のA」の形式のみ対象
    TICKER_RE = re.compile(r"^/ipo/(\d{3,4}[A-Za-z]?)$")
    tickers = []
    for a in soup.find_all("a", href=TICKER_RE):
        m = TICKER_RE.match(a["href"])
        if not m:
            continue
        code = m.group(1)
        name = a.get_text(strip=True)
        if code and name:
            tickers.append((code, name))
    # 重複除去
    seen = set()
    result = []
    for t in tickers:
        if t[0] not in seen:
            seen.add(t[0])
            result.append(t)
    return result

# ── ロックアップ解除日をテキストから抽出 ──
def extract_release_date(text):
    # 「2026年6月16日」パターン
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None

# ── 株数テキストを整数化 ──
def parse_shares(text):
    text = text.replace(",", "").replace("株", "").replace(" ", "").strip()
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else 0

# ── ホルダー種別推定 ──
def guess_type(name):
    name_lower = name
    if any(k in name_lower for k in ["VC", "ファンド", "投資事業", "キャピタル",
                                       "インキュベ", "グロービス", "ANRI", "ジャフコ",
                                       "Ventures", "Venture", "East", "WiL", "PE",
                                       "L.P.", "組合", "Cayman"]):
        return "vc"
    if any(k in name_lower for k in ["代表取締役", "社長", "創業者", "代表者",
                                       "資産管理", "Holdings", "ホールディングス"]):
        return "founder"
    if any(k in name_lower for k in ["取締役", "役員", "監査役", "従業員持株",
                                       "持株会"]):
        return "other"
    if any(k in name_lower for k in ["銀行", "証券", "保険", "商事", "造船",
                                       "電機", "自動車", "通信", "ドコモ", "トヨタ",
                                       "DeNA", "KDDI", "NTT"]):
        return "corporate"
    return "other"

# ── 上場日・公開価格・市場をテキストから抽出 ──
def extract_basic_info(soup):
    ipo_date    = None
    market      = ""
    init_price  = 0

    text = soup.get_text()

    # 上場日
    m = re.search(r"上場日[：:\s]*(\d{4})[年/](\d{1,2})[月/](\d{1,2})日?", text)
    if m:
        ipo_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # 公開価格
    m = re.search(r"公開価格[：:\s]*([0-9,]+)円", text)
    if m:
        init_price = int(m.group(1).replace(",", ""))

    # 市場
    for kw in ["東証グロース", "東証スタンダード", "東証プライム", "グロース", "スタンダード", "プライム"]:
        if kw in text:
            if "グロース" in kw:
                market = "東証グロース"
            elif "スタンダード" in kw:
                market = "東証スタンダード"
            elif "プライム" in kw:
                market = "東証プライム"
            break

    return ipo_date, market, init_price

# ── ロックアップテーブルをパース ──
def parse_lockup_tables(soup, ipo_date, ticker):
    lockups = []

    # ページ全文から解除日を探す
    full_text = soup.get_text()

    # テーブルを全走査
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        rows    = table.find_all("tr")
        if len(rows) < 2:
            continue

        # 株主名・株数・ロック期間カラムを特定
        name_idx   = next((i for i, h in enumerate(headers) if "株主" in h or "名前" in h or "氏名" in h), None)
        shares_idx = next((i for i, h in enumerate(headers) if "株数" in h or "保有" in h), None)
        lock_idx   = next((i for i, h in enumerate(headers) if "ロック" in h or "制限" in h or "期間" in h), None)

        if name_idx is None:
            continue

        holders = []
        lock_days = 180
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= name_idx:
                continue
            name = cells[name_idx].get_text(strip=True)
            if not name or name in ["合計", "—", "-"]:
                continue

            shares = 0
            if shares_idx is not None and shares_idx < len(cells):
                shares = parse_shares(cells[shares_idx].get_text(strip=True))

            # ロック期間
            lock_text = ""
            if lock_idx is not None and lock_idx < len(cells):
                lock_text = cells[lock_idx].get_text(strip=True)
                m90  = re.search(r"90", lock_text)
                m360 = re.search(r"360", lock_text)
                if m360:
                    lock_days = 360
                elif m90:
                    lock_days = 90
                else:
                    lock_days = 180

            htype = guess_type(name)
            holders.append({"name": name, "shares": shares, "type": htype})

        if not holders:
            continue

        # 解除日を計算 or テキストから取得
        release_date = None

        # テキストから日付を拾う
        for date_str in re.findall(r"\d{4}年\d{1,2}月\d{1,2}日", full_text):
            release_date = extract_release_date(date_str)
            break

        # なければIPO日 + ロック日数で算出
        if not release_date and ipo_date:
            try:
                base = date.fromisoformat(ipo_date)
                rd   = base + timedelta(days=lock_days)
                release_date = rd.isoformat()
            except Exception:
                pass

        if not release_date:
            continue

        # 既存ロックアップと解除日が同じなら統合
        existing = next((l for l in lockups if l["release_date"] == release_date), None)
        if existing:
            existing["holders"].extend(holders)
            existing["shares"] += sum(h["shares"] for h in holders)
        else:
            label = f"主要株主（{lock_days}日）"
            if any(h["type"] == "vc" for h in holders):
                label = f"VC・主要株主（{lock_days}日）"
            lockups.append({
                "label": label,
                "release_date": release_date,
                "shares": sum(h["shares"] for h in holders),
                "holders": holders
            })

    # テーブルが空でもテキストから日付だけ拾えた場合の最小エントリ
    if not lockups and ipo_date:
        release_date = None
        for date_str in re.findall(r"\d{4}年\d{1,2}月\d{1,2}日", full_text):
            candidate = extract_release_date(date_str)
            if candidate and candidate > ipo_date:
                release_date = candidate
                break
        if not release_date:
            try:
                base = date.fromisoformat(ipo_date)
                release_date = (base + timedelta(days=180)).isoformat()
            except Exception:
                pass
        if release_date:
            lockups.append({
                "label": "主要株主（180日）",
                "release_date": release_date,
                "shares": 0,
                "holders": []
            })

    return lockups

# ── 1銘柄を取得 ──
def fetch_stock(ticker, name_hint=""):
    url = f"{BASE}/ipo/{ticker}"
    try:
        soup = get(url)
    except Exception as e:
        print(f"  {ticker} 取得失敗: {e}")
        return None

    ipo_date, market, init_price = extract_basic_info(soup)
    lockups = parse_lockup_tables(soup, ipo_date, ticker)

    if not ipo_date:
        print(f"  {ticker}: 上場日を取得できず → スキップ")
        return None

    return {
        "ticker":        ticker,
        "name":          name_hint or ticker,
        "ipo_date":      ipo_date,
        "market":        market or "東証グロース",
        "initial_price": init_price,
        "lockups":       lockups,
        "stock_options": [],
        "notes":         "auto-scraped from ipokabu.net",
        "prospectus_url": ""
    }

# ── 既存データ読み込み ──
def load_existing():
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

# ── 保存 ──
def save(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[OK] {len(data)} stocks saved: {DATA_FILE}")

# ── メイン ──
def main():
    existing  = load_existing()
    ex_map    = {s["ticker"]: s for s in existing}
    updated   = 0
    added     = 0

    for year, month in target_months():
        print(f"\n[{year}/{month:02d}] bancol list...")
        tickers = fetch_month_tickers(year, month)
        print(f"  → {len(tickers)} 銘柄")

        for ticker, name in tickers:
            if ticker in ex_map:
                existing_stock = ex_map[ticker]
                is_auto = "auto-scraped" in existing_stock.get("notes", "")
                # 株数が全部0の場合は自動データで補完する
                all_zero = all(
                    h["shares"] == 0
                    for lk in existing_stock.get("lockups", [])
                    for h in lk.get("holders", [])
                )
                if not is_auto and not all_zero:
                    print(f"  {ticker} {name}: 手動データのためスキップ")
                    continue
                print(f"  {ticker} {name}: 更新中...")
            else:
                print(f"  {ticker} {name}: 新規取得中...")

            stock = fetch_stock(ticker, name)
            if stock:
                ex_map[ticker] = stock
                if ticker in {s["ticker"] for s in existing}:
                    updated += 1
                else:
                    added += 1

    # IPO日降順でソート
    result = sorted(ex_map.values(),
                    key=lambda s: s.get("ipo_date", ""),
                    reverse=True)
    save(result)
    print(f"\n完了: {added} 件追加, {updated} 件更新")

if __name__ == "__main__":
    main()
