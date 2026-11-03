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


BRAND_TAIL = re.compile(r"\s+برند\s+.*$")


def find(cfg: dict, q: str, limit: int = 30, wc=None) -> list[dict]:
    """(3 Nov) A name as the site shows it ('… برند لاکی گلد (Lucky Gold)'), the product's edit or page
    link, its ID or SKU. A product not found in the runs' files is asked from the site: the source
    page it was made from (meta _caravan_source_url) leads back to its run."""
    q = str(q or "").strip()
    if not q:
        return []
    m = re.search(r"[?&](?:post|p)=(\d{3,8})", q)
    if m:
        q = m.group(1)
    out = _find(cfg, q, limit)
    if out or not re.fullmatch(r"\d{3,8}", q):
        return out
    return _from_site(cfg, int(q), limit, wc)


def _from_site(cfg: dict, pid: int, limit: int, wc=None) -> list[dict]:
    try:
        from .wc import WC
        wc = wc or WC(timeout=30)
        if not wc.ok():
            return []
        p = wc.get_product(pid)
    except Exception:                                        # noqa: BLE001 - nothing to show
        return []
    meta = {m.get("key"): m.get("value") for m in p.get("meta_data") or []}
    src = str(meta.get("_caravan_source_url") or "")
    if src:
        hits = _find(cfg, "", limit, url=src)
        if hits:
            for h in hits:
                h["product_id"] = h["product_id"] or str(pid)
            return hits
    if not (src or meta.get("_caravan_supplier")):
        return []
    from urllib.parse import urlparse
    return [{"run": "", "name": p.get("name", ""), "product_id": str(pid), "sku": p.get("sku", ""),
             "supplier": meta.get("_caravan_supplier") or urlparse(src).netloc, "domain": urlparse(src).netloc,
             "url": src, "phones": "", "other_shops": "", "price_toman": meta.get("_caravan_price_toman", ""),
             "read_at": meta.get("_caravan_price_date", ""), "category": ""}]


def _find(cfg: dict, q: str, limit: int = 30, url: str = "") -> list[dict]:
    runs = Path(cfg["_data_dir"]) / "runs"
    words = _words(BRAND_TAIL.sub("", q))                    # the site's name adds 'برند …' to the crawled one
    by_id = q if re.fullmatch(r"\d{3,8}", q) else ""
    by_sku = q.upper() if re.fullmatch(r"(CRV|CAT)-[0-9A-Za-z\-]+", q, re.I) else ""
    out: list[dict] = []
    for d in sorted((x for x in runs.iterdir() if x.is_dir() and not x.name.startswith("fix-")),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        pub, pubname = {}, {}
        p7 = d / "07_published.csv"
        if p7.exists():
            try:
                t = read_csv(p7)
                pub = {u: (i, s) for u, i, s in zip(t.get("source_url", []), t.get("product_id", []), t.get("sku", []))}
                pubname = dict(zip(t.get("source_url", []), t.get("name", [])))
            except Exception:                                # noqa: BLE001 - a broken file is skipped
                pub, pubname = {}, {}
        p5 = d / "05_new_products.csv"
        if p5.exists():
            try:
                t = read_csv(p5)
            except Exception:                                # noqa: BLE001
                continue
            t = t.reindex(columns=NEW_COLS, fill_value="")
            for r in t.to_dict("records"):
                if url and r["url"] != url:
                    continue
                pid, sku = pub.get(r["url"], ("", ""))
                name = r["name_fa"] or r["name"]
                if by_id and str(pid).split(".")[0] != by_id:
                    continue
                if by_sku and str(sku).upper() != by_sku:
                    continue
                if not (by_id or by_sku or url) and not (_match(words, name) or _match(words, pubname.get(r["url"], ""))):
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
        if c6.exists() and not url:
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
