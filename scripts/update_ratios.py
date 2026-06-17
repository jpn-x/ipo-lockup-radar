#!/usr/bin/env python3
"""
全銘柄の ipokiso ページから比率(%)と継続所有ホルダーを補完する。
既存の shares / ratio は上書きしない。
"""
import json, re, time
from pathlib import Path
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

BASE      = "https://www.ipokiso.com"
DATA_FILE = Path(__file__).parent.parent / "data" / "stocks.json"
HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; ipo-lockup-radar/1.0)"}

def get(url, delay=1.2):
    time.sleep(delay)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "lxml")

def build_ticker_url_map(years):
    """ipokiso index から ticker→URL マップを構築"""
    ticker_map = {}
    index_url = f"{BASE}/company/index.html"
    try:
        soup = get(index_url)
    except Exception as e:
        print(f"index取得失敗: {e}")
        return ticker_map

    for year in years:
        pat = re.compile(rf"/company/{year}/[^/]+\.html$")
        for a in soup.find_all("a", href=pat):
            href = a["href"]
            url = BASE + href
            # ページを取得してtickerを確認
            try:
                psoup = get(url, delay=1.0)
                text = psoup.get_text(" ", strip=True)
                m = re.search(r"[（(](\d{3,4}[A-Za-z]?)[）)]", text)
                if m:
                    ticker = m.group(1)
                    ticker_map[ticker] = url
                    print(f"  {ticker} → {href}")
            except Exception as e:
                print(f"  {href}: {e}")

    return ticker_map

def normalize_name(n):
    n = re.sub(r"[　\s]+", "", n)
    n = re.sub(r"株式会社|（株）|\(株\)|合同会社|有限会社", "", n)
    return n.upper()

def name_similar(a, b):
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(short) >= 3 and short in long

def calc_release(ipo_str, days):
    d = date.fromisoformat(ipo_str) + timedelta(days=days)
    return d.isoformat()

def parse_ipokiso_holders(soup, ipo_date):
    result = {"lockup": [], "cont": []}
    for table in soup.find_all("table"):
        ths = [th.get_text(strip=True) for th in table.find_all("th")]
        name_idx  = next((i for i,h in enumerate(ths) if "株主" in h or "氏名" in h), None)
        ratio_idx = next((i for i,h in enumerate(ths) if "比率" in h or "割合" in h or "保有" in h), None)
        lock_idx  = next((i for i,h in enumerate(ths) if "ロック" in h or "期間" in h), None)
        if name_idx is None or lock_idx is None:
            continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td","th"])
            if len(cells) <= lock_idx:
                continue
            name = re.sub(r"\s+", " ", cells[name_idx].get_text(" ", strip=True))
            if not name or name in ["-","—","合計"]:
                continue
            lock_text = cells[lock_idx].get_text(strip=True)
            if "なし" in lock_text:
                continue
            ratio = 0.0
            if ratio_idx is not None and len(cells) > ratio_idx:
                m = re.search(r"(\d+\.\d+)", cells[ratio_idx].get_text(strip=True))
                if m:
                    try: ratio = float(m.group(1))
                    except: pass
            if "継続所有" in lock_text:
                result["cont"].append({"name": name, "ratio": ratio})
            else:
                days = 180
                if "360" in lock_text: days = 360
                elif "90" in lock_text: days = 90
                result["lockup"].append({
                    "name": name, "ratio": ratio, "days": days,
                    "release": calc_release(ipo_date, days) if ipo_date else None
                })
    return result

def _guess_type(name):
    if any(k in name for k in ["ファンド","投資事業","キャピタル","Ventures","組合","VC","L.P.","CVC"]):
        return "vc"
    if any(k in name for k in ["社長","代表取締役","創業者","会長","CEO"]):
        return "founder"
    if any(k in name for k in ["銀行","証券","保険","商事","造船","電機","トヨタ","ドコモ","三菱","住友","伊藤忠","KDDI","NTT"]):
        return "corporate"
    return "other"

def main():
    with open(DATA_FILE, encoding="utf-8") as f:
        stocks = json.load(f)

    # URL未設定の銘柄を対象に ipokiso index からURL取得
    no_url = [s for s in stocks if not s.get("prospectus_url") or "ipokiso.com" not in s.get("prospectus_url","")]
    print(f"URLなし銘柄: {len(no_url)} 件 → ipokiso indexから検索")

    years = list(range(2023, date.today().year + 1))
    print(f"検索年: {years}")
    ticker_map = build_ticker_url_map(years)
    print(f"発見: {len(ticker_map)} 銘柄")

    # URLを stocks に保存
    for s in stocks:
        if s["ticker"] in ticker_map and ("ipokiso.com" not in s.get("prospectus_url","")):
            s["prospectus_url"] = ticker_map[s["ticker"]]

    updated_count = 0

    for stock in stocks:
        url = stock.get("prospectus_url", "")
        if not url or "ipokiso.com" not in url:
            continue

        ipo_date = stock.get("ipo_date", "")
        if not ipo_date:
            continue

        print(f"[{stock['ticker']}] {stock['name']} ...", end=" ", flush=True)
        try:
            soup = get(url)
        except Exception as e:
            print(f"取得失敗: {e}")
            continue

        ipokiso = parse_ipokiso_holders(soup, ipo_date)
        if not ipokiso["lockup"] and not ipokiso["cont"]:
            print("データなし")
            continue

        stock_updated = False

        # 既存lockupホルダーに ratio を補完
        for lk in stock.get("lockups", []):
            if lk.get("release_date") == "2099-12-31":
                continue
            for h in lk.get("holders", []):
                if h.get("ratio", 0) > 0:
                    continue
                for src in ipokiso["lockup"]:
                    if name_similar(h["name"], src["name"]) and src["ratio"] > 0:
                        h["ratio"] = src["ratio"]
                        stock_updated = True
                        break

        # 継続所有グループを追加（なければ）
        has_cont = any(lk.get("release_date") == "2099-12-31" for lk in stock.get("lockups", []))
        if not has_cont and ipokiso["cont"]:
            ts = stock.get("total_shares", 0)
            cont_holders = []
            for src in ipokiso["cont"]:
                h = {"name": src["name"], "shares": 0, "type": _guess_type(src["name"])}
                if src["ratio"] > 0:
                    h["ratio"] = src["ratio"]
                    if ts > 0:
                        h["shares"] = round(ts * src["ratio"] / 100)
                cont_holders.append(h)
            total_cont = sum(h["shares"] for h in cont_holders)
            stock["lockups"].append({
                "label": "継続所有（売却制限なし）",
                "release_date": "2099-12-31",
                "shares": total_cont,
                "holders": cont_holders
            })
            stock_updated = True
            print(f"継続所有 {len(cont_holders)}名追加 ", end="")

        # ratio から shares を計算（shares=0 かつ total_shares あり）
        ts = stock.get("total_shares", 0)
        if ts > 0:
            for lk in stock.get("lockups", []):
                changed = False
                for h in lk.get("holders", []):
                    if h.get("shares", 0) == 0 and h.get("ratio", 0) > 0:
                        h["shares"] = round(ts * h["ratio"] / 100)
                        changed = True
                        stock_updated = True
                if changed:
                    lk["shares"] = sum(h.get("shares", 0) for h in lk["holders"])

        print("OK" if stock_updated else "変更なし")
        if stock_updated:
            updated_count += 1

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(stocks, f, ensure_ascii=False, indent=2)
    print(f"\n完了: {updated_count} 銘柄更新 → {DATA_FILE}")

if __name__ == "__main__":
    main()
