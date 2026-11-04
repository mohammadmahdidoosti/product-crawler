"""Brand-wide fixes the owner asks for by phone (4 Nov), with a dry run first and a backup of every product.

  out_of_stock   every product of some brands (optionally only names matching a filter, e.g. only the
                 tillers of a brand) -> out of stock, marked _caravan_oos_list 'owner:<brand> <date>'.
                 They come back in stock when a later price list has them.
  shop_not_brand a shop's name that stood in for a brand ('پی فارم', 'سبز توس'): its products get the
                 brand their name holds (or none), the shop's name leaves the product name, and the
                 crawler's brand table marks it as a shop so it never comes back.

Backups: DATA_DIR/pricelists/brandtools/backup.jsonl (one line per product, before the change).
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from pathlib import Path

log = logging.getLogger(__name__)

GENERIC = {"برند", "brand", "سمپاش", "پاور", "power", "اره", "ابزار", "صنعت", "اصل", "گاردن", "ورکس", "works",
           "مارک", "mark", "استار", "star", "مدل", "model"}


def _n(s) -> str:
    from .normalize import norm_text
    return re.sub(r"\s+", " ", norm_text(re.sub(r"<[^>]+>", " ", str(s or ""))).casefold()).strip()


def _word(needle: str, text: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", text))


def term_names(t: dict) -> list[str]:
    """'ماکیتا (Makita)' -> ['ماکیتا', 'makita']."""
    nm = str(t.get("name") or "")
    m = re.match(r"\s*(.*?)\s*\((.*?)\)\s*$", nm)
    return [x for x in (_n(p) for p in ([m.group(1), m.group(2)] if m else [nm])) if x]


def find_terms(terms: list[dict], wanted: list[str]) -> list[dict]:
    """The site's brand terms for the names asked (Persian or Latin, either part of 'fa (en)')."""
    w = {_n(x) for x in wanted if _n(x)}
    return [t for t in terms if w & set(term_names(t)) or any(x in _n(t.get("slug")) for x in w if len(x) >= 4)]


def brand_products(rows: list[dict], term_ids: set, names: list[str], name_filter: str = "") -> list[dict]:
    """Products with one of the terms, or with no brand and the brand's name as a word in their name."""
    flt = re.compile(name_filter, re.I) if name_filter else None
    ns = [_n(x) for x in names if len(_n(x)) >= 3]
    out = []
    for r in rows:
        if flt and not flt.search(str(r.get("name") or "")):
            continue
        bids = {b.get("id") for b in r.get("brands") or []}
        if term_ids & bids or (not bids and any(_word(x, _n(r.get("name"))) for x in ns)):
            out.append(r)
    return out


def _backup(cfg: dict, wc, ids: list[int], why: str) -> None:
    d = Path(cfg["_data_dir"]) / "pricelists" / "brandtools"
    d.mkdir(parents=True, exist_ok=True)
    cur = wc._call(lambda: wc._wc("GET", "products", params={"include": ",".join(map(str, ids)), "per_page": 100,
                                                             "status": "any", "context": "edit"}), "backup")
    with (d / "backup.jsonl").open("a", encoding="utf-8") as fh:
        for p in cur or []:
            fh.write(json.dumps({"id": p["id"], "name": p.get("name"), "stock_status": p.get("stock_status"),
                                 "brands": p.get("brands"), "why": why, "at": time.strftime("%Y-%m-%d %H:%M")},
                                ensure_ascii=False) + "\n")


def _send(cfg: dict, wc, items: list[dict], why: str) -> int:
    ok = 0
    for i in range(0, len(items), 50):
        chunk = items[i:i + 50]
        _backup(cfg, wc, [x["id"] for x in chunk], why)
        j = wc.batch_update(chunk)
        ok += sum(1 for p in j.get("update") or [] if not p.get("error"))
    return ok


def out_of_stock(cfg: dict, wc, brands: list[str], name_filter: str = "", go: bool = False) -> dict:
    """Every product of these brands -> out of stock (variable products are left, as in the price lists)."""
    from .pricelist import site_cache
    terms = wc.list_brands() or []
    rows = site_cache(cfg, wc)
    day = time.strftime("%Y-%m-%d")
    res = {"go": go, "brands": []}
    for b in brands:
        ts = find_terms(terms, [b])
        names = [b] + [x for t in ts for x in term_names(t)]
        ps = brand_products(rows, {t["id"] for t in ts}, names, name_filter)
        todo = [p for p in ps if p.get("stock_status") != "outofstock" and p.get("type") != "variable"]
        one = {"brand": b, "terms": [t.get("name") for t in ts], "products": len(ps), "to_change": len(todo),
               "variable_left": sum(1 for p in ps if p.get("type") == "variable"), "done": 0}
        if go and todo:
            one["done"] = _send(cfg, wc, [{"id": int(p["id"]), "stock_status": "outofstock",
                                           "meta_data": [{"key": "_caravan_oos_list", "value": f"owner:{b} {day}"}]}
                                          for p in todo], f"oos {b}")
        res["brands"].append(one)
    log.info("BRANDTOOLS out of stock: %s", res)
    return res


SHOP_WORDS = r"(?:برند|فروشگاه)"


def shop_not_brand(cfg: dict, wc, shops: list[str], go: bool = False) -> dict:
    """A shop's name used as a brand: its products get the real brand from their name (or none)."""
    from . import brands_fa as BF
    from .pricelist import site_cache
    terms = wc.list_brands() or []
    bad = find_terms(terms, shops)
    badids = {t["id"] for t in bad}
    needles = sorted(((x, t) for t in terms if t["id"] not in badids for x in term_names(t)
                      if len(x) >= 4 and x not in GENERIC), key=lambda z: -len(z[0]))
    alt = "|".join(re.escape(s).replace(r"\ ", r"\s*") for s in shops)
    strip = re.compile(rf"\s*[-–(]?\s*{SHOP_WORDS}?\s*(?:{alt})\s*\)?\s*", re.I)
    todo, how = [], Counter()
    for r in site_cache(cfg, wc):
        bids = [b.get("id") for b in r.get("brands") or []]
        name = str(r.get("name") or "")
        if not (badids & set(bids)) and not strip.search(name):
            continue
        clean = re.sub(r"\s+", " ", strip.sub(" ", name)).strip()
        keep = [b for b in bids if b not in badids]
        hit = None if keep else next((t for x, t in needles if _word(x, _n(clean))), None)
        x = {"id": int(r["id"]), "brands": [{"id": b} for b in keep] or ([{"id": hit["id"]}] if hit else [])}
        if clean and clean != name.strip():
            x["name"] = clean
        how["kept its brand" if keep else (hit or {}).get("name") or "no brand"] += 1
        todo.append(x)
    res = {"go": go, "shop_terms": [t.get("name") for t in bad], "products": len(todo),
           "names_cleaned": sum(1 for x in todo if "name" in x), "to": dict(how.most_common(20)), "done": 0}
    if go:
        if todo:
            res["done"] = _send(cfg, wc, todo, f"shop {','.join(shops)}")
        df = BF.load(cfg)                                  # the crawler: a shop from now on
        for s in shops:
            df = df[df["source"].map(BF.key) != BF.key(s)]
        import pandas as pd
        df = pd.concat([df, pd.DataFrame([{"source": s, "kind": "shop", "display": "", "by": "manual",
                                           "updated": time.strftime("%Y-%m-%d")} for s in shops])], ignore_index=True)
        BF.save(cfg, df)
    log.info("BRANDTOOLS shop not brand: %s", res)
    return res
