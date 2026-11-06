"""Supplier finder (6 Nov, the owner): for a brand an order came in for, a list of likely suppliers.

The shop crawled often is not the supplier. The owner looked by hand: the brand's channel with a price list,
else Google 'نمایندگی X در تهران' and a phone call. Here, on demand, for one brand:

  1. Google (Serper search): representatives, importers, wholesalers ('نمایندگی', 'نماینده رسمی', 'پخش عمده',
     'واردکننده'), the brand's own site and channels Google knows (Telegram, Rubika, Eitaa)
  2. Google Maps (Serper places): shops and representatives in Tehran with phone, address, rating, site
  3. price lists: PDFs Google knows ('لیست قیمت X')
  4. our own crawl pool: shops that sell the brand, their item count and how cheap they are (the cheapest
     shops are often nearest to the source)

Every supplier gets a kind (official / wholesale / shop / channel / price list) and a score; the person's own
notes (DATA_DIR/suppliers/notes.csv: worked / real rep / good price / no) put the ones that answered first next time.
Results: DATA_DIR/suppliers/<brand>.json. One brand costs about 6-7 Serper searches.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from .io import read_csv, write_csv
from .normalize import norm_text

log = logging.getLogger(__name__)
NOTE_COLS = ["brand", "key", "name", "phone", "verdict", "note", "at"]
OFFICIAL = re.compile(r"نمایندگی\s*رسمی|نماینده\s*رسمی|نماینده\s*انحصاری|واردکننده|وارد\s*کننده|official|exclusive", re.I)
WHOLESALE = re.compile(r"پخش|عمده|بنکدار|توزیع|نمایندگی|نماینده|distribut", re.I)
CHANNEL_HOSTS = ("t.me", "telegram.me", "rubika.ir", "eitaa.com", "bale.ai", "instagram.com")
SKIP_HOSTS = ("torob.com", "emalls.ir", "digikala.com", "divar.ir", "wikipedia.org", "youtube.com", "aparat.com",
              "caravansanat.com", "sheypoor.com", "basalam.com")
PHONE = re.compile(r"(?<!\d)(?:0?9\d{9}|0\d{2,3}[-\s]?\d{7,8}|\+98\s?\d{9,10})(?!\d)")
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def sdir(cfg: dict) -> Path:
    d = Path(cfg["_data_dir"]) / "suppliers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _host(u: str) -> str:
    h = urlparse(str(u or "")).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _phones(text: str) -> list[str]:
    t = str(text or "").translate(FA_DIGITS)
    return list(dict.fromkeys(re.sub(r"[-\s]", "", m) for m in PHONE.findall(t)))


def _serper(f, kind: str, q: str, num: int = 10) -> dict:
    from .config import secret
    key = secret("SERPER_API_KEY")
    if not key:
        return {}
    res = f.post_json(f"https://google.serper.dev/{kind}", {"q": q, "gl": "ir", "hl": "fa", "num": num},
                      {"X-API-KEY": key, "Content-Type": "application/json"})
    return (res or {}).get("json") or {}


def queries(brand: str) -> list[tuple[str, str]]:
    b = brand.strip()
    return [("search", f"نمایندگی {b} در تهران"), ("search", f"نماینده رسمی {b} واردکننده"),
            ("search", f"پخش عمده {b}"), ("search", f"لیست قیمت {b} pdf"),
            ("search", f"کانال {b} لیست قیمت تلگرام روبیکا ایتا"), ("places", f"نمایندگی {b} تهران"),
            ("places", f"{b} فروشگاه ابزار تهران")]


def kind_of(text: str, url: str) -> str:
    h = _host(url)
    if url.lower().split("?")[0].endswith(".pdf"):
        return "price_list"
    if any(h == c or h.endswith("." + c) for c in CHANNEL_HOSTS):
        return "channel"
    if OFFICIAL.search(text):
        return "official"
    if WHOLESALE.search(text):
        return "wholesale"
    return "shop"


RANK = {"official": 5, "wholesale": 4, "price_list": 3, "channel": 3, "shop": 1}


def from_pool(cfg: dict, brand: str, names: list[str]) -> list[dict]:
    """Shops of our own crawl that sell the brand: item count and how their prices compare (cheapest first)."""
    p = Path(cfg["_data_dir"]) / "crawl_pool.csv"
    if not p.exists():
        return []
    try:
        df = read_csv(p)
    except Exception:                                    # noqa: BLE001
        return []
    keys = [norm_text(n) for n in names if n and len(n) >= 2]
    nm = df["name"].map(norm_text)
    hit = df[nm.map(lambda s: any(k in s for k in keys)) & (df.get("source_type", "") != "foreign")].copy()
    if hit.empty:
        return []
    hit["v"] = pd.to_numeric(hit["price_toman"], errors="coerce")
    out = []
    med = hit["v"].median()
    for dom, g in hit.groupby("source_domain"):
        if not dom or any(s in dom for s in SKIP_HOSTS):
            continue
        rel = float((g["v"] / med).median()) if med and g["v"].notna().any() else None
        out.append({"name": dom, "url": f"https://{dom}", "kind": "shop", "source": "کراول خودمان",
                    "items": int(len(g)), "price_vs_median": round(rel, 2) if rel else "",
                    "phones": [], "address": "", "rating": "", "snippet": ""})
    out.sort(key=lambda x: (-x["items"], x["price_vs_median"] or 9))
    return out[:15]


def find(cfg: dict, brand: str, aliases: list[str] | None = None) -> dict:
    """The candidates for one brand, merged by site/phone, best first; saved to DATA_DIR/suppliers/<brand>.json."""
    from .pipeline import _fetchers
    f = _fetchers(cfg)["iran"]
    names = [brand] + list(aliases or [])
    found: dict[str, dict] = {}
    searches = 0

    def add(c: dict):
        k = _host(c.get("url")) or (c.get("phones") or [c.get("name")])[0]
        if not k:
            return
        old = found.get(k)
        if old is None:
            found[k] = {**c, "sources": [c["source"]]}
            return
        old["phones"] = list(dict.fromkeys(old["phones"] + c.get("phones", [])))
        old["sources"] = list(dict.fromkeys(old["sources"] + [c["source"]]))
        for f_ in ("address", "rating", "items", "price_vs_median"):
            if not old.get(f_) and c.get(f_):
                old[f_] = c[f_]
        if RANK[c["kind"]] > RANK[old["kind"]]:
            old["kind"] = c["kind"]

    for kind, q in queries(brand):
        j = _serper(f, kind, q)
        searches += 1
        if kind == "places":
            for p in j.get("places") or []:
                add({"name": p.get("title", ""), "url": p.get("website", ""), "phones": _phones(p.get("phoneNumber", "")),
                     "address": p.get("address", ""), "rating": p.get("rating", ""),
                     "kind": kind_of(f"{p.get('title', '')} {p.get('category', '')}", p.get("website", "")),
                     "source": "نقشه گوگل", "snippet": p.get("category", "")})
            continue
        for o in j.get("organic") or []:
            url, text = o.get("link", ""), f"{o.get('title', '')} {o.get('snippet', '')}"
            if any(s in _host(url) for s in SKIP_HOSTS):
                continue
            if not any(norm_text(n) in norm_text(text) or n.lower() in url.lower() for n in names):
                continue                                 # a page that does not name the brand
            add({"name": o.get("title", "")[:90], "url": url, "phones": _phones(text), "address": "",
                 "rating": "", "kind": kind_of(text, url), "source": f"گوگل: {q}", "snippet": o.get("snippet", "")[:200]})
    for c in from_pool(cfg, brand, names):
        add(c)
    notes = notes_for(cfg, brand)
    for k, c in found.items():
        n = notes.get(k)
        c["verdict"] = n.get("verdict", "") if n else ""
        c["score"] = (RANK[c["kind"]] * 10 + len(c["sources"]) * 3 + (5 if c["phones"] else 0)
                      + (min(int(c.get("items") or 0), 20) // 4) + {"good": 100, "ok": 50, "no": -100}.get(c["verdict"], 0))
        c["key"] = k
    out = {"brand": brand, "at": time.strftime("%Y-%m-%d %H:%M"), "searches": searches,
           "suppliers": sorted(found.values(), key=lambda c: -c["score"])}
    (sdir(cfg) / f"{norm_text(brand).replace(' ', '_')}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                                          encoding="utf-8")
    log.info("suppliers %s: %d found with %d searches", brand, len(found), searches)
    return out


def notes_for(cfg: dict, brand: str) -> dict[str, dict]:
    p = sdir(cfg) / "notes.csv"
    if not p.exists():
        return {}
    df = read_csv(p)
    df = df[df["brand"].map(norm_text) == norm_text(brand)]
    return {r["key"]: r for r in df.to_dict("records")}


def note(cfg: dict, brand: str, key: str, verdict: str, text: str = "", name: str = "", phone: str = "") -> dict:
    """The person's word on one supplier: good (real supplier, good price) / ok / no; kept for next time."""
    if verdict not in ("good", "ok", "no", ""):
        raise ValueError("verdict")
    p = sdir(cfg) / "notes.csv"
    df = read_csv(p) if p.exists() else pd.DataFrame(columns=NOTE_COLS)
    df = df[~((df["brand"].map(norm_text) == norm_text(brand)) & (df["key"] == key))]
    row = {"brand": brand, "key": key, "name": name, "phone": phone, "verdict": verdict, "note": text,
           "at": time.strftime("%Y-%m-%d %H:%M")}
    write_csv(pd.concat([df, pd.DataFrame([row])], ignore_index=True), p, NOTE_COLS)
    return row
