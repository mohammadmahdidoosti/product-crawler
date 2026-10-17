"""Automatic route decision per category (step 1 of 2: free rules).

Rules settle what needs no measurement:
  * category names an Iranian brand  -> Iranian sources (brand site first)
  * category names a foreign brand   -> Iranian sources (dealers); never
    Made-in-China, which would return copies, not the original
  * spare parts, seeds, fertiliser, building chemicals, appliance parts
                                     -> Iranian sources only
  * everything else (unbranded equipment) -> "probe": one Persian web
    search measures the Iranian market and decides (probe.py, next step)
Every decision carries a plain-Persian reason for review.
"""
from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path

from .config import ROOT
from .normalize import norm_text

BRANDS_FILE = ROOT / "brands.csv"

IRAN_ONLY_L1 = {norm_text(x) for x in (
    "بذر", "کود و نهاده‌های کشاورزی و دامی", "مصالح و شیمی ساختمان",
    "قطعات یدکی ماشین‌آلات راهسازی و موتور دیزل", "لوازم خانگی و آشپزخانه",
)}
PART_WORDS = [norm_text(x) for x in (
    "یدکی", "قطعات", "قطعه", "لباسشویی", "یخچال", "ظرفشویی", "جاروبرقی", "کولر آبی", "پکیج",
)]


@lru_cache(maxsize=1)
def brands(path: str = str(BRANDS_FILE)) -> list[tuple[str, str, str]]:
    """[(normalized, display, origin)], longest first so 'پارس شعاع توس' wins over 'پارس شعاع'."""
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig") as f:
        out = [(norm_text(r["brand"]), r.get("canonical") or r["brand"], r.get("origin", "unknown"))
               for r in csv.DictReader(f) if r.get("brand") and r.get("is_brand", "yes") != "no"]
    return sorted(out, key=lambda x: -len(x[0]))


LATIN = {  # Latin names of brands, to recognise them in product names (Persian or English)
    "اشنایدر": "schneider", "هیوندای": "hyundai", "پنتاکس": "pentax", "ماکیتا": "makita", "ابارا": "ebara",
    "زیمنس": "siemens", "بوش": "bosch", "هوندا": "honda", "کرون": "crown", "رکسروت": "rexroth", "ال جی": "lg",
    "هیوکی": "hioki", "فستو": "festo", "فیندر": "finder", "یورک": "york", "دلتا": "delta", "پیمتاش": "pimtas",
    "دانفوس": "danfoss", "دنفوس": "danfoss", "ویکا": "wika", "دیوالت": "dewalt", "ال اس": "ls", "فلوک": "fluke",
    "اتونیکس": "autonics", "دوو": "daewoo", "گراندفوس": "grundfos", "میتسوبیشی": "mitsubishi",
    "پارکساید": "parkside", "برتولینی": "bertolini", "سامسونگ": "samsung", "فونیکس": "phoenix",
    "هانیول": "honeywell", "شیائومی": "xiaomi", "گرووات": "growatt", "اینکو": "ingco", "لورچ": "lorch",
    "پارکر": "parker", "امرون": "omron", "هیلتی": "hilti", "روزمونت": "rosemount", "تله مکانیک": "telemecanique",
    "چینت": "chint", "روتورک": "rotork", "ایمرگاس": "immergas", "لگراند": "legrand", "زیگلر": "ziegler",
    "ریوبی": "ryobi", "بلمو": "belimo", "بلیمو": "belimo", "فلوسرو": "flowserve", "auma": "auma", "آوما": "auma",
    "زیلابگ": "ziehl-abegg", "مونتاناری": "montanari", "ویتور": "wittur", "الکو": "elco",
    "abb": "abb", "smc": "smc", "aeg": "aeg", "obo": "obo", "eti": "eti", "dca": "dca", "فلوسرو ": "flowserve",
}


def brand_in(name: str, brand: str) -> bool:
    """Is this brand (Persian or Latin name) in the product name?"""
    t = f" {norm_text(name)} "
    keys = {norm_text(brand)} | {norm_text(k) for k, v in LATIN.items() if v == LATIN.get(brand, "~")}
    lat = LATIN.get(brand) or (brand if brand.isascii() else "")
    return any(f" {k} " in t for k in keys if k) or bool(lat and re.search(rf"\b{re.escape(lat)}\b", name, re.I))


