"""Brand of every product, never empty.

1. the brand the page gives, if it is a brand (not a country, not the product name, not 'OEM')
2. a known brand (brands.csv / Latin names) inside the product name
3. a Latin word inside a Persian name that looks like a brand ('اکچویتور برقی GINICE مدل GQ' -> GINICE)
4. otherwise the supplier: the Chinese factory's company name, or the Iranian shop's name
brand_source records which one was used (internal).
"""
from __future__ import annotations

import re

import pandas as pd

from .normalize import norm_text

COUNTRIES = {norm_text(x) for x in (
    "ایران", "چین", "آلمان", "المان", "ایتالیا", "انگلیس", "انگلستان", "فرانسه", "ژاپن", "کره", "کره جنوبی", "ترکیه",
    "تایوان", "آمریکا", "امریکا", "سوئیس", "سوییس", "سوئد", "دانمارک", "اسپانیا", "هند", "روسیه", "هلند", "اتریش",
    "بلژیک", "لهستان", "چک", "مالزی", "تایلند", "ویتنام", "امارات", "اروپا", "کانادا", "برزیل", "مکزیک",
    "china", "germany", "italy", "england", "uk", "usa", "france", "japan", "korea", "turkey", "taiwan", "iran",
)}
NOT_BRANDS = {norm_text(x) for x in ("oem", "odm", "no brand", "nobrand", "none", "n/a", "متفرقه", "سایر", "بدون برند",
                                     "نامشخص", "customized", "custom", "other", "others", "generic", "متفرقه ایرانی")}
LATIN_STOP = {"ip", "ac", "dc", "led", "pvc", "upvc", "cpvc", "pe", "pp", "ppr", "abs", "lcd", "usb", "cnc", "plc",
              "hmi", "vfd", "rpm", "bar", "psi", "mm", "cm", "kw", "hp", "kg", "nm", "mpa", "dn", "pn", "iso", "ce",
              "din", "ansi", "api", "ss", "nbr", "epdm", "ptfe", "hvac", "rtu", "modbus", "wifi", "smart", "pro",
              "max", "mini", "series", "type", "model", "new", "set", "kit", "inch", "volt", "fast", "running"}
CO_SUFFIX = re.compile(r"[\s,.]*(co\.?,?\s*ltd\.?|company limited|co\.?|ltd\.?|limited|inc\.?|corp\.?|llc|"
                       r"group|industrial|industry|technology|trading|manufacturing|import\s*&\s*export)[\s,.]*$", re.I)


def clean_brand(brand: str, name: str = "") -> str:
    b = re.sub(r"\s+", " ", str(brand or "")).strip(" |،,-")
    if not b:
        return ""
    if " | " in b:                                     # 'LINAK | لیناک' -> keep both readable forms once
        b = b.split(" | ")[0].strip()
    nb = norm_text(b)
    if nb in COUNTRIES or nb in NOT_BRANDS or len(b) > 30 or len(b) < 2:
        return ""
    if name and (nb == norm_text(name) or len(nb.split()) >= 4):
        return ""                                      # the whole product name copied into 'brand'
    return b


def brand_from_name(name: str) -> str:
    from .routing import LATIN, find_brand
    b = find_brand(name or "")
    if b:
        return b[0]
    for lat in sorted(set(LATIN.values()), key=len, reverse=True):
        if len(lat) >= 3 and re.search(rf"\b{re.escape(lat)}\b", name or "", re.I):
            return lat.upper() if len(lat) <= 4 else lat.capitalize()
    if re.search(r"[؀-ۿ]", name or ""):     # a Latin word in a Persian name is usually the brand
        for w in re.findall(r"\b[A-Za-z][A-Za-z\-]{2,}\b", name):
            if w.lower() not in LATIN_STOP and not re.search(r"\d", w):
                return w
    return ""


def company_short(name: str) -> str:
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    for _ in range(3):
        n = CO_SUFFIX.sub("", n).strip()
    return n or str(name or "").strip()


def fill_brands(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    brands, how = [], []
    for r in df.fillna("").to_dict("records"):
        name = r.get("source_name") or r.get("name") or ""
        b = clean_brand(r.get("brand", ""), name)
        src = "page" if b else ""
        if not b:
            b = brand_from_name(name)
            src = "name" if b else ""
        if not b:
            seller = (r.get("sellers") or r.get("seller_name") or "").split(" | ")[0]
            b = company_short(seller) if r.get("source_type") == "foreign" else seller
            src = "supplier" if b else ""
        if not b:
            b, src = (r.get("source_domain") or ""), "domain"
        brands.append(b)
        how.append(src)
    df["brand"], df["brand_source"] = brands, how
    return df
