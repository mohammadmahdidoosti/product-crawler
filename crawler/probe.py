"""Step 2 of routing: measure the Iranian market with one Persian web search.

One search per category GROUP (the parent of the leaves, level 3), not per
leaf: siblings share a market, and this keeps the whole taxonomy inside
Serper's free 2,500 searches. The result decides the route of every leaf in
the group that the free rules left open, and the shops it finds become
candidate sources ("discovered sources") for the crawl.

Decision (thresholds in config routing.*):
  priced Iranian shops >= iran_min_priced     -> iran
  Iranian shops        <= foreign_max_shops   -> foreign (Made-in-China)
  otherwise                                   -> both
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

from .config import secret
from .fetch import Fetcher, domain_of
from .io import ENCODING, read_csv, write_csv
from .normalize import fa_digits_to_en, norm_text

log = logging.getLogger(__name__)

NOT_SHOPS = {  # marketplaces/aggregators, media and social sites are never sources
    "torob.com", "digikala.com", "divar.ir", "sheypoor.com", "emalls.ir", "basalam.com", "caravansanat.com",
    "aparat.com", "instagram.com", "youtube.com", "wikipedia.org", "fa.wikipedia.org", "virgool.io",
    "namnak.com", "blogfa.com", "telegram.me", "t.me", "linkedin.com", "pinterest.com", "facebook.com",
    "alibaba.com", "aliexpress.com", "made-in-china.com", "amazon.com", "ebay.com", "zoomit.ir",
    "digikala.com", "snapp.market", "tapsi.ir", "iranestekhdam.ir", "balad.ir", "neshan.org",
}
PRODUCT_PATH = re.compile(r"/(product|products|shop|p|item|kala|محصول|فروشگاه)/", re.I)
PRICE_TEXT = re.compile(r"\d[\d,٬.]*\s*(تومان|تومن|ریال)|قیمت")
COLS = ["group", "query", "shops", "priced", "results", "domains_json", "searched_at"]


def probe_file(cfg) -> Path:
    return cfg["_data_dir"] / "probes.csv"


def usage_file(cfg) -> Path:
    return cfg["_data_dir"] / "serper_usage.csv"


def load_probes(cfg) -> dict[str, dict]:
    p = probe_file(cfg)
    if not p.exists():
        return {}
    return {r["group"]: r for r in read_csv(p).to_dict("records")}


_USAGE_LOCK = __import__("threading").Lock()


def record_usage(cfg, n: int = 1) -> int:
    """Searches used so far. Counting happens where a search is really sent (Fetcher.post_json:
    a cached answer costs nothing), so the callers' own +1 is no longer added."""
    with _USAGE_LOCK:
        return _record_usage(cfg, 0)


def _count(cfg) -> int:
    with _USAGE_LOCK:                                  # several threads count searches at once
        return _record_usage(cfg, 1)


def _record_usage(cfg, n: int = 1) -> int:
    p = usage_file(cfg)
    df = read_csv(p) if p.exists() else pd.DataFrame(columns=["date", "searches"])
    today = dt.date.today().isoformat()
    if not n:
        return int(pd.to_numeric(df["searches"]).sum()) if len(df) else 0
    if len(df) and df.iloc[-1]["date"] == today:
        df.loc[df.index[-1], "searches"] = str(int(df.iloc[-1]["searches"]) + n)
    else:
        df = pd.concat([df, pd.DataFrame([{"date": today, "searches": str(n)}])], ignore_index=True)
    write_csv(df, p)
    return int(pd.to_numeric(df["searches"]).sum())


def total_usage(cfg) -> int:
    p = usage_file(cfg)
    return int(pd.to_numeric(read_csv(p)["searches"]).sum()) if p.exists() else 0


def group_query(group: str) -> str:
    """'شیرآلات > اکچویتور برقی و محرک شیر > محرک الکتریکی' -> 'محرک الکتریکی شیر' style query."""
    parts = [p.strip() for p in group.split(">")]
    title = parts[-1]
    if len(norm_text(title).split()) <= 1 and len(parts) > 1:
        title = f"{title} {parts[-2]}"
    return f"خرید {title} قیمت"


