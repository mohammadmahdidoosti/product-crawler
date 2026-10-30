"""Business lists: public contacts of businesses of one trade (possible customers), collection only.

1. places  -> Google Maps through Serper (/maps): every term x every city, 2 pages each: name, the
              phone the business itself published, address, website, category, rating.
2. enrich  -> each business's own website (home page + up to 2 contact/about pages, robots.txt
              respected, slow): the phones, Instagram, Telegram, WhatsApp, Eitaa and e-mail the
              business published for sales and support.
Out: data/runs/leads-<name>/leads.csv (and 01_places.csv). Nothing is sent to anyone: no messages,
no sign-ups; only what a business made public for its customers is kept.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd

from .config import secret
from .io import read_csv, write_csv

log = logging.getLogger(__name__)

CITIES = ["تهران", "کرج", "اصفهان", "مشهد", "شیراز", "تبریز", "قم", "اهواز", "رشت", "ساری", "کیش", "یزد",
          "کرمان", "همدان", "ارومیه", "قزوین", "بندرعباس", "گرگان", "کاشان", "زنجان"]
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
MOBILE = re.compile(r"(?<!\d)(?:\+?98|0098|0)?\s?(9\d{2})[\s\-]?(\d{3})[\s\-]?(\d{4})(?!\d)")
LANDLINE = re.compile(r"(?<!\d)(?:\+?98|0098|0)\s?([1-8]\d)[\s\-]?(\d{4})[\s\-]?(\d{4})(?!\d)")
SOCIAL = {
    "instagram": re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})", re.I),
    "telegram": re.compile(r"(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})", re.I),
    "whatsapp": re.compile(r"(?:wa\.me|api\.whatsapp\.com/send\?phone=)/?\+?(\d{10,13})", re.I),
    "eitaa": re.compile(r"eitaa\.com/([A-Za-z][A-Za-z0-9_]{2,31})", re.I),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{2,40}@[A-Za-z0-9.\-]{2,60}\.[A-Za-z]{2,10}")
NOT_HANDLES = {"p", "reel", "explore", "share", "joinchat", "s", "accounts", "stories", "addstickers", "tv"}
CONTACT = re.compile(r"contact|about|تماس|درباره|ارتباط", re.I)
COLS = ["name", "phone", "phone_type", "mobiles", "landlines", "instagram", "telegram", "whatsapp", "eitaa", "email",
        "website", "site_status", "city", "address", "category", "rating", "reviews", "query", "maps_cid"]


def norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", str(p or "").translate(FA_DIGITS))
    if d.startswith("0098"):
        d = "0" + d[4:]
    elif d.startswith("98") and len(d) > 10:
        d = "0" + d[2:]
    elif len(d) == 10 and d.startswith("9"):
        d = "0" + d
    return d


def phone_type(p: str) -> str:
    return "موبایل" if p.startswith("09") else ("ثابت" if p else "")


def contacts(html: str) -> dict:
    """Phones, social handles and e-mails published on one page (Persian digits read too)."""
    t = str(html or "").translate(FA_DIGITS)
    mob = sorted({"0" + "".join(m.groups()) for m in MOBILE.finditer(t)})
    land = sorted({"0" + "".join(m.groups()) for m in LANDLINE.finditer(t)})
    out = {"mobiles": mob, "landlines": [x for x in land if x not in mob]}
    for k, rx in SOCIAL.items():
        vals = {m.group(1).rstrip(".") for m in rx.finditer(t)}
        out[k] = sorted(v for v in vals if v.lower() not in NOT_HANDLES)
    out["email"] = sorted({e for e in EMAIL.findall(t) if not e.lower().endswith((".png", ".jpg", ".webp", ".svg"))})[:3]
    return out


def contact_links(base: str, html: str) -> list[str]:
    host = urlparse(base).netloc
    out = []
    for href, text in re.findall(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', str(html or ""), re.I | re.S):
        u = urljoin(base, href)
        if urlparse(u).netloc == host and (CONTACT.search(href) or CONTACT.search(re.sub("<[^>]+>", "", text))):
            if u not in out:
                out.append(u)
    return out[:2]


def places(cfg: dict, terms: list[str], cities: list[str], progress=None, stop=None) -> pd.DataFrame:
    import requests
    key = secret("SERPER_API_KEY")
    if not key:
        raise ValueError("کلید SERPER_API_KEY روی سرور تنظیم نشده")
    seen, rows, calls = set(), [], 0
    total = len(terms) * len(cities)
    for ci, city in enumerate(cities):
        for ti, term in enumerate(terms):
            if stop and stop():
                break
            q = f"{term} {city}"
            for page in (1, 2):
                try:
                    r = requests.post("https://google.serper.dev/maps", timeout=30, headers={"X-API-KEY": key},
                                      json={"q": q, "gl": "ir", "hl": "fa", "page": page})
                    calls += 1
                    got = r.json().get("places", [])
                except Exception as e:                      # noqa: BLE001 - one query must not stop the rest
                    log.warning("leads maps %s: %s", q, e)
                    break
                for p in got:
                    ph = norm_phone(p.get("phoneNumber"))
                    k = ph or (p.get("title", "") + p.get("address", ""))
                    if k in seen:
                        continue
                    seen.add(k)
                    rows.append({"name": p.get("title", ""), "phone": ph, "website": p.get("website", ""),
                                 "address": p.get("address", ""), "city": city, "category": p.get("type", ""),
                                 "rating": p.get("rating", ""), "reviews": p.get("ratingCount", ""), "query": q,
                                 "maps_cid": p.get("cid", "")})
                if len(got) < 15:
                    break
                time.sleep(0.4)
            if progress:
                progress(ci * len(terms) + ti + 1, total)
    log.info("leads: %d businesses from %d map searches (Serper)", len(rows), calls)
    df = pd.DataFrame(rows)
    df.attrs["calls"] = calls
    return df


def enrich(cfg: dict, df: pd.DataFrame, progress=None, stop=None) -> pd.DataFrame:
    """Reads each business's own site (home + contact pages) for the contacts it published."""
    from .fetch import Fetcher
    f = Fetcher(cfg, "iran")
    sites = [w for w in df.get("website", pd.Series(dtype=str)).fillna("") if w.startswith("http")]
    found: dict[str, dict] = {}
    status: dict[str, str] = {}
    for i, w in enumerate(dict.fromkeys(sites)):
        if stop and stop():
            break
        acc = {k: set() for k in ("mobiles", "landlines", *SOCIAL, "email")}
        r = f.get(w)
        if not r or r.get("status") != 200:
            status[w] = f"HTTP {r.get('status')}" if r else "not reached"
        else:
            status[w] = "ok"
            pages = [r.get("text", "")]
            for u in contact_links(r.get("url") or w, r.get("text", "")):
                rr = f.get(u)
                if rr and rr.get("status") == 200:
                    pages.append(rr.get("text", ""))
            for html in pages:
                for k, v in contacts(html).items():
                    acc[k] |= set(v)
        found[w] = {k: " | ".join(sorted(v)[:4]) for k, v in acc.items()}
        if progress:
            progress(i + 1, len(sites))
    out = df.copy()
    for k in ("mobiles", "landlines", *SOCIAL, "email"):
        out[k] = [found.get(w, {}).get(k, "") for w in out.get("website", pd.Series("", index=out.index)).fillna("")]
    out["site_status"] = [status.get(w, "") for w in out.get("website", pd.Series("", index=out.index)).fillna("")]
    # the map phone counts too
    out["mobiles"] = [" | ".join(sorted(set(x for x in str(m).split(" | ") if x) | ({p} if p.startswith("09") else set())))
                      for m, p in zip(out["mobiles"], out["phone"])]
    log.info("leads: %d sites read, %d with a mobile, %d with Instagram", sum(1 for s in status.values() if s == "ok"),
             int((out["mobiles"] != "").sum()), int((out["instagram"] != "").sum()))
    return out


