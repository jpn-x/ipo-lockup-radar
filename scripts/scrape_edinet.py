#!/usr/bin/env python3
"""
EDINET APIから有価証券届出書PDFを取得し、
「株主の状況」テーブルの実株数で stocks.json を補完するスクリプト

依存: requests, pdfminer.six
使い方: EDINET_API_KEY=<key> python scripts/scrape_edinet.py
"""
import io, json, os, re, sys, time
from datetime import date, timedelta
from pathlib import Path

import requests
from pdfminer.high_level import extract_text_to_fp
from pdfminer.layout import LAParams, LTTextBox
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.converter import PDFPageAggregator

API_KEY   = os.environ.get("EDINET_API_KEY", "")
BASE      = "https://api.edinet-fsa.go.jp/api/v2"
DATA_FILE = Path(__file__).parent.parent / "data" / "stocks.json"

# IPO有価証券届出書のdocTypeCode
TARGET_DOC_TYPES = {"030", "040"}  # 030=有価証券届出書(新規公開時) 040=訂正

SEARCH_BACK_DAYS = 110  # IPO日の何日前まで検索するか
MIN_PDF_BYTES    = 300_000  # これ未満のPDFは訂正版とみなしスキップ

# ── API 共通 ──
def api_get(url, params, delay=0.4, timeout=60):
    time.sleep(delay)
    params["Subscription-Key"] = API_KEY
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r

def get_json(url, params, delay=0.4):
    return api_get(url, params, delay).json()

def get_bytes(url, params, delay=1.5):
    return api_get(url, params, delay, timeout=120).content

# ── EDINET: IPO届出書を検索 ──
def search_ipo_docs(ipo_date_str: str, company_name: str) -> list[dict]:
    """IPO日の前後を検索して届出書を返す（名前マッチあり）"""
    ipo   = date.fromisoformat(ipo_date_str)
    start = ipo - timedelta(days=SEARCH_BACK_DAYS)
    end   = ipo + timedelta(days=5)

    candidates = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # 平日のみ
            try:
                data = get_json(f"{BASE}/documents.json",
                                {"date": cur.isoformat(), "type": 2})
                for doc in data.get("results", []):
                    if doc.get("docTypeCode") in TARGET_DOC_TYPES:
                        fn = doc.get("filerName", "") or ""
                        if name_match(company_name, fn):
                            candidates.append(doc)
            except Exception as e:
                sys.stderr.write(f"  {cur}: {e}\n")
        cur += timedelta(days=1)

    # 原本（030）を優先。なければ訂正版（040）を使う
    originals = [d for d in candidates if d.get("docTypeCode") == "030"]
    return originals if originals else candidates

# ── 名前正規化・マッチング ──
def zen_to_han(s: str) -> str:
    """全角英数字・スペースを半角に変換"""
    result = []
    for c in s:
        cp = ord(c)
        if 0xFF01 <= cp <= 0xFF5E:          # 全角英数記号 → 半角
            result.append(chr(cp - 0xFEE0))
        elif c == "　":                  # 全角スペース → 半角
            result.append(" ")
        else:
            result.append(c)
    return "".join(result)

def normalize(s: str) -> str:
    s = zen_to_han(s)
    s = re.sub(r"[　\s]+", "", s)
    s = re.sub(r"株式会社|（株）|\(株\)|合同会社|有限会社", "", s)
    s = re.sub(r"[（(][^）)]*[）)]", "", s)  # 括弧内除去
    return s.upper()

def name_match(stock: str, filer: str) -> bool:
    sn, fn = normalize(stock), normalize(filer)
    if not sn or not fn:
        return False
    if sn == fn:
        return True
    short, long = (sn, fn) if len(sn) <= len(fn) else (fn, sn)
    if len(short) >= 4 and short in long:
        return True
    n = min(len(sn), len(fn), 7)
    return n >= 4 and sn[:n] == fn[:n]

# ── PDF テキスト抽出（シンプル版） ──
def pdf_to_text(pdf_bytes: bytes) -> str:
    buf_in  = io.BytesIO(pdf_bytes)
    buf_out = io.StringIO()
    laparams = LAParams(line_margin=0.3, word_margin=0.1, char_margin=2.5,
                        boxes_flow=0.5)
    extract_text_to_fp(buf_in, buf_out, laparams=laparams,
                       output_type="text", codec="utf-8")
    return buf_out.getvalue()

