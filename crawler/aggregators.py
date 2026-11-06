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
    """Torob's lowest price for the same model, from Google's result for its page (6 Nov: Torob's own API
    answers bots with HTTP 490). 'From X at N shops' with N >= 2 counts as two shops at X."""
    prices, shops, _ = torob_snippet({}, f, rec)
    if not prices:
        return []
    low = min(prices)
    return [("torob:min", low)] + ([("torob:n", low)] if shops >= 2 else [])


def torob_snippet(cfg: dict, f, rec: dict) -> tuple[list[float], int, str]:
    """(6 Nov) Torob's price line as Google shows it ('از ۲۱٫۵۳۸٫۰۰۰ تومان در ۱۶ فروشگاه'): one search, no
    request to Torob (its API answers bots with HTTP 490). Returns (prices, shops, title) of the first result
    that is the same model; ([], 0, '') when none."""
    from .config import secret
    from .names import clean_name
    from .normalize import fa_digits_to_en, parse_number
    key = secret("SERPER_API_KEY")
    if not key:
        return [], 0, ""
    q = f"site:torob.com {clean_name(str(rec.get('name') or ''))}"
    res = f.post_json("https://google.serper.dev/search", {"q": q, "gl": "ir", "hl": "fa", "num": 5},
                      {"X-API-KEY": key, "Content-Type": "application/json"})
    from .detective import _same_model
    for o in ((res or {}).get("json") or {}).get("organic") or []:
        title = re.sub(r"\s*[|\-–]\s*ترب.*$", "", str(o.get("title") or "")).replace("قیمت و خرید", "").strip()
        if "torob.com" not in str(o.get("link") or "") or not _same_model(rec, title):
            continue
        text = fa_digits_to_en(f"{o.get('snippet', '')} {o.get('price', '')}").replace("٫", ",")
        prices = []
        for m in re.finditer(r"(\d[\d,٬.]{4,})\s*(تومان|تومن|ریال)", text):
            v = parse_number(m.group(1))
            if v:
                prices.append(v / 10 if m.group(2) == "ریال" else v)
        n = re.search(r"در\s*(\d+)\s*فروشگاه", text)
        if prices:
            return prices, int(n.group(1)) if n else 1, title
    return [], 0, ""


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
