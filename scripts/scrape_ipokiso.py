#!/usr/bin/env python3
"""
ipokiso.com から直近IPO銘柄のロックアップ情報を自動取得して
data/stocks.json を更新するスクリプト

依存: requests, beautifulsoup4, lxml
"""
import json, re, time, sys
from datetime import date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE      = "https://www.ipokiso.com"
DATA_FILE = Path(__file__).parent.parent / "data" / "stocks.json"
HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; ipo-lockup-radar/1.0)"}

MONTHS_MAP = {
    1:"01",2:"02",3:"03",4:"04",5:"05",6:"06",
    7:"07",8:"08",9:"09",10:"10",11:"11",12:"12"
}

def get(url, delay=1.5):
    time.sleep(delay)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "lxml")

# ── インデックスページから銘柄URLを収集 ──
def fetch_company_urls(years):
    urls = []
    seen = set()

    # メインインデックスから全年分をまとめて取得
    index_url = f"{BASE}/company/index.html"
    try:
        soup = get(index_url)
        for year in years:
            pat = re.compile(rf"/company/{year}/[^/]+\.html$")
            for a in soup.find_all("a", href=pat):
                href = a["href"]
                if href not in seen:
                    seen.add(href)
                    urls.append((year, href))
    except Exception as e:
        print(f"  index.html 取得失敗: {e}")

    return urls

# ── ページ本文から基本情報を抽出 ──
def extract_basic(soup, year):
    text = soup.get_text(" ", strip=True)

    # 銘柄コード: （123A） or (123A)
    ticker = None
    m = re.search(r"[（(](\d{3,4}[A-Za-z]?)[）)]", text)
    if m:
        ticker = m.group(1)

    # 銘柄名: タイトルタグから
    name = ""
    title = soup.find("title")
    if title:
        t = title.get_text()
        t = re.sub(r"[（(]\d{3,4}[A-Za-z]?[）)]", "", t)  # 「（598A）」形式を丸ごと除去
        t = re.sub(r"のIPO.*", "", t)
        t = re.sub(r"【.*?】", "", t)
        t = re.sub(r"[（(]\s*[）)]", "", t)  # 残った空括弧を除去
        name = t.strip(" 　[]（）()")

    # 市場
    market = "東証グロース"
    for kw, label in [("プライム","東証プライム"),("スタンダード","東証スタンダード"),("グロース","東証グロース")]:
        if kw in text:
            market = label
            break

    # 上場日（月日のみ → 年は year パラメータで補完）
    ipo_date = None
    m = re.search(r"上場日[^\d]*(\d{1,2})月(\d{1,2})日", text)
    if m:
        mo, day = int(m.group(1)), int(m.group(2))
        ipo_date = f"{year}-{mo:02d}-{day:02d}"

    # 公開価格
    init_price = 0
    m = re.search(r"公募価格[^\d]*([0-9,]+)円", text)
    if m:
        init_price = int(m.group(1).replace(",",""))

    return ticker, name, market, ipo_date, init_price

# ── ロックアップ解除日を計算 ──
def calc_release(ipo_date_str, days):
    try:
        d = date.fromisoformat(ipo_date_str) + timedelta(days=days)
        return d.isoformat()
    except Exception:
        return None

# ── ホルダー種別推定 ──
def guess_type(name):
    n = name
    if any(k in n for k in ["ファンド","投資事業","キャピタル","インキュベ","グロービス",
                              "ANRI","ジャフコ","Ventures","Venture","East","WiL",
                              "L.P.","組合","Cayman","PE","CVC","VC","Angel","投資家"]):
        return "vc"
    if any(k in n for k in ["社長","代表取締役","創業者","資産管理","会長","CEO"]):
        return "founder"
    if any(k in n for k in ["取締役","役員","監査役","持株会","従業員","執行役員"]):
        return "other"
    if any(k in n for k in ["銀行","証券","保険","商事","造船","電機","自動車",
                              "通信","ドコモ","トヨタ","DeNA","KDDI","NTT",
                              "三菱","住友","伊藤忠","丸紅","日立","ソニー"]):
        return "corporate"
    return "other"

