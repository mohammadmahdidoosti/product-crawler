"""Price comparison sites as evidence only: what other shops ask for the same model.

Nothing from these sites is published or copied into products; their offers are only compared
with our own price by the price detective (crawler/detective.py), and the prices are remembered
per shop (price memory) so the same model needs no lookup next time.

  Torob: its public search API, then the product's offers (shop + price), free (no search credit)
  Emalls: the price line Google shows for an Emalls page (one Serper search, only when Torob found
          nothing)
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


def torob_offers(f, rec: dict, same_model) -> list[tuple[str, float]]:
    from .names import clean_name
    q = clean_name(str(rec.get("name") or ""))
    res = f.get("https://api.torob.com/v4/base-product/search/", params={"q": q, "page": 0, "size": 8}, as_json=True)
    if not res or res.get("status") != 200:
        log.info("torob search not available for %s (%s)", q[:40], res.get("status") if res else "blocked/none")
        return []
    out = []
    for it in ((res.get("json") or {}).get("results") or [])[:4]:
        name = it.get("name1") or it.get("name") or ""
        if not name or not same_model(rec, name):
            continue
        det = f.get("https://api.torob.com/v4/base-product/details/", params={"prk": it.get("random_key")}, as_json=True)
        offers = (((det or {}).get("json") or {}).get("products_info") or {}).get("result") or []
        for o in offers:
            p = o.get("price")
            shop = o.get("shop_text") or o.get("shop_name") or ""
            if isinstance(p, (int, float)) and p > 0 and shop and o.get("availability", True) is not False:
                out.append((f"torob:{shop}", float(p)))
        if not offers and isinstance(it.get("price"), (int, float)) and it["price"] > 0:
            out.append(("torob:min", float(it["price"])))       # the product's lowest price only
        if out:
            break
    return out


def emalls_range(cfg: dict, f, rec: dict) -> list[tuple[str, float]]:
    from .config import secret
    from .names import clean_name
    from .normalize import fa_digits_to_en
    from .normalize import parse_number
    key = secret("SERPER_API_KEY")
    if not key:
        return []
    q = f"site:emalls.ir {clean_name(str(rec.get('name') or ''))}"
    res = f.post_json("https://google.serper.dev/search", {"q": q, "gl": "ir", "hl": "fa", "num": 5},
                      {"X-API-KEY": key, "Content-Type": "application/json"})
    out = []
    from .detective import _same_model
    for o in ((res or {}).get("json") or {}).get("organic") or []:
        if not _same_model(rec, o.get("title", "")):
            continue
        text = fa_digits_to_en(f"{o.get('snippet', '')} {o.get('price', '')}")
        for m in re.finditer(r"(\d[\d,٬.]{4,})\s*(تومان|تومن|ریال)", text):
            v = parse_number(m.group(1))
            if v:
                out.append(("emalls", v / 10 if m.group(2) == "ریال" else v))
        if out:
            break
    return out