# ── PDF 座標ベーステキストボックス抽出 ──
def pdf_to_textboxes(pdf_bytes: bytes) -> list[dict]:
    """各テキストブロックを {x, y, text} のリストで返す"""
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
    from pdfminer.converter import PDFPageAggregator
    from pdfminer.layout import LAParams, LTTextBox

    rsrc = PDFResourceManager()
    laparams = LAParams(line_margin=0.3, word_margin=0.1, char_margin=2.5)
    device = PDFPageAggregator(rsrc, laparams=laparams)
    interp  = PDFPageInterpreter(rsrc, device)

    boxes = []
    buf = io.BytesIO(pdf_bytes)
    for page_num, page in enumerate(PDFPage.get_pages(buf, check_extractable=False)):
        interp.process_page(page)
        layout = device.get_result()
        page_h = layout.height
        for elem in layout:
            if isinstance(elem, LTTextBox):
                t = elem.get_text().strip()
                if t:
                    boxes.append({
                        "x": elem.x0,
                        "y": page_h - elem.y1,  # 上からの距離に変換
                        "w": elem.width,
                        "h": elem.height,
                        "text": t,
                        "page": page_num,
                    })
    return boxes

# ── 「株主の状況」テーブルを座標ベースでパース ──
def parse_shareholders_from_boxes(boxes: list[dict]) -> list[dict]:
    """
    座標情報を使って「株主の状況」テーブルを行単位に再構築し、
    株主名・株数・比率を返す。
    """
    # ページ番号でグループ化
    pages: dict[int, list[dict]] = {}
    for b in boxes:
        pages.setdefault(b["page"], []).append(b)

    shareholders = []

    for page_num, pboxes in sorted(pages.items()):
        page_text = " ".join(b["text"] for b in pboxes)
        if "株主の状況" not in page_text and "大株主" not in page_text:
            continue

        # このページのボックスをy座標でソート → 行グループ化
        pboxes_sorted = sorted(pboxes, key=lambda b: (b["y"], b["x"]))

        # 行グループ化（y座標が近いもの同士を同一行と見なす）
        rows = []
        cur_row = []
        cur_y = None
        Y_THRESHOLD = 8  # ピクセル

        for b in pboxes_sorted:
            if cur_y is None or abs(b["y"] - cur_y) <= Y_THRESHOLD:
                cur_row.append(b)
                cur_y = b["y"] if cur_y is None else (cur_y + b["y"]) / 2
            else:
                if cur_row:
                    rows.append(sorted(cur_row, key=lambda x: x["x"]))
                cur_row = [b]
                cur_y = b["y"]
        if cur_row:
            rows.append(sorted(cur_row, key=lambda x: x["x"]))

        # 各行を解析
        in_section = False
        for row in rows:
            row_text = " ".join(b["text"].replace("\n", " ") for b in row)

            if "株主の状況" in row_text or "大株主" in row_text:
                in_section = True
                continue

            if not in_section:
                continue

            # セクション終了判定
            if re.search(r"(ストックオプション|新株予約権|役員|沿革|事業の概要|第[二三四五六七八九十])", row_text):
                in_section = False
                continue

            # 行から株主名・株数・比率を抽出
            # 数字ブロックを探す
            nums = re.findall(r"[\d,]+(?:\.\d+)?", row_text)
            # 株数候補（4桁以上の整数）と比率候補（xx.xx形式）
            share_cands = []
            ratio_cands = []
            for n in nums:
                if not n or n == ",":
                    continue
                if "." in n:
                    try:
                        v = float(n)
                        if 0.1 <= v <= 100:
                            ratio_cands.append(v)
                    except ValueError:
                        pass
                else:
                    try:
                        v = int(n.replace(",", ""))
                        if v >= 1000 and not (1900 <= v <= 2100):  # 年号除外
                            share_cands.append(v)
                    except ValueError:
                        pass

            if not share_cands and not ratio_cands:
                continue

            # 株主名（数字・記号・住所っぽいものを除いた部分）
            name_part = re.sub(r"[\d,.\(\)（）\-－−\s　〒都道府県市区町村]+", " ", row_text)
            name_part = re.sub(r"\s{2,}", " ", name_part).strip()
            # 短すぎる・住所のみ → スキップ
            if len(name_part) < 2:
                continue
            # 「合計」「計」はスキップ
            if re.search(r"^(合計|計|ヘッダー|氏名|住所|所有)$", name_part.strip()):
                continue

            shares = share_cands[0] if share_cands else 0
            ratio  = ratio_cands[0] if ratio_cands else 0.0

            if shares > 0 or ratio > 0:
                # 「※」以降は注記・住所なので切り落とす
                clean_name = name_part.split("※")[0].strip()
                # 都道府県名以降も除去（住所が混入している場合）
                clean_name = re.split(r"\s+(?:東京|神奈川|埼玉|千葉|大阪|京都|兵庫|北海道|愛知|福岡|沖縄|宮城|広島|静岡|茨城|栃木|群馬|新潟|長野|岐阜|三重|滋賀|奈良|和歌山|岡山|山口|徳島|香川|愛媛|高知|福島|山形|秋田|岩手|青森|富山|石川|福井|山梨|島根|鳥取|佐賀|長崎|熊本|大分|宮崎|鹿児島)", clean_name)[0].strip()
                # ダッシュ・空白のみは除外
                if not clean_name or clean_name in ("―", "-", "—"):
                    continue
                shareholders.append({
                    "name": clean_name,
                    "shares": shares,
                    "ratio": ratio,
                })

    return shareholders

