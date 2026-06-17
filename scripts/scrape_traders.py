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

def get(url, delay=3.0):
    time.sleep(delay)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "lxml")

_SMALL_KANA = str.maketrans("ァィゥェォッャュョヮヵヶぁぃぅぇぉっゃゅょゎ",
                             "アイウエオツヤユヨワカケあいうえおつやゆよわ")

def normalize(name):
    name = re.sub(r"[　\s]+", "", name)
    name = re.sub(r"株式会社|（株）|\(株\)|合同会社|有限会社|㈱", "", name)
    name = name.replace("（", "(").replace("）", ")")
    name = name.translate(_SMALL_KANA)   # 小書き仮名 → 大書き（ウェ→ウエ等）
    return name.upper()

def _guess_type(name):
    if any(k in name for k in ["ファンド","投資事業","キャピタル","Ventures","組合","VC","L.P.","CVC","投資業"]):
        return "vc"
    if any(k in name for k in ["社長","代表取締役","創業者","会長","CEO","代表執行役"]):
        return "founder"
    if any(k in name for k in ["銀行","証券","保険","商事","造船","電機","三菱","住友","伊藤忠","KDDI","NTT","トヨタ","ドコモ"]):
        return "corporate"
    return "other"

def name_match(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(short) >= 3 and short in long

def parse_shareholders(soup):
    """大株主テーブルをパース → [{name, tekiyo, shares, ratio}]"""
    result = []
    for table in soup.find_all("table"):
        ths = [th.get_text(strip=True) for th in table.find_all("th")]
        # 「大株主名」列を優先、なければ「株主名」「氏名」で探す
        name_idx   = next((i for i, h in enumerate(ths) if "大株主" in h), None)
        if name_idx is None:
            name_idx = next((i for i, h in enumerate(ths) if h in ("株主名", "氏名")), None)
        tekiyo_idx = next((i for i, h in enumerate(ths) if "摘要" in h or "属性" in h), None)
        share_idx  = next((i for i, h in enumerate(ths) if "株数" in h or "株式数" in h), None)
        ratio_idx  = next((i for i, h in enumerate(ths) if "比率" in h or "割合" in h), None)
        if name_idx is None or ratio_idx is None:
            continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= name_idx:
                continue
            name = re.sub(r"\s+", " ", cells[name_idx].get_text(" ", strip=True))
            if not name or name in ["-", "—", "合計"]:
                continue
            tekiyo = ""
            if tekiyo_idx is not None and len(cells) > tekiyo_idx:
                tekiyo = re.sub(r"\s+", " ", cells[tekiyo_idx].get_text(" ", strip=True))
            shares = 0
            if share_idx is not None and len(cells) > share_idx:
                raw = cells[share_idx].get_text(strip=True).replace(",", "")
                digits = re.sub(r"[^\d]", "", raw)
                if digits:
                    val = int(digits)
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
                result.append({"name": name, "tekiyo": tekiyo, "shares": shares, "ratio": ratio})
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

        # ── 既存ホルダーを更新（shares / ratio / tekiyo 補完） ──
        for lk in stock.get("lockups", []):
            lk_changed = False
            for h in lk.get("holders", []):
                has_shares = h.get("shares", 0) > 0
                has_ratio  = h.get("ratio", 0) > 0
                has_tekiyo = bool(h.get("tekiyo", ""))

                for src in traders_holders:
                    if not name_match(h["name"], src["name"]):
                        continue
                    # 株数: 未設定 OR traders値と現在値が大きく乖離（10倍以上）なら上書き
                    if src["shares"] > 0:
                        cur = h.get("shares", 0)
                        if cur == 0 or (cur > src["shares"] * 5):
                            h["shares"] = src["shares"]
                            lk_changed = True
                            stock_updated = True
                    if src["ratio"] > 0:
                        h["ratio"] = src["ratio"]
                        lk_changed = True
                        stock_updated = True
                    if src.get("tekiyo"):
                        h["tekiyo"] = src["tekiyo"]
                        stock_updated = True
                    break

            if lk_changed:
                lk["shares"] = sum(h.get("shares", 0) for h in lk["holders"])

        # ── traders にいて JSON にいない株主を追加 ──
        # 全既存ホルダー名を収集
        existing_names = [
            h["name"]
            for lk in stock.get("lockups", [])
            for h in lk.get("holders", [])
        ]
        # 非継続所有のロックアップグループ（追加先候補）
        non_cont_lks = [
            lk for lk in stock.get("lockups", [])
            if lk.get("release_date") != "2099-12-31"
        ]
        if non_cont_lks:
            # 最もホルダー数が多いグループを追加先とする
            target_lk = max(non_cont_lks, key=lambda lk: len(lk.get("holders", [])))
            added_names = []
            for src in traders_holders:
                # 既存ホルダーと一致するものはスキップ
                if any(name_match(src["name"], en) for en in existing_names):
                    continue
                # 継続所有系（会社役員など）は別途スキップしない — すべて追加
                new_holder = {
                    "name": src["name"],
                    "shares": src["shares"],
                    "type": _guess_type(src["name"]),
                }
                if src["ratio"] > 0:
                    new_holder["ratio"] = src["ratio"]
                if src.get("tekiyo"):
                    new_holder["tekiyo"] = src["tekiyo"]
                target_lk["holders"].append(new_holder)
                existing_names.append(src["name"])
                added_names.append(src["name"])
                stock_updated = True
            if added_names:
                target_lk["shares"] = sum(h.get("shares", 0) for h in target_lk["holders"])
                print(f"+{len(added_names)}名追加 ", end="")

        # ── total_shares がある場合、ratio → shares 計算 ──
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
