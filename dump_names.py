#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["UnityPy>=1.25.0"]
# ///
"""
dump_names.py  -  regenerate tbh_item_names.json and tbh_item_meta.json from the installed game

Run after a game update when item keys / names may have changed:

    uv run dump_names.py

Writes tbh_item_names.json and tbh_item_meta.json next to this script.
Requires the game to be installed (not necessarily running).
"""
import csv, io, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def find_game_dir():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
        vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
        libs = [steam]
        if os.path.isfile(vdf):
            for m in re.finditer(r'"path"\s+"([^"]+)"',
                                 open(vdf, encoding="utf-8", errors="ignore").read()):
                libs.append(m.group(1).replace("\\\\", "\\"))
        for lb in libs:
            d = os.path.join(lb, "steamapps", "common", "TaskbarHero")
            if os.path.isdir(d):
                return d
    except Exception:
        pass
    for drive in ("C", "D", "E", "F", "G"):
        for pf in (f"{drive}:\\Program Files (x86)\\Steam", f"{drive}:\\Steam"):
            d = os.path.join(pf, "steamapps", "common", "TaskbarHero")
            if os.path.isdir(d):
                return d
    return None


def load_localization(gdir):
    """Returns name_by_namekey and grade_display_by_enum (both keyed by string)."""
    import UnityPy
    aa = os.path.join(gdir, "TaskbarHero_Data", "StreamingAssets", "aa", "StandaloneWindows64")
    shared = os.path.join(aa, "localization-assets-shared_assets_all.bundle")
    en_path = os.path.join(aa, "localization-string-tables-english(unitedstates)(en-us)_assets_all.bundle")

    key_by_id, name_by_id = {}, {}
    for obj in UnityPy.load(shared).objects:
        if obj.type.name != "MonoBehaviour": continue
        try: tt = obj.read_typetree()
        except: continue
        if isinstance(tt, dict):
            for e in tt.get("m_Entries", []):
                mid = e.get("m_Id"); k = e.get("m_Key", "")
                if mid is not None: key_by_id[mid] = k

    for obj in UnityPy.load(en_path).objects:
        if obj.type.name != "MonoBehaviour": continue
        try: tt = obj.read_typetree()
        except: continue
        if isinstance(tt, dict):
            for e in tt.get("m_TableData", []):
                mid = e.get("m_Id"); v = e.get("m_Localized")
                if mid is not None and isinstance(v, str): name_by_id[mid] = v

    name_by_key = {}
    for mid, k in key_by_id.items():
        v = name_by_id.get(mid)
        if v: name_by_key[k] = v

    grade_display = {k.replace("Grade_", ""): v
                     for k, v in name_by_key.items() if k.startswith("Grade_")}
    return name_by_key, grade_display


def load_item_info(gdir):
    """Returns list of row dicts from ItemInfoData CSV in sharedassets0.assets."""
    import UnityPy
    data_dir = os.path.join(gdir, "TaskbarHero_Data")
    env = UnityPy.load(os.path.join(data_dir, "sharedassets0.assets"))
    for obj in env.objects:
        if obj.type.name != "TextAsset": continue
        ta = obj.read()
        if getattr(ta, "m_Name", "") != "ItemInfoData": continue
        text = ta.m_Script
        if isinstance(text, bytes): text = text.decode("utf-8-sig", errors="ignore")
        rows = list(csv.DictReader(io.StringIO(text)))
        # Strip BOM from first column name if present
        if rows:
            bom_key = list(rows[0].keys())[0]
            if bom_key != "ItemKey":
                for r in rows: r["ItemKey"] = r.pop(bom_key, "")
        return rows
    return []


def _gear_variant(item_key_int):
    """Derive market variant letter from last digit of ItemKey: 1=A, 2=B, …"""
    last = item_key_int % 10
    return chr(ord("A") + max(0, last - 1)) if last > 0 else "A"


def build_tables(gdir):
    """
    Returns:
      names: {ItemKey_int: display_name_str}
      meta:  {ItemKey_int: {tradeable, level, item_type, gear_type, market_hash_name}}
    """
    name_by_key, grade_display = load_localization(gdir)
    rows = load_item_info(gdir)

    # Seed names from localization: ItemName_* (materials/gear archetypes)
    # and CurrencyName_* (e.g. Gold key 100001) so everything gets covered.
    names, meta = {}, {}
    for key_str, base_name in name_by_key.items():
        m = re.match(r"(?:ItemName|CurrencyName)_(\d+)$", key_str)
        if m:
            names[int(m.group(1))] = base_name

    for r in rows:
        try:
            key_int = int(r.get("ItemKey", ""))
        except ValueError:
            continue
        name_key = r.get("NameKey", "")
        base_name = name_by_key.get(name_key, "")
        if not base_name:
            continue

        grade_enum = r.get("GRADE", "")
        grade_name = grade_display.get(grade_enum, grade_enum.capitalize())
        item_type = r.get("ITEMTYPE", "")
        gear_type = r.get("GEARTYPE", "")
        try:
            level = int(r.get("Level", "") or "0")
        except ValueError:
            level = 0
        tradeable = r.get("IsCanExchangeMarketable", "").strip() == "True"

        if item_type == "GEAR":
            variant = _gear_variant(key_int)
            display_name = f"{base_name} ({grade_name}) {variant}"
            market_name = display_name
        else:
            display_name = base_name
            market_name = base_name

        names[key_int] = display_name
        meta[key_int] = {
            "tradeable": tradeable,
            "level": level,
            "item_type": item_type,
            "gear_type": gear_type,
            "market_hash_name": market_name if tradeable else "",
        }

    return names, meta


def main():
    gdir = find_game_dir()
    if not gdir:
        sys.exit("TaskbarHero install not found. Is the game installed via Steam?")

    print(f"Game dir: {gdir}")
    print("Reading game data ...", flush=True)
    names, meta = build_tables(gdir)
    if not names:
        sys.exit("No items extracted — data format may have changed.")

    sorted_names = {str(k): v for k, v in sorted(names.items())}
    sorted_meta  = {str(k): v for k, v in sorted(meta.items())}

    names_out = os.path.join(HERE, "tbh_item_names.json")
    meta_out  = os.path.join(HERE, "tbh_item_meta.json")

    json.dump(sorted_names, open(names_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(sorted_meta,  open(meta_out,  "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    tradeable = sum(1 for v in meta.values() if v["tradeable"])
    print(f"Written {len(names)} items ({tradeable} tradeable)")
    print(f"  names -> {names_out}")
    print(f"  meta  -> {meta_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
