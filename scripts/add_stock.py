#!/usr/bin/env python3
"""
IPOロックアップデータ追加ヘルパー
目論見書から抽出した情報をstocks.jsonに追記するCLIツール

使い方:
  python scripts/add_stock.py --ticker 123A --name "会社名" --ipo 2025-01-15 ...
"""
import json, argparse, sys
from pathlib import Path

DATA_FILE = Path(__file__).parent.parent / "data" / "stocks.json"


def load():
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def save(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ {DATA_FILE} を更新しました（{len(data)}銘柄）")


def main():
    p = argparse.ArgumentParser(description="stocks.jsonに銘柄を追加")
    p.add_argument("--ticker", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--ipo", required=True, help="上場日 YYYY-MM-DD")
    p.add_argument("--market", default="東証グロース")
    p.add_argument("--initial-price", type=int, default=0)
    p.add_argument("--lockup-date", required=True, help="メインロックアップ解除日 YYYY-MM-DD")
    p.add_argument("--lockup-shares", type=int, default=0)
    p.add_argument("--lockup-label", default="主要株主・VC（180日）")
    p.add_argument("--so-shares", type=int, default=0, help="1円SO株数（0=なし）")
    p.add_argument("--so-date", default="", help="1円SO行使可能日 YYYY-MM-DD")
    p.add_argument("--notes", default="")
    args = p.parse_args()

    stocks = load()

    # 重複チェック
    if any(s["ticker"] == args.ticker for s in stocks):
        print(f"⚠️  {args.ticker} は既に登録済みです。手動で編集してください。")
        sys.exit(1)

    entry = {
        "ticker": args.ticker,
        "name": args.name,
        "ipo_date": args.ipo,
        "market": args.market,
        "initial_price": args.initial_price,
        "lockups": [
            {
                "label": args.lockup_label,
                "release_date": args.lockup_date,
                "shares": args.lockup_shares,
                "holders": []
            }
        ],
        "stock_options": [],
        "notes": args.notes,
        "prospectus_url": ""
    }

    if args.so_shares > 0 and args.so_date:
        entry["stock_options"].append({
            "holder": "役員・従業員",
            "shares": args.so_shares,
            "strike_price": 1,
            "exercisable_from": args.so_date,
            "note": "1円SO"
        })

    stocks.append(entry)
    save(stocks)
    print(f"追加: {args.ticker} {args.name}")
    print("※ holders（大株主詳細）は data/stocks.json を直接編集してください")


if __name__ == "__main__":
    main()
