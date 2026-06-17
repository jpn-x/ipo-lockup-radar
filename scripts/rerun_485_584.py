#!/usr/bin/env python3
"""485A と 584A のみ EDINET 再取得"""
import json, sys, os
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent))
import scrape_edinet as e

stocks = e.load_stocks()
targets = [s for s in stocks if s["ticker"] in ("485A", "584A")]
print(f"Target: {len(targets)} stocks")
total_updated = 0

for stock in targets:
    ticker   = stock["ticker"]
    name     = stock["name"]
    ipo_date = stock.get("ipo_date", "")
    print(f"\n[{ticker}] {name}  IPO: {ipo_date}")

    docs = e.search_ipo_docs(ipo_date, name)
    if not docs:
        print("  No docs found")
        continue
    doc = docs[-1]
    print(f"  Found: {doc.get('filerName')} ({doc['docID']})")

    pdf_bytes = None
    for cd in docs:
        cid = cd["docID"]
        print(f"  Getting PDF {cid}...", end=" ", flush=True)
        try:
            b = e.get_bytes(f"{e.BASE}/documents/{cid}", {"type": 2})
        except Exception as ex:
            print(f"failed: {ex}")
            continue
        print(f"{len(b)//1024} KB", end=" ")
        if len(b) >= e.MIN_PDF_BYTES:
            pdf_bytes = b
            print("OK")
            break
        else:
            print("(too small, skip)")

    if not pdf_bytes:
        print("  No valid PDF")
        continue

    text = e.pdf_to_text(pdf_bytes)
    total_shares = e.extract_total_shares(text)
    if total_shares:
        print(f"  Total shares: {total_shares:,}")
    else:
        print("  Total shares: unknown")

    edinet_rows = e.parse_shareholders_from_text(text)
    if not edinet_rows:
        print("  Text parse failed, trying coordinate-based...")
        boxes = e.pdf_to_textboxes(pdf_bytes)
        edinet_rows = e.parse_shareholders_from_boxes(boxes)

    print(f"  EDINET shareholders: {len(edinet_rows)}")
    for r in edinet_rows[:10]:
        print(f"    {r['name']}: {r['shares']:,} ({r['ratio']}%)")

    updated = 0
    for lk in stock.get("lockups", []):
        for h in lk.get("holders", []):
            if h.get("shares", 0) > 0:
                continue
            match = e.holder_match(h["name"], edinet_rows) if edinet_rows else None
            if match and match["shares"] > 0:
                h["shares"] = match["shares"]
                print(f"  [MATCH] {h['name']} -> {match['shares']:,}")
                updated += 1
            elif total_shares > 0:
                est = e.calc_shares_from_ratio(h["name"], total_shares)
                if est > 0:
                    h["shares"] = est
                    print(f"  [CALC] {h['name']} -> {est:,} (estimated)")
                    updated += 1
        lk["shares"] = sum(h.get("shares", 0) for h in lk.get("holders", []))

    if updated:
        stock["notes"] = stock.get("notes", "") + f" +edinet:{date.today()}"
        total_updated += 1
        print(f"  -> {updated} holders updated")
    else:
        print("  -> No match found")

e.save_stocks(stocks)
print(f"\nDone: {total_updated}/2 updated -> {e.DATA_FILE}")