def run(cfg: dict, name: str, terms: list[str], cities: list[str] | None = None, read_sites: bool = True,
        progress=None, stop=None) -> dict:
    d = Path(cfg["_data_dir"]) / "runs" / f"leads-{name}"
    d.mkdir(parents=True, exist_ok=True)
    cities = cities or CITIES
    df = places(cfg, terms, cities, progress=(lambda a, b: progress("places", a, b)) if progress else None, stop=stop)
    from .costs import add
    add(d, "maps_search", df.attrs.get("calls", 0), round(df.attrs.get("calls", 0) * 0.001, 4), "serper maps",
        estimated=True)
    write_csv(df, d / "01_places.csv")
    if df.empty:
        return {"run": d.name, "businesses": 0}
    if read_sites:
        df = enrich(cfg, df, progress=(lambda a, b: progress("sites", a, b)) if progress else None, stop=stop)
    df["phone_type"] = df["phone"].map(phone_type)
    write_csv(df.reindex(columns=COLS, fill_value=""), d / "leads.csv")
    res = {"run": d.name, "businesses": len(df), "with_phone": int((df["phone"] != "").sum()),
           "with_mobile": int((df.get("mobiles", pd.Series("", index=df.index)) != "").sum()),
           "with_instagram": int((df.get("instagram", pd.Series("", index=df.index)) != "").sum()),
           "with_site": int((df["website"] != "").sum())}
    log.info("leads %s: %s", name, res)
    return res


def runs(cfg: dict) -> list[dict]:
    out = []
    for d in sorted((Path(cfg["_data_dir"]) / "runs").glob("leads-*"), key=lambda x: x.stat().st_mtime, reverse=True):
        p = d / "leads.csv"
        if p.exists():
            t = read_csv(p)
            out.append({"run": d.name, "name": d.name[6:], "businesses": len(t),
                        "with_mobile": int((t.get("mobiles", pd.Series(dtype=str)).fillna("") != "").sum()),
                        "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))})
    return out