def foreign_brand_in(name: str) -> str | None:
    """A known foreign brand written in Latin inside an English product name."""
    for lat in sorted(set(LATIN.values()), key=len, reverse=True):
        if len(lat) >= 3 and re.search(rf"\b{re.escape(lat)}\b", name, re.I):
            return lat
    return None


def find_brand(text: str) -> tuple[str, str] | None:
    t = f" {norm_text(text)} "
    for n, display, origin in brands():
        if f" {n} " in t:
            return display, origin
    return None


LATIN_NOT_BRAND = {"ip", "ac", "dc", "led", "pvc", "upvc", "cpvc", "pe", "pp", "ppr", "abs", "lcd", "usb", "cnc", "plc",
                   "hmi", "vfd", "rpm", "bar", "psi", "mm", "cm", "kw", "hp", "kg", "nm", "mpa", "dn", "pn", "iso", "ce",
                   "din", "ansi", "api", "ss", "nbr", "epdm", "ptfe", "hvac", "rtu", "wifi", "smart", "pro", "max",
                   "mini", "series", "type", "model", "new", "set", "kit", "inch", "volt", "ups", "ats", "mcb", "mccb",
                   "rcd", "rcbo", "ct", "pt", "cat", "rj", "utp", "ftp", "sfp", "poe", "nvr", "dvr", "ip67", "ip68",
                   "gps", "gsm", "sms", "rf", "ir", "pir", "co2", "lpg", "cng", "hdpe", "ldpe", "pex", "pert", "ppr",
                   "frp", "grp", "mdf", "hdf", "osb", "pu", "eva", "tpu", "pom", "pa", "pc", "pmma", "ptc", "ntc",
                   "rtd", "pt100", "ac/dc", "ccs", "sr", "x", "v", "w", "a", "u", "l", "s", "m", "t", "z", "y"}


def leaf_brand(text: str) -> tuple[str, str] | None:
    """A brand in a category name: a known one (brands.csv), else a Latin word
    ('موتور آسانسور elco', 'اره شارژی pap') — in Persian category names those are brands."""
    b = find_brand(text)
    if b:
        return b
    for w in re.findall(r"(?<![\w-])[A-Za-z][A-Za-z\-&]+(?![\w-])", text or ""):
        if w.lower() not in LATIN_NOT_BRAND and len(w) >= 2:
            return w, "unknown"
    return None


def rule_route(breadcrumb: str) -> dict:
    parts = [p.strip() for p in breadcrumb.split(">")]
    # a brand in the leaf beats one higher up; the top-level title is skipped
    # ("مصالح و شیمی ساختمان" is a category, "شیمی ساختمان" a brand)
    leaf_first = " ".join(reversed(parts[1:]))
    b = find_brand(leaf_first) or leaf_brand(parts[-1])
    if b:
        name, origin = b
        label = {"iranian": "برند ایرانی", "foreign": "برند خارجی", "unknown": "برند"}[origin] if origin in (
            "iranian", "foreign", "unknown") else "برند"
        note = "؛ اول سایت رسمی برند" if origin == "iranian" else ("؛ نمایندگی‌ها، نه کالای چینی" if origin == "foreign" else "")
        return {"decision": "iran", "kind": "brand", "brand": name, "brand_origin": origin,
                "reason": f"{label}: {name}{note}"}
    if norm_text(parts[0]) in IRAN_ONLY_L1:
        return {"decision": "iran", "kind": "local_category", "brand": "", "brand_origin": "",
                "reason": f"دسته‌ی «{parts[0]}» فقط منبع ایرانی دارد"}
    # part words count only below the top level ("قطعات الکترونیک و ماژول" is
    # a category of components, not of spare parts)
    path = f" {norm_text(' '.join(parts[1:]))} "
    hit = next((w for w in PART_WORDS if f" {w} " in path), None)
    if hit:
        return {"decision": "iran", "kind": "part", "brand": "", "brand_origin": "",
                "reason": f"قطعه یا لوازم مصرفی («{hit}»)؛ منبع خارجی کمکی نمی‌کند"}
    return {"decision": "probe", "kind": "generic", "brand": "", "brand_origin": "",
            "reason": "بی‌برند و عمومی؛ با سنجش بازار ایران تصمیم گرفته می‌شود"}
