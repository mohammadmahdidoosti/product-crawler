"""Merge duplicate products that are already on the site (one model sold by several shops became
several products in earlier batches).

Per group:
  kept product   -> the merged title, the checked price, every category of the group
  the duplicates -> the same price, hidden from the shop, categories and search
                    (catalog_visibility 'hidden'), and a Rank Math canonical URL pointing at the kept
                    product, so their old links keep working and Google moves their weight over.
Nothing is deleted; setting the visibility back and clearing the canonical undoes it.

DATA_DIR/merged.csv remembers every merge, so later publishes and resyncs do not undo it:
a duplicate is never sent again, a kept product keeps the merged title, price and categories.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

from .io import read_csv, write_csv
from .wc import WC, WCError

log = logging.getLogger(__name__)
REG_COLS = ["sku", "product_id", "role", "keep_id", "keep_sku", "name", "price_usd", "categories", "canonical",
            "merged_at"]
CANONICAL_KEY = "rank_math_canonical_url"


def _reg_path(cfg: dict) -> Path:
    return cfg["_data_dir"] / "merged.csv"


def registry(cfg: dict) -> dict[str, dict]:
    p = _reg_path(cfg)
    if not p.exists():
        return {}
    return {r["sku"]: r for r in read_csv(p).to_dict("records") if r.get("sku")}


def apply(cfg: dict, groups: list[dict], dry_run: bool = False, wc: WC | None = None, progress=None,
          stop=None) -> dict:
    """groups: [{keep: {id, sku}, dups: [{id, sku}], name, price_usd, categories: [ids]}]"""
    wc = wc or WC()
    if not wc.ok():
        raise ValueError("WC_URL / WC_KEY / WC_SECRET روی سرور تنظیم نشده")
    rows, failed, done_groups = [], [], 0
    today = time.strftime("%Y-%m-%d %H:%M")
    for gi, g in enumerate(groups):
        if stop and stop():
            break
        if progress:
            progress(gi, len(groups))
        keep = g["keep"]
        try:
            kp = wc.get_product(int(keep["id"]))
        except WCError as e:
            failed.append({"keep": keep["id"], "error": str(e)[:200]})
            continue
        link = kp.get("permalink") or ""
        price = f"{float(g['price_usd']):.2f}"
        cats = sorted({int(c) for c in g.get("categories") or [] if str(c).strip().isdigit()}
                      | {c["id"] for c in kp.get("categories") or []})
        items = [{"id": int(keep["id"]), "name": g["name"], "regular_price": price,
                  "categories": [{"id": c} for c in cats]}]
        for dp in g.get("dups") or []:
            items.append({"id": int(dp["id"]), "regular_price": price, "catalog_visibility": "hidden",
                          "meta_data": [{"key": CANONICAL_KEY, "value": link}]})
        if dry_run:
            log.info("merge (plan) %s <- %s", keep["id"], [d["id"] for d in g.get("dups") or []])
        else:
            try:
                j = wc.batch_update(items)
            except WCError as e:
                failed.append({"keep": keep["id"], "error": str(e)[:200]})
                continue
            errs = [p for p in j.get("update") or [] if p.get("error")]
            if errs:
                failed.append({"keep": keep["id"], "error": str(errs[0].get("error"))[:200]})
                continue
        done_groups += 1
        rows.append({"sku": keep.get("sku", ""), "product_id": keep["id"], "role": "keep", "keep_id": keep["id"],
                     "keep_sku": keep.get("sku", ""), "name": g["name"], "price_usd": price,
                     "categories": ",".join(map(str, cats)), "canonical": "", "merged_at": today})
        for dp in g.get("dups") or []:
            rows.append({"sku": dp.get("sku", ""), "product_id": dp["id"], "role": "dup", "keep_id": keep["id"],
                         "keep_sku": keep.get("sku", ""), "name": "", "price_usd": price, "categories": "",
                         "canonical": link, "merged_at": today})
    if rows and not dry_run:
        p = _reg_path(cfg)
        old = read_csv(p) if p.exists() else pd.DataFrame(columns=REG_COLS)
        allr = pd.concat([old, pd.DataFrame(rows, columns=REG_COLS)], ignore_index=True)
        write_csv(allr.drop_duplicates("sku", keep="last"), p)
    log.info("merge: %d group(s) done, %d failed%s", done_groups, len(failed), " (plan only)" if dry_run else "")
    return {"groups": done_groups, "failed": failed, "products": len(rows)}


def check(cfg: dict, keep_id: int, dup_id: int, wc: WC | None = None) -> dict:
    """What the site now says for one merged pair (for a look after a run)."""
    wc = wc or WC()
    k, d = wc.get_product(keep_id), wc.get_product(dup_id)
    meta = {m.get("key"): m.get("value") for m in d.get("meta_data") or []}
    return {"keep": {"name": k.get("name"), "price": k.get("regular_price"), "link": k.get("permalink"),
                     "categories": [c["id"] for c in k.get("categories") or []]},
            "dup": {"visibility": d.get("catalog_visibility"), "price": d.get("regular_price"),
                    "status": d.get("status"), "link": d.get("permalink"), "canonical": meta.get(CANONICAL_KEY)}}
