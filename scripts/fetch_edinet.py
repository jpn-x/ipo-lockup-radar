#!/usr/bin/env python3
"""
EDINET APIから新規上場銘柄の目論見書一覧を取得するスクリプト
（参考: https://disclosure2.edinet-fsa.go.jp/API/v2/documents.json）

使い方:
  python scripts/fetch_edinet.py --days 30
"""
import json, argparse, urllib.request
from datetime import date, timedelta

EDINET_API = "https://disclosure2.edinet-fsa.go.jp/API/v2/documents.json"

def fetch_edinet(target_date: str) -> list:
    url = f"{EDINET_API}?date={target_date}&type=2"
    try:
        with urllib.request.urlopen(url, timeout=15) as res:
            data = json.loads(res.read())
        results = data.get("results", [])
        # 目論見書(有価証券届出書)に絞る
        prospectus = [r for r in results if "目論見書" in (r.get("docDescription") or "") or
                      r.get("docTypeCode") in ("030", "032")]
        return prospectus
    except Exception as e:
        print(f"  EDINET {target_date}: {e}")
        return []

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=14, help="遡る日数")
    args = p.parse_args()

    today = date.today()
    all_docs = []
    for i in range(args.days):
        d = today - timedelta(days=i)
        docs = fetch_edinet(d.isoformat())
        if docs:
            print(f"{d}: {len(docs)}件")
            all_docs.extend(docs)
        else:
            print(f"{d}: 0件")

    print(f"\n合計 {len(all_docs)} 件の目論見書関連書類")
    for doc in all_docs[:20]:
        print(f"  {doc.get('submitDateTime','')[:10]}  {doc.get('filerName',''):<30}  {doc.get('docDescription','')}")

if __name__ == "__main__":
    main()
