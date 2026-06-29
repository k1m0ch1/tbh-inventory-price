#!/usr/bin/env python3
"""
TBH Inventory Reader  -  for "TBH: Task Bar Hero" (Steam)

Reads the *live* inventory straight from the running game's memory and prints a
named, grouped report. The on-disk save (SaveFile_Live.es3) is AES-encrypted, so
instead of decrypting it we read the copy the game has already decrypted into RAM.

Usage:   python tbh_inventory.py [--prices] [--currency idr]
Prereq:  the game must be running; open the inventory tab once before running.
         No administrator rights are needed (run as the same Windows user).

Outputs: prints JSON and writes TBH_inventory.json next to this script.
"""
import ctypes, json, os, re, subprocess, sys, time, urllib.parse, urllib.request, urllib.error

PROCESS_VM_READ            = 0x0010 # idk man it might be wrong, but "apparently" I got this number
PROCESS_QUERY_INFORMATION  = 0x0400
MEM_COMMIT                 = 0x1000
MEM_PRIVATE                = 0x20000
PAGE_NOACCESS              = 0x01
PAGE_GUARD                 = 0x100

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress",        ctypes.c_void_p),
                ("AllocationBase",     ctypes.c_void_p),
                ("AllocationProtect",  ctypes.c_uint32),
                ("RegionSize",         ctypes.c_size_t),
                ("State",              ctypes.c_uint32),
                ("Protect",            ctypes.c_uint32),
                ("Type",               ctypes.c_uint32)]

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------- #
#  Find the game process
# --------------------------------------------------------------------------- #
def find_pid(name="TaskBarHero.exe"):
    out = subprocess.check_output(
        ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
        text=True, errors="ignore")
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if parts and parts[0].lower() == name.lower():
            return int(parts[1])
    return None

# --------------------------------------------------------------------------- #
#  Scan memory for the decrypted save JSON
# --------------------------------------------------------------------------- #
SAVE_MARK = b'{"commonSaveData":{"version' # the actual file its actually encrypted, so I just get it live from process memory and dump it
MARK_U16  = SAVE_MARK.decode().encode("utf-16-le")   # C# string (UTF-16) form
MARK_U8   = SAVE_MARK                                 # escaped ASCII form

def _run_u16(data, off):
    out, i, n = [], off, len(data)
    while i + 1 < n:
        lo, hi = data[i], data[i + 1]
        if hi == 0 and 0x20 <= lo <= 0x7E:
            out.append(lo); i += 2
        else:
            break
    return bytes(out).decode("ascii", "ignore")

def _run_u8(data, off):
    out, i, n = [], off, len(data)
    while i < n and 0x20 <= data[i] <= 0x7E:
        out.append(data[i]); i += 1
    return bytes(out).decode("ascii", "ignore")

def scan_save(pid):
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        raise OSError(f"OpenProcess failed (err={ctypes.get_last_error()}). "
                      f"Run as the same Windows user that launched the game.")
    CHUNK = 4 * 1024 * 1024
    buf = (ctypes.c_char * CHUNK)()
    best = ""

    def walk(private_only):
        nonlocal best
        mbi, addr = MBI(), 0
        while True:
            if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base, size = mbi.BaseAddress or 0, mbi.RegionSize or 0
            ok = (base >= addr and mbi.State == MEM_COMMIT and
                  (mbi.Protect & 0xFF) != PAGE_NOACCESS and
                  not (mbi.Protect & PAGE_GUARD))
            if private_only:
                ok = ok and (mbi.Type == MEM_PRIVATE)
            if ok and size:
                off = 0
                while off < size:
                    n = min(CHUNK, size - off)
                    rd = ctypes.c_size_t(0)
                    if k32.ReadProcessMemory(h, ctypes.c_void_p(base + off),
                                             buf, n, ctypes.byref(rd)) and rd.value:
                        data = bytes(buf[:rd.value])
                        for mark, runner in ((MARK_U16, _run_u16), (MARK_U8, _run_u8)):
                            s = 0
                            while True:
                                p = data.find(mark, s)
                                if p < 0:
                                    break
                                cand = runner(data, p)
                                if len(cand) > len(best):
                                    try:
                                        json.loads(cand)
                                        best = cand
                                    except Exception:
                                        pass
                                s = p + len(mark)
                    off += max(1, CHUNK - 1024 * 1024)   # 1 MB overlap
            nxt = base + size
            if nxt <= addr:
                break
            addr = nxt

    try:
        walk(True)            # managed heap first (fast, skips mapped files)
        if not best:
            walk(False)
    finally:
        k32.CloseHandle(h)
    if not best:
        raise RuntimeError("No save found in memory. Open the inventory in-game, then re-run.")
    return best