# ── シンプルテキストから大株主を抽出（フォールバック） ──
def parse_shareholders_from_text(text: str) -> list[dict]:
    """
    pdfminerのシンプルテキストから大株主セクションを抽出。
    会社名・株数・比率が1行に連続している場合に有効。
    """
    # セクション特定
    m = re.search(r"(?:大株主|株主の状況)[\s\S]{0,200}(?:所有株式数|氏名又は名称)", text)
    if not m:
        return []

    sec = text[m.start():]
    end = re.search(r"\n(?:第[二三四五六七八九十]|ストックオプション|新株予約権|役員)", sec[200:])
    if end:
        sec = sec[:200 + end.start()]

    # 行パース: 名前 + 株数(カンマ区切り5桁以上) + 比率(xx.xx)
    rows = []
    PAT = re.compile(
        r"([^\d\n]{2,30?}?)"            # 株主名
        r"\s+([\d,]{5,})"               # 株数
        r"(?:\s*[\(（][\d,]+[\)）])?"    # 括弧内株数（省略可）
        r"\s+(\d{1,3}\.\d{1,2})"       # 比率
    )
    for m2 in PAT.finditer(sec):
        name   = m2.group(1).strip()
        shares = int(m2.group(2).replace(",", ""))
        ratio  = float(m2.group(3))
        if len(name) >= 2 and shares >= 1000 and 0.01 <= ratio <= 100:
            rows.append({"name": name, "shares": shares, "ratio": ratio})

    return rows

# ── 比率×総株数で株数を計算（フォールバック） ──
def calc_shares_from_ratio(holder_name: str, total_shares: int) -> int:
    """ホルダー名に含まれる比率から株数を概算"""
    m = re.search(r"(\d{1,3}\.\d{1,2})%?[）)]", holder_name)
    if m and total_shares > 0:
        ratio = float(m.group(1))
        return round(total_shares * ratio / 100)
    return 0

def extract_total_shares(text: str) -> int:
    """PDFテキストから総発行株数を抽出"""
    # 「発行済株式総数」または「総株主の議決権」付近の株数
    for pat in [
        r"発行済株式総数[^\d]*?([\d,]+)\s*株",
        r"完全議決権株式\(その他\)[^\d]+([\d,]+)",
        r"総株主の議決権[^\d]*?([\d,]+)",
    ]:
        m = re.search(pat, text)
        if m:
            v = int(m.group(1).replace(",", ""))
            if v > 10000:
                return v
    return 0

# ── ホルダー名あいまいマッチ ──
def holder_match(holder_name: str, edinet_rows: list[dict]) -> dict | None:
    hn = normalize(holder_name)
    # 比率・括弧を除いて正規化
    hn = re.sub(r"\d+\.\d+", "", hn).strip()
    hn = re.sub(r"[（(][^）)]*[）)]", "", hn).strip()

    best, best_score = None, 0
    for row in edinet_rows:
        # ※・住所を除いたクリーンな名前で比較
        raw_name = row["name"].split("※")[0].strip()
        en = normalize(raw_name)
        if not en:
            continue
        if hn == en:
            return row
        short, long = (hn, en) if len(hn) <= len(en) else (en, hn)
        if len(short) >= 3 and short in long:
            s = len(short)
            if s > best_score:
                best_score, best = s, row
        n = min(len(hn), len(en), 8)
        if n >= 4 and hn[:n] == en[:n]:
            if n > best_score:
                best_score, best = n, row

    return best if best_score >= 3 else None

# ── メイン ──
def load_stocks():
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)

