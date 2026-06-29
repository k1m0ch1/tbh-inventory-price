#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""
market_scan.py - snapshot every TBH Steam Market listing (USD, complete).

The per-item `priceoverview` endpoint returns NO price when an item has zero
active listings, so a legitimately tradeable item can look "priceless", and its
currency handling is per-item (slow). The market *search* feed lists every
currently-listed item with its `sell_price_text` (the "Starting at" / lowest ask)
in one paginated stream.

This script always scans in USD — search/render is unreliable for other
currencies (it leaks mixed-currency prices mid-scan). tbh_inventory localizes
the displayed items to the requested currency afterward via priceoverview.

Writes  tbh_market.json : { market_hash_name: {price, listings} }
Prints   a tradeable cross-reference vs tbh_item_meta.json.

Usage:  uv run market_scan.py
"""
import json, os, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APPID = 3678970
PAGE = 100
DELAY = 2.0   # be nice to steamcommunity


def fetch_all():
    """Return ({hash_name: {price, price_cents, listings}}, total_count), USD market."""
    out, start, total = {}, 0, None
    while True:
        url = (f"https://steamcommunity.com/market/search/render/"
               f"?query=&start={start}&count={PAGE}&appid={APPID}"
               f"&currency=1&norender=1&sort_column=price&sort_dir=asc")
        with urllib.request.urlopen(url, timeout=15) as r:
            d = json.loads(r.read())
        if not d.get("success"):
            break
        total = d.get("total_count", 0)
        page = d.get("results", [])
        for it in page:
            hn = it.get("hash_name") or it.get("name")
            out[hn] = {
                "price":       it.get("sell_price_text", ""),
                "price_cents": it.get("sell_price", 0),   # USD cents — clean numeric sort key
                "listings":    it.get("sell_listings", 0),
            }
        # increment by what was actually returned — Steam often caps a page below `count`
        got = start + len(page)
        print(f"  ...{got}/{total} ({len(page)}/page, {len(out)} unique)", flush=True)
        if not page or got >= (total or 0):
            break
        start += len(page) or PAGE
        time.sleep(DELAY)
    return out, total


def main():
    print("Scanning TBH market (USD) ...", flush=True)
    market, total = fetch_all()
    out_path = os.path.join(HERE, "tbh_market.json")
    json.dump(market, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Got {len(market)} listed items (total_count={total}) -> {out_path}")

    meta_path = os.path.join(HERE, "tbh_item_meta.json")
    if os.path.exists(meta_path):
        meta = {int(k): v for k, v in json.load(open(meta_path, encoding="utf-8")).items()}
        tradeable = [(k, v) for k, v in meta.items() if v.get("tradeable")]
        listed = [v for _, v in tradeable if v["market_hash_name"] in market]
        print(f"\nTradeable items : {len(tradeable)}")
        print(f"  with listing  : {len(listed)}")
        print(f"  unlisted      : {len(tradeable) - len(listed)}  (tradeable but nobody selling right now)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