# ── ロックアップテーブルをパース ──
def parse_lockups(soup, ipo_date):
    lockups_raw = {}   # release_date -> {label, shares, holders}

    for table in soup.find_all("table"):
        ths = [th.get_text(strip=True) for th in table.find_all("th")]
        # 株主名 / 比率 / ロックアップ のカラムを探す
        name_idx  = next((i for i,h in enumerate(ths) if "株主" in h or "氏名" in h or "名前" in h), None)
        ratio_idx = next((i for i,h in enumerate(ths) if "比率" in h or "割合" in h or "保有" in h), None)
        lock_idx  = next((i for i,h in enumerate(ths) if "ロック" in h or "制限" in h or "期間" in h), None)

        if name_idx is None or lock_idx is None:
            continue

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td","th"])
            if len(cells) <= lock_idx:
                continue

            holder_name = cells[name_idx].get_text(" ", strip=True)
            holder_name = re.sub(r"\s+", " ", holder_name)
            if not holder_name or holder_name in ["-","—","合計"]:
                continue

            lock_text = cells[lock_idx].get_text(strip=True)

            # ロック日数を解析（360日 / 180日 / 90日 / 継続所有）
            if "継続所有" in lock_text or "なし" in lock_text:
                continue
            days = 180
            if "360" in lock_text:
                days = 360
            elif "90" in lock_text:
                days = 90

            # 1.5倍条件・2倍条件の注記
            condition = ""
            if "1.5倍" in lock_text:
                condition = "/1.5倍条件"
            elif "2倍" in lock_text:
                condition = "/2倍条件"

            release = calc_release(ipo_date, days) if ipo_date else None
            if not release:
                continue

            htype = guess_type(holder_name)

            key = release
            if key not in lockups_raw:
                label_base = "VC・主要株主" if htype == "vc" else "主要株主"
                lockups_raw[key] = {
                    "label": f"{label_base}（{days}日{condition}）",
                    "release_date": release,
                    "shares": 0,
                    "holders": [],
                    "days": days
                }
            # 比率を取得
            ratio = 0.0
            if ratio_idx is not None and len(cells) > ratio_idx:
                ratio_text = cells[ratio_idx].get_text(strip=True)
                m = re.search(r"(\d+\.\d+)", ratio_text)
                if m:
                    try:
                        ratio = float(m.group(1))
                    except ValueError:
                        pass

            holder = {"name": holder_name, "shares": 0, "type": htype}
            if ratio > 0:
                holder["ratio"] = ratio
            lockups_raw[key]["holders"].append(holder)
            # VCがいたらラベル更新
            if htype == "vc":
                d = lockups_raw[key]["days"]
                cond = condition
                lockups_raw[key]["label"] = f"VC・主要株主（{d}日{cond}）"

    return sorted(lockups_raw.values(), key=lambda x: x["release_date"])

# ── 1銘柄をフェッチ ──
def fetch_stock(year, url_path):
    url = BASE + url_path if url_path.startswith("/") else url_path
    try:
        soup = get(url)
    except Exception as e:
        print(f"    取得失敗: {e}")
        return None

    ticker, name, market, ipo_date, init_price = extract_basic(soup, year)
    if not ticker:
        print(f"    銘柄コード不明 → スキップ")
        return None
    if not ipo_date:
        print(f"    {ticker}: 上場日不明 → スキップ")
        return None

    lockups = parse_lockups(soup, ipo_date)
    if not lockups:
        # ロックアップ情報がなければ180日で最小エントリ
        rel = calc_release(ipo_date, 180)
        if rel:
            lockups = [{"label":"主要株主（180日）","release_date":rel,"shares":0,"holders":[]}]

    return {
        "ticker":        ticker,
        "name":          name,
        "ipo_date":      ipo_date,
        "market":        market,
        "initial_price": init_price,
        "lockups":       lockups,
        "stock_options": [],
        "notes":         f"auto-scraped from ipokiso.com {date.today()}",
        "prospectus_url": url
    }

# ── 既存データ ──
def load_existing():
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

def save(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[OK] {len(data)} stocks -> {DATA_FILE}")

# ── 対象年リスト（直近1年分） ──
def target_years():
    today = date.today()
    years = [today.year]
    years.append(today.year - 1)   # 常に前年も含める
    return years

# ── メイン ──
def main():
    existing = load_existing()
    ex_map   = {s["ticker"]: s for s in existing}
    added = updated = 0

    years = target_years()
    print(f"対象年: {years}")
    company_urls = fetch_company_urls(years)
    print(f"銘柄URL: {len(company_urls)} 件")

    for year, url_path in company_urls:
        slug = url_path.rstrip("/").rsplit("/", 1)[-1]
        print(f"  [{year}] {slug} ...", end=" ", flush=True)

        # 先にページを取ってtickerを確認（軽量処理）
        # 既存の手動データは上書きしない
        # → 一旦fetchして判断する
        stock = fetch_stock(year, url_path)
        if not stock:
            print("skip")
            continue

        ticker = stock["ticker"]
        is_new = ticker not in ex_map
        existing_stock = ex_map.get(ticker, {})
        is_auto = "auto-scraped" in existing_stock.get("notes","")
        # 株数が全部0なら自動補完OK
        all_zero = all(
            h["shares"] == 0
            for lk in existing_stock.get("lockups",[])
            for h in lk.get("holders",[])
        )

        if not is_new and not is_auto and not all_zero:
            print(f"manual({ticker}) skip")
            continue

        # EDINETで補完済みのshares/ratio/total_sharesを引き継ぐ
        if not is_new:
            ex = existing_stock
            # total_shares を保持
            if ex.get("total_shares"):
                stock["total_shares"] = ex["total_shares"]
            # ホルダーの shares/ratio をマージ（名前で照合）
            ex_holders = {
                h["name"]: h
                for lk in ex.get("lockups", [])
                for h in lk.get("holders", [])
            }
            for lk in stock.get("lockups", []):
                for h in lk.get("holders", []):
                    prev = ex_holders.get(h["name"])
                    if prev:
                        if prev.get("shares", 0) > 0:
                            h["shares"] = prev["shares"]
                        if prev.get("ratio", 0) > 0:
                            h["ratio"] = prev["ratio"]
                lk["shares"] = sum(h.get("shares", 0) for h in lk.get("holders", []))

        ex_map[ticker] = stock
        if is_new:
            added += 1
            print(f"added({ticker})")
        else:
            updated += 1
            print(f"updated({ticker})")

    result = sorted(
        ex_map.values(),
        key=lambda s: s.get("ipo_date",""),
        reverse=True
    )
    save(result)
    print(f"\n完了: {added} 追加 / {updated} 更新")

if __name__ == "__main__":
    main()