def save_stocks(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def main():
    if not API_KEY:
        print("[SKIP] 環境変数 EDINET_API_KEY が未設定 → EDINETスクレイプをスキップ")
        sys.exit(0)

    stocks = load_stocks()
    total_updated = 0

    # 株数0ホルダーを持つ銘柄のみ対象
    targets = [
        s for s in stocks
        if any(h.get("shares", 0) == 0
               for lk in s.get("lockups", [])
               for h in lk.get("holders", []))
    ]
    print(f"対象: {len(targets)} 銘柄")

    for stock in targets:
        ticker   = stock["ticker"]
        name     = stock["name"]
        ipo_date = stock.get("ipo_date", "")
        if not ipo_date:
            continue

        print(f"\n[{ticker}] {name}  IPO: {ipo_date}")

        # ── EDINET 書類を検索 ──
        print("  EDINET検索中...", end=" ", flush=True)
        docs = search_ipo_docs(ipo_date, name)
        if not docs:
            print("書類なし → スキップ")
            continue
        doc = docs[-1]  # 最新版
        print(f"発見: {doc.get('filerName')} ({doc['docID']} / {doc.get('docDescription','')[:30]})")

        # ── PDF ダウンロード（サイズが小さい訂正版はスキップ） ──
        pdf_bytes = None
        for candidate_doc in docs:
            cid = candidate_doc["docID"]
            print(f"  PDF取得中 {cid}...", end=" ", flush=True)
            try:
                b = get_bytes(f"{BASE}/documents/{cid}", {"type": 2})
            except Exception as e:
                print(f"失敗: {e}")
                continue
            print(f"{len(b)//1024} KB", end=" ")
            if len(b) >= MIN_PDF_BYTES:
                pdf_bytes = b
                print("OK")
                break
            else:
                print("(too small, skip)")

        if pdf_bytes is None:
            print("  -> 有効なPDFが取得できませんでした")
            continue

        # ── テキスト抽出 ──
        try:
            text = pdf_to_text(pdf_bytes)
        except Exception as e:
            print(f"  テキスト抽出失敗: {e}")
            continue

        # 総株数を抽出
        total_shares = extract_total_shares(text)
        print(f"  総発行株数: {total_shares:,}" if total_shares else "  総発行株数: 不明")

        # ── 株主テーブル解析（テキストベース） ──
        edinet_rows = parse_shareholders_from_text(text)

        # テキストベースで取れなければ座標ベースで再試行
        if not edinet_rows:
            print("  テキストパース不発 → 座標ベースで再試行...")
            try:
                boxes = pdf_to_textboxes(pdf_bytes)
                edinet_rows = parse_shareholders_from_boxes(boxes)
            except Exception as e:
                print(f"  座標パースも失敗: {e}")

        # どちらでも取れなければ比率計算のみ
        print(f"  EDINET株主: {len(edinet_rows)} 名")
        for r in edinet_rows[:8]:
            print(f"    {r['name']}: {r['shares']:,}株 ({r['ratio']}%)")

        # ── lockupホルダーに株数を書き込む ──
        updated = 0
        for lk in stock.get("lockups", []):
            for h in lk.get("holders", []):
                if h.get("shares", 0) > 0:
                    continue

                # 1) EDINET テーブルとマッチング
                match = holder_match(h["name"], edinet_rows) if edinet_rows else None
                if match and match["shares"] > 0:
                    h["shares"] = match["shares"]
                    if match.get("ratio", 0) > 0:
                        h["ratio"] = match["ratio"]
                    elif total_shares > 0:
                        h["ratio"] = round(match["shares"] / total_shares * 100, 2)
                    print(f"    [EDINET] {h['name']} → {match['shares']:,}株")
                    updated += 1

                # 2) 名前中の比率×総株数でフォールバック計算
                elif total_shares > 0:
                    est = calc_shares_from_ratio(h["name"], total_shares)
                    if est > 0:
                        h["shares"] = est
                        h["ratio"] = round(est / total_shares * 100, 2)
                        print(f"    [比率計算] {h['name']} → {est:,}株 (推定)")
                        updated += 1

            # lockup合計を再計算
            lk["shares"] = sum(h.get("shares", 0) for h in lk.get("holders", []))

        if updated:
            if total_shares > 0:
                stock["total_shares"] = total_shares
            stock["notes"] = stock.get("notes", "") + f" +edinet:{date.today()}"
            total_updated += 1
            print(f"  -> {updated} ホルダー更新")
        else:
            print(f"  -> マッチなし（手動確認推奨）")

    save_stocks(stocks)
    print(f"\n完了: {total_updated}/{len(targets)} 銘柄更新 → {DATA_FILE}")

if __name__ == "__main__":
    main()
