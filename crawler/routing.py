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
        out = [(norm_text(r["brand"]), r["brand"], r.get("origin", "unknown"))
               for r in csv.DictReader(f) if r.get("brand") and r.get("is_brand", "yes") != "no"]
    return sorted(out, key=lambda x: -len(x[0]))


def find_brand(text: str) -> tuple[str, str] | None:
    t = f" {norm_text(text)} "
    for n, display, origin in brands():
        if f" {n} " in t:
            return display, origin
    return None


def rule_route(breadcrumb: str) -> dict:
    parts = [p.strip() for p in breadcrumb.split(">")]
    # a brand in the leaf beats one higher up; the top-level title is skipped
    # ("مصالح و شیمی ساختمان" is a category, "شیمی ساختمان" a brand)
    leaf_first = " ".join(reversed(parts[1:]))
    b = find_brand(leaf_first)
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