# --------------------------------------------------------------------------- #
#  Locate the game install (for name-table regeneration via UnityPy)
# --------------------------------------------------------------------------- #
def find_game_dir():
    # 1. Steam install from registry, then all library folders
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
        vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
        libs = [steam]
        if os.path.isfile(vdf):
            for m in re.finditer(r'"path"\s+"([^"]+)"', open(vdf, encoding="utf-8", errors="ignore").read()):
                libs.append(m.group(1).replace("\\\\", "\\"))
        for lb in libs:
            d = os.path.join(lb, "steamapps", "common", "TaskbarHero")
            if os.path.isdir(d):
                return d
    except Exception:
        pass
    # 2. common defaults
    for drive in ("C", "D", "E", "F", "G"):
        for pf in (f"{drive}:\\Program Files (x86)\\Steam", f"{drive}:\\Steam"):
            d = os.path.join(pf, "steamapps", "common", "TaskbarHero")
            if os.path.isdir(d):
                return d
    return None

# --------------------------------------------------------------------------- #
#  Build ItemKey -> name from the English localization bundle
# --------------------------------------------------------------------------- #
def build_names():
    try:
        import UnityPy
    except ImportError:
        return {}
    gdir = find_game_dir()
    if not gdir:
        return {}
    aa = os.path.join(gdir, "TaskbarHero_Data", "StreamingAssets", "aa", "StandaloneWindows64")
    shared = os.path.join(aa, "localization-assets-shared_assets_all.bundle")
    en = os.path.join(aa, "localization-string-tables-english(unitedstates)(en-us)_assets_all.bundle")
    if not (os.path.exists(shared) and os.path.exists(en)):
        return {}
    key_by_id, name_by_id = {}, {}
    for path, fld in ((shared, "m_Entries"), (en, "m_TableData")):
        for obj in UnityPy.load(path).objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                tt = obj.read_typetree()
            except Exception:
                continue
            if not isinstance(tt, dict):
                continue
            for e in tt.get(fld, []):
                mid = e.get("m_Id")
                if path == shared:
                    k = e.get("m_Key")
                    if isinstance(k, str) and k.startswith("ItemName_") and mid is not None:
                        key_by_id[mid] = k
                else:
                    v = e.get("m_Localized")
                    if mid is not None and isinstance(v, str):
                        name_by_id[mid] = v
    out = {}
    for mid in set(key_by_id) & set(name_by_id):
        m = re.match(r"ItemName_(\d+)$", key_by_id[mid])
        if m:
            out[int(m.group(1))] = name_by_id[mid]
    return out

