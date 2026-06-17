#!/usr/bin/env python3
"""
traders.co.jp から全銘柄の大株主（株数・比率）を取得して stocks.json を補完する。
URL パターン: https://www.traders.co.jp/ipo/{ticker}
既存の shares / ratio は上書きしない。
"""
import json, re, time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE      = "https://www.traders.co.jp/ipo"
DATA_FILE = Path(__file__).parent.parent / "data" / "stocks.json"
HEADERS   = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ja,en;q=0.9",
}

def get(url, delay=1.5):
    time.sleep(delay)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "lxml")

def normalize(name):
    name = re.sub(r"[　\s]+", "", name)
    name = re.sub(r"株式会社|（株）|\(株\)|合同会社|有限会社|㈱", "", name)
    name = name.replace("（", "(").replace("）", ")")
    return name.upper()

def name_match(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(short) >= 3 and short in long

def parse_shareholders(soup):
    """大株主テーブルをパース → [{name, shares, ratio}]"""
    result = []
    for table in soup.find_all("table"):
        ths = [th.get_text(strip=True) for th in table.find_all("th")]
        # 大株主名 / 株数 / 比率 の列を探す
        name_idx  = next((i for i, h in enumerate(ths) if "株主" in h or "氏名" in h), None)
        share_idx = next((i for i, h in enumerate(ths) if "株数" in h or "株式数" in h), None)
        ratio_idx = next((i for i, h in enumerate(ths) if "比率" in h or "割合" in h), None)
        if name_idx is None:
            continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= name_idx:
                continue
            name = re.sub(r"\s+", " ", cells[name_idx].get_text(" ", strip=True))
            if not name or name in ["-", "—", "合計"]:
                continue
            shares = 0
            if share_idx is not None and len(cells) > share_idx:
                m = re.search(r"[\d,]+", cells[share_idx].get_text(strip=True).replace(",", ""))
                if m:
                    raw = cells[share_idx].get_text(strip=True).replace(",", "")
                    digits = re.sub(r"[^\d]", "", raw)
                    if digits:
                        val = int(digits)
                        # 年っぽい数字（1900〜2100）は除外
                        if not (1900 <= val <= 2100):
                            shares = val
            ratio = 0.0
            if ratio_idx is not None and len(cells) > ratio_idx:
                m = re.search(r"(\d+\.\d+)", cells[ratio_idx].get_text(strip=True))
                if m:
                    try:
                        ratio = float(m.group(1))
                    except ValueError:
                        pass
            if name:
                result.append({"name": name, "shares": shares, "ratio": ratio})
    return result

def parse_total_shares(soup):
    """発行済株式総数をページから抽出"""
    text = soup.get_text(" ", strip=True)
    for pat in [
        r"発行済株式[総数]*[^\d]*([\d,]+)\s*株",
        r"株式総数[^\d]*([\d,]+)",
        r"総株式数[^\d]*([\d,]+)",
    ]:
        m = re.search(pat, text)
        if m:
            val = int(m.group(1).replace(",", ""))
            if val > 100000:  # 最低10万株以上
                return val
    return 0

def main():
    with open(DATA_FILE, encoding="utf-8") as f:
        stocks = json.load(f)

    updated_count = 0

    for stock in stocks:
        ticker = stock["ticker"]
        url = f"{BASE}/{ticker}"

        print(f"[{ticker}] {stock['name']} ...", end=" ", flush=True)

        try:
            soup = get(url)
        except requests.HTTPError as e:
            print(f"HTTP {e.response.status_code} → スキップ")
            continue
        except Exception as e:
            print(f"取得失敗: {e}")
            continue

        traders_holders = parse_shareholders(soup)
        if not traders_holders:
            print("データなし")
            continue

        # total_shares を補完（未設定の場合のみ）
        if not stock.get("total_shares"):
            ts = parse_total_shares(soup)
            if ts > 0:
                stock["total_shares"] = ts

        stock_updated = False

        for lk in stock.get("lockups", []):
            lk_changed = False
            for h in lk.get("holders", []):
                # shares と ratio どちらも埋まっていればスキップ
                has_shares = h.get("shares", 0) > 0
                has_ratio  = h.get("ratio", 0) > 0
                if has_shares and has_ratio:
                    continue

                for src in traders_holders:
                    if not name_match(h["name"], src["name"]):
                        continue
                    if not has_shares and src["shares"] > 0:
                        h["shares"] = src["shares"]
                        lk_changed = True
                        stock_updated = True
                    if not has_ratio and src["ratio"] > 0:
                        h["ratio"] = src["ratio"]
                        lk_changed = True
                        stock_updated = True
                    break

            if lk_changed:
                lk["shares"] = sum(h.get("shares", 0) for h in lk["holders"])

        # total_shares がある場合、ratio から shares を計算（shares=0 のホルダー）
        ts = stock.get("total_shares", 0)
        if ts > 0:
            for lk in stock.get("lockups", []):
                lk_changed = False
                for h in lk.get("holders", []):
                    if h.get("shares", 0) == 0 and h.get("ratio", 0) > 0:
                        h["shares"] = round(ts * h["ratio"] / 100)
                        lk_changed = True
                        stock_updated = True
                if lk_changed:
                    lk["shares"] = sum(h.get("shares", 0) for h in lk["holders"])

        if stock_updated:
            updated_count += 1
            print("OK")
        else:
            print("変更なし")

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(stocks, f, ensure_ascii=False, indent=2)

    print(f"\n完了: {updated_count} 銘柄更新 → {DATA_FILE}")

if __name__ == "__main__":
    main()
