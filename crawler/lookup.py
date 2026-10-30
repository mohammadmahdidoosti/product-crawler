"""Where did a site product come from? Type its name (or its site ID / SKU) and get the shop it was
crawled from, the page, the seller's phones, the price then and when it was read.

A customer orders a product and the owner must buy it from the supplier (29 Oct: «رول یدک غلطک
۲۵ سانت دکور … کد 1081» came from siminabzar.com). Every run keeps this in 05_new_products.csv
(and its site ID in 07_published.csv); catalogs keep their supplier in their settings.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from .io import read_csv
from .normalize import norm_text

NEW_COLS = ["url", "name", "name_fa", "source_domain", "seller_name", "seller_phones", "sellers", "urls",
            "price_toman", "crawled_at", "breadcrumb"]


def _words(q: str) -> list[str]:
    return [w for w in norm_text(q).split() if w]


def _match(words: list[str], text: str) -> bool:
    t = norm_text(text)
    return all(w in t for w in words)


def find(cfg: dict, q: str, limit: int = 30) -> list[dict]:
    q = str(q or "").strip()
    if not q:
        return []
    runs = Path(cfg["_data_dir"]) / "runs"
    words = _words(q)
    by_id = q if re.fullmatch(r"\d{3,8}", q) else ""
    by_sku = q.upper() if re.fullmatch(r"(CRV|CAT)-[0-9A-Za-z\-]+", q, re.I) else ""
    out: list[dict] = []
    for d in sorted((x for x in runs.iterdir() if x.is_dir() and not x.name.startswith("fix-")),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        pub = {}
        p7 = d / "07_published.csv"
        if p7.exists():
            try:
                t = read_csv(p7)
                pub = {u: (i, s) for u, i, s in zip(t.get("source_url", []), t.get("product_id", []), t.get("sku", []))}
            except Exception:                                # noqa: BLE001 - a broken file is skipped
                pub = {}
        p5 = d / "05_new_products.csv"
        if p5.exists():
            try:
                t = read_csv(p5)
            except Exception:                                # noqa: BLE001
                continue
            t = t.reindex(columns=NEW_COLS, fill_value="")
            for r in t.to_dict("records"):
                pid, sku = pub.get(r["url"], ("", ""))
                name = r["name_fa"] or r["name"]
                if by_id and str(pid).split(".")[0] != by_id:
                    continue
                if by_sku and str(sku).upper() != by_sku:
                    continue
                if not (by_id or by_sku) and not _match(words, name):
                    continue
                out.append({"run": d.name, "name": name, "product_id": str(pid).split(".")[0], "sku": sku,
                            "supplier": r["seller_name"] or r["source_domain"], "domain": r["source_domain"],
                            "url": r["url"], "phones": r["seller_phones"],
                            "other_shops": " | ".join(x for x in str(r["sellers"]).split(" | ")
                                                      if x and x != r["seller_name"])[:200],
                            "price_toman": r["price_toman"], "read_at": r["crawled_at"], "category": r["breadcrumb"]})
                if len(out) >= limit:
                    return out
        c6 = d / "C6_published.csv"                          # a supplier catalog
        if c6.exists():
            try:
                t = read_csv(c6)
                st = json.loads((d / "C0_settings.json").read_text(encoding="utf-8")) \
                    if (d / "C0_settings.json").exists() else {}
            except Exception:                                # noqa: BLE001
                continue
            for r in t.to_dict("records"):
                pid = str(r.get("product_id") or "").split(".")[0]
                if (by_id and pid != by_id) or (by_sku and str(r.get("sku", "")).upper() != by_sku) or \
                        (not (by_id or by_sku) and not _match(words, r.get("name", ""))):
                    continue
                out.append({"run": d.name, "name": r.get("name", ""), "product_id": pid, "sku": r.get("sku", ""),
                            "supplier": st.get("supplier") or st.get("brand") or "کاتالوگ",
                            "domain": st.get("maker_site", ""), "url": "", "phones": "", "other_shops": "",
                            "price_toman": "", "read_at": r.get("published_at", ""), "category": r.get("categories", "")})
    return out[:limit]