def load_names():
    p = os.path.join(HERE, "tbh_item_names.json")
    if os.path.exists(p):
        return {int(k): v for k, v in json.load(open(p, encoding="utf-8")).items()}
    names = build_names()
    if names:
        json.dump(names, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return names

def load_meta():
    p = os.path.join(HERE, "tbh_item_meta.json")
    if os.path.exists(p):
        return {int(k): v for k, v in json.load(open(p, encoding="utf-8")).items()}
    return {}

def name_of(key, names):
    if key in names:
        return names[key]
    if key % 1000 == 900 and (key // 1000) in names:
        return names[key // 1000] + " (variant)"
    return f"#{key} (equipment-tier)"

# --------------------------------------------------------------------------- #
#  Steam Market price data (optional, --prices flag)
# --------------------------------------------------------------------------- #
PRICE_TTL   = 1800  # 30 minutes — freshness window for snapshots / localized prices
PRICE_DELAY = 1.0   # seconds between priceoverview calls

# Steam currency IDs: https://partner.steamgames.com/doc/store/pricing/currencies
CURRENCIES = {
    "usd": 1,  "gbp": 2,  "eur": 3,  "rub": 5,  "brl": 7,
    "jpy": 8,  "idr": 10, "myr": 11, "php": 12, "sgd": 13,
    "thb": 14, "aud": 18, "cad": 19, "cny": 23, "inr": 24,
    "krw": 34, "try": 35, "uah": 37, "mxn": 38,
}

def load_listings():
    """
    Return the USD market snapshot {hash_name: {price, listings}}, building it via
    market_scan if missing/stale. This is the complete set of items that currently
    have an active seller — i.e. which tradeable items actually have a price.
    """
    path = os.path.join(HERE, "tbh_market.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < PRICE_TTL:
        return json.load(open(path, encoding="utf-8"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("_mscan", os.path.join(HERE, "market_scan.py"))
    mscan = importlib.util.module_from_spec(spec); spec.loader.exec_module(mscan)
    print("Market snapshot missing/stale - scanning Steam Market (USD) ...", flush=True)
    snap, _ = mscan.fetch_all()
    json.dump(snap, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return snap

def fetch_market_details(hash_names, currency_id, cache_path):
    """
    Per-item priceoverview -> {hash_name: {price, demand}}.
    `price`  = lowest ask in the requested currency.
    `demand` = units sold in the last 24h (Steam's `volume`).
    Disk-cached per currency with PRICE_TTL. Only the items you actually own are
    queried, so the set stays small.
    """
    cache = {}
    if os.path.exists(cache_path):
        try: cache = json.load(open(cache_path, encoding="utf-8"))
        except Exception: pass
    now, results, to_fetch = time.time(), {}, []
    for hn in hash_names:
        e = cache.get(hn, {})
        if now - e.get("ts", 0) < PRICE_TTL:
            results[hn] = {"price": e.get("price", ""), "demand": e.get("demand")}
        else:
            to_fetch.append(hn)
    if to_fetch:
        print(f"Fetching price + 24h demand for {len(to_fetch)} items ...", flush=True)
        for i, hn in enumerate(to_fetch):
            url = ("https://steamcommunity.com/market/priceoverview/"
                   f"?appid=3678970&currency={currency_id}"
                   f"&market_hash_name={urllib.parse.quote(hn)}")
            price, demand, ok = "", None, False
            for attempt in range(4):                       # retry w/ backoff on Steam 429
                try:
                    with urllib.request.urlopen(url, timeout=10) as r:
                        d = json.loads(r.read())
                    if d.get("success"):
                        ok = True
                        price = d.get("lowest_price", "")
                        vol = (d.get("volume") or "").replace(",", "")
                        demand = int(vol) if vol.isdigit() else 0
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429 and attempt < 3:
                        time.sleep(5 * (attempt + 1))      # rate-limited: back off and retry
                        continue
                    break
                except Exception:
                    break
            results[hn] = {"price": price, "demand": demand}
            if ok:
                cache[hn] = {"price": price, "demand": demand, "ts": now}   # don't cache failures -> retry next run
            if i < len(to_fetch) - 1:
                time.sleep(PRICE_DELAY)
        try: json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception: pass
    return results

# --------------------------------------------------------------------------- #
#  Report
# --------------------------------------------------------------------------- #
def main():
    import argparse
    ap = argparse.ArgumentParser(description="TBH live inventory reader")
    ap.add_argument("-p", "--prices", action="store_true",
                    help="Fetch live prices from Steam Market (cached 30 min)")
    ap.add_argument("-c", "--currency", default="usd", metavar="CODE",
                    help=f"Steam Market currency (default: usd). Options: {', '.join(CURRENCIES)}")
    args = ap.parse_args()

    currency_code = args.currency.lower()
    if currency_code not in CURRENCIES:
        print(f"Unknown currency '{args.currency}'. Valid options: {', '.join(CURRENCIES)}")
        return 1
    currency_id = CURRENCIES[currency_code]

    pid = find_pid()
    if not pid:
        print("TaskBarHero.exe is not running. Launch the game, open the inventory, then re-run.")
        return 1
    print(f"Reading live memory of TaskBarHero.exe (pid {pid}) ...", flush=True)
    sv    = json.loads(scan_save(pid))
    names = load_names()
    meta  = load_meta()

    items = sv["itemSaveDatas"]
    uid_to_key = {int(i["UniqueId"]): int(i["ItemKey"]) for i in items}

    def filled(slots):
        return [int(s["ItemUniqueId"]) for s in slots if int(s["ItemUniqueId"])]

    def group(uids):
        g = {}
        for u in uids:
            k = uid_to_key.get(u)
            if k is not None:
                g[k] = g.get(k, 0) + 1
        return g

    inv   = group(filled(sv["inventorySaveDatas"]))
    stash = group(filled(sv["stashSaveDatas"]))
    owned = {}
    for i in items:
        owned[int(i["ItemKey"])] = owned.get(int(i["ItemKey"]), 0) + 1

    # Optionally resolve price + 24h demand for tradeable items:
    #   - USD market snapshot (sell_price cents) = clean numeric sort key + completeness
    #   - priceoverview per owned item = localized price AND 24h volume (demand)
    listings = load_listings() if args.prices else {}      # {hash: {price, price_cents, listings}}
    details  = {}                                           # {hash: {price, demand}}
    if args.prices:
        listed_hashes = set()
        for k in set(inv) | set(stash) | set(owned):
            m = meta.get(k, {})
            hn = m.get("market_hash_name", "")
            if m.get("tradeable") and hn in listings:
                listed_hashes.add(hn)
        if listed_hashes:
            details = fetch_market_details(
                sorted(listed_hashes), currency_id,
                os.path.join(HERE, f"tbh_prices_{currency_code}.json"))

    def item_entry(k, qty):
        m = meta.get(k, {})
        hn   = m.get("market_hash_name", "")
        snap = listings.get(hn, {})
        price, demand = None, None
        if args.prices and m.get("tradeable") and hn:
            d = details.get(hn, {})
            price  = d.get("price") or None
            demand = d.get("demand")
            # Steam has no regional data for some items in non-USD currencies;
            # fall back to the scan's USD price (flagged) so a listed item still
            # shows a number.
            if not price and hn in listings:
                usd = snap.get("price", "")
                if usd:
                    price = f"{usd} (usd)"
        return {
            "key":        k,
            "name":       name_of(k, names),
            "quantity":   qty,
            "level":      m.get("level") or None,
            "item_type":  m.get("item_type") or None,
            "gear_type":  m.get("gear_type") or None,
            "tradeable":  m.get("tradeable", False),
            "price":      price,
            "demand":     demand,                 # units sold in the last 24h
            "_sort_cents": snap.get("price_cents") if (args.prices and hn in listings) else None,
        }

    def build_list(g):
        entries = [item_entry(k, qty) for k, qty in g.items()]
        # tradeable-with-price first (highest price desc), then the rest by quantity
        entries.sort(key=lambda e: (e["_sort_cents"] is None,
                                    -(e["_sort_cents"] or 0),
                                    -e["quantity"], e["name"]))
        for e in entries:
            e.pop("_sort_cents", None)
        return entries

    currency_out = {name_of(int(c["Key"]), names): int(c["Quantity"])
                    for c in sv["currenySaveDatas"]}

    inv_list, stash_list, owned_list = build_list(inv), build_list(stash), build_list(owned)

    output = {
        "save_version":       sv["commonSaveData"].get("version", "?"),
        "total_instances":    len(items),
        "inventory_used":     sum(inv.values()),
        "inventory_capacity": len(sv["inventorySaveDatas"]),
        "stash_used":         sum(stash.values()),
        "stash_capacity":     len(sv["stashSaveDatas"]),
        "price_currency":     currency_code.upper() if args.prices else None,
        "currency":           currency_out,
        "inventory":          inv_list,
        "stash":              stash_list,
        "owned":              owned_list,
    }

    out_path = os.path.join(HERE, "TBH_inventory.json")
    json.dump(output, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ---- console: summary + top-30 tradeable-by-price table ----
    cur_str = ", ".join(f"{n}: {q:,}" for n, q in currency_out.items())
    print(f"\nTBH LIVE INVENTORY  (save {output['save_version']})  "
          f"| {len(items)} instances | inv {output['inventory_used']}/{output['inventory_capacity']} "
          f"| stash {output['stash_used']}/{output['stash_capacity']}")
    print(f"CURRENCY: {cur_str}\n")

    if args.prices:
        _print_top_table(owned_list, currency_code)
        print(f"\n[full sorted JSON written to {out_path}]", flush=True)
    else:
        print(f"[full JSON written to {out_path}]  (use --prices for market value + demand)",
              flush=True)
    return 0


def _print_top_table(items, currency_code, top_n=30):
    """Print the top-N tradeable items ranked by highest market price."""
    ranked = [i for i in items if i.get("tradeable") and i.get("price")]
    if not ranked:
        print("No priced tradeable items. Run with --prices and a working market scan.")
        return
    n = min(top_n, len(ranked))
    W_NAME, W_PRICE = 38, 14
    print(f"TOP {n} TRADEABLE ITEMS BY PRICE ({currency_code.upper()})")
    print(f"  {'#':>2}  {'Item':<{W_NAME}}  {'Qty':>4}  {'Price':<{W_PRICE}}  {'Demand':>9}")
    print(f"  {'--':>2}  {'-'*W_NAME}  {'----':>4}  {'-'*W_PRICE}  {'-'*9}")
    for rank, it in enumerate(ranked[:n], 1):
        name = it["name"] if len(it["name"]) <= W_NAME else it["name"][:W_NAME - 1] + "…"
        demand = "n/a" if it.get("demand") is None else f"{it['demand']}/24h"
        print(f"  {rank:>2}  {name:<{W_NAME}}  {it['quantity']:>4}  "
              f"{(it['price'] or ''):<{W_PRICE}}  {demand:>9}")
    print(f"\n  ({len(ranked)} priced tradeable item{'s' if len(ranked)!=1 else ''} total)")

if __name__ == "__main__":
    sys.exit(main())