def classify_results(organic: list[dict]) -> dict:
    shops: dict[str, dict] = {}
    for o in organic:
        url = o.get("link", "")
        dom = domain_of(url)
        if not dom or any(dom == d or dom.endswith("." + d) for d in NOT_SHOPS):
            continue
        is_ir = dom.endswith(".ir") or "ایران" in o.get("title", "") or re.search(r"[آ-ی]", o.get("title", ""))
        if not is_ir:
            continue
        text = fa_digits_to_en(f"{o.get('title', '')} {o.get('snippet', '')} {o.get('price', '')}")
        priced = bool(o.get("price")) or bool(re.search(r"\d[\d,٬.]*\s*(تومان|تومن|ریال)", text))
        productish = bool(PRODUCT_PATH.search(urlparse(url).path)) or priced or "خرید" in o.get("title", "")
        if not productish:
            continue
        cur = shops.get(dom)
        if not cur or (priced and not cur["priced"]):
            shops[dom] = {"domain": dom, "url": url, "title": o.get("title", "")[:120], "priced": priced}
    lst = sorted(shops.values(), key=lambda s: -s["priced"])
    return {"shops": len(lst), "priced": sum(s["priced"] for s in lst), "domains": lst}


BRAND_SEP = " || "


def brand_key(group: str, brand: str) -> str:
    """Probe key of one brand inside a group: its own search finds the brand's site and dealers."""
    return f"{group}{BRAND_SEP}{brand}"


def brand_query(group: str, brand: str) -> str:
    title = [p.strip() for p in group.split(">")][-1]
    if norm_text(brand) in norm_text(title).split() or norm_text(brand) in norm_text(title):
        return f"خرید {title} قیمت"
    return f"خرید {title} {brand} قیمت"


def run_probe(cfg, group: str, fetcher: Fetcher | None = None, query: str | None = None) -> dict:
    key = secret(cfg.get("sources", {}).get("web_search", {}).get("api_key_env", "SERPER_API_KEY")) \
        or secret("SERPER_API_KEY")
    if not key:
        raise RuntimeError("SERPER_API_KEY is not set")
    f = fetcher or Fetcher(cfg, "iran")
    q = query or group_query(group)
    res = f.post_json("https://google.serper.dev/search",
                      {"q": q, "gl": "ir", "hl": "fa", "num": 10},
                      {"X-API-KEY": key, "Content-Type": "application/json"})
    if not res:
        raise RuntimeError("Serper did not answer (check the key and credit)")
    organic = res["json"].get("organic", [])
    c = classify_results(organic)
    return {"group": group, "query": q, "shops": c["shops"], "priced": c["priced"], "results": len(organic),
            "domains_json": json.dumps(c["domains"], ensure_ascii=False),
            "searched_at": dt.datetime.now().isoformat(timespec="seconds")}


def save_probes(cfg, rows: list[dict]) -> None:
    p = probe_file(cfg)
    old = read_csv(p) if p.exists() else pd.DataFrame(columns=COLS)
    new = pd.DataFrame(rows)
    df = pd.concat([old[~old["group"].isin(new["group"])] if len(old) else old, new], ignore_index=True)
    write_csv(df, p, COLS)


def decide(probe: dict, cfg) -> tuple[str, str]:
    r = cfg.get("routing", {})
    iran_min = int(r.get("iran_min_priced", 3))
    foreign_max = int(r.get("foreign_max_shops", 1))
    shops, priced = int(probe["shops"]), int(probe["priced"])
    ev = f"{shops} فروشگاه ایرانی، {priced} قیمت‌دار"
    if priced >= iran_min:
        return "iran", f"سنجش بازار: {ev} ← منبع ایرانی"
    if shops <= foreign_max:
        return "foreign", f"سنجش بازار: {ev} ← بازار ایران ضعیف، Made-in-China"
    return "both", f"سنجش بازار: {ev} ← هر دو مسیر"
