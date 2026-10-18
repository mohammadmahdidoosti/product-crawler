"""Top-up: a second, targeted pass for categories still (nearly) empty after the filter.

For each such category, in this order:
  1. Google (Serper) with the exact category name, as a person would search it;
     plus the Latin form ('STIHL GTA26') when the brand has one, and 'خرید …'
     when the first search found almost nothing
  2. for an empty category: brand + model searched in every WooCommerce shop met so far
  3. a generic (unbranded) category: Made-in-China with its English term
  4. Torob as a directory only: which shops sell this model; their own pages are crawled
     (Torob's content is never used)
Then pages that failed earlier are retried once with a longer timeout, and the new
links are crawled and filtered. Every category's outcome is in 02_topup_done.csv.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pandas as pd

from .config import run_dir, secret
from .fetch import Fetcher, canon_url, domain_of
from .io import FILES, HITS_COLS, Appender, done_values, read_csv, write_csv

log = logging.getLogger(__name__)
RETRY = re.compile(r"http (5\d\d|blocked/none)|error", re.I)


def _serper(f: Fetcher, key: str, q: str, num: int = 10) -> list[dict]:
    from .probe import classify_results
    res = f.post_json("https://google.serper.dev/search", {"q": q, "gl": "ir", "hl": "fa", "num": num},
                      {"X-API-KEY": key, "Content-Type": "application/json"})
    return classify_results((res or {}).get("json", {}).get("organic", []))["domains"] if res else []


def latin_query(row: dict) -> str:
    """'اره شارژی اشتیل gta26' -> 'STIHL gta26' (shops often write the model in Latin)."""
    from .routing import LATIN, leaf_brand
    leaf = row["breadcrumb"].split(">")[-1].strip()
    b = leaf_brand(leaf)
    if not b:
        return ""
    lat = LATIN.get(b[0]) or (b[0] if b[0].isascii() else "")
    if not any(re.search(r"[0-9۰-۹]", w) for w in leaf.split()):
        return ""
    codes = [w for w in leaf.split() if re.fullmatch(r"[A-Za-z0-9.\-/]+", w) and w.lower() != lat.lower()]
    return f"{lat.upper()} {' '.join(codes)}".strip() if lat and codes else ""


def all_woo_shops(cfg: dict) -> list[dict]:
    from .search import _base
    shops = [s for s in (cfg["sources"].get("iran_sites") or []) if s.get("type") == "woo_store_api"]
    p = cfg["_data_dir"] / "discovered.csv"
    if p.exists():
        d = read_csv(p)
        shops += [{"domain": r["domain"], "type": "woo_store_api"} for r in d.to_dict("records")
                  if r.get("type") == "woo_store_api"]
    seen, out = set(), []
    for s in shops:
        if s["domain"] not in seen:
            seen.add(s["domain"])
            out.append({**s, "kind": "iran", "base_url": _base(s)})
    return out


def torob_shops(f: Fetcher, query: str, n: int = 5) -> list[tuple[str, str]]:
    """Shop pages that sell this product, found through Torob's search (directory use only)."""
    out: list[tuple[str, str]] = []
    res = f.get("https://api.torob.com/v4/base-product/search/", params={"q": query, "page": 0, "size": 5},
                as_json=True)
    if not res or res.get("status") != 200:
        log.info("torob search not available (%s)", res.get("status") if res else "blocked/none")
        return out
    items = (res.get("json") or {}).get("results") or []
    for it in items[:3]:
        prk = it.get("random_key")
        if not prk:
            continue
        det = f.get("https://api.torob.com/v4/base-product/details/", params={"prk": prk}, as_json=True)
        if not det or det.get("status") != 200:
            continue
        offers = ((det.get("json") or {}).get("products_info") or {}).get("result") or []
        for o in offers:
            url, shop = o.get("page_url") or "", o.get("shop_text") or o.get("shop_name") or ""
            if not url:
                continue
            if "torob.com" in url:                         # a Torob redirect: follow it to the shop's own page
                r = f.get(url, use_cache=False)
                url = (r or {}).get("url", "")
            if url and "torob.com" not in url:
                out.append((url, shop))
            if len(out) >= n:
                return out
    return out


def retry_failed(cfg: dict, d: Path) -> int:
    """Pages that timed out or got a server error get one more try (the crawl re-reads them)."""
    p = d / "03_failed.csv"
    if not p.exists():
        return 0
    fl = read_csv(p)
    if fl.empty:
        return 0
    again = fl["reason"].str.contains(RETRY)
    if not again.any():
        return 0
    write_csv(fl[~again], p, ["taxonomy_id", "url", "reason"])
    return int(again.sum())


def step_topup(cfg: dict, run: str) -> Path | None:
    from .pipeline import _fetchers, _progress, step_crawl, step_filter
    from .probe import record_usage
    from .routing import leaf_brand
    from .search import SEARCHERS, _hit, detect_type, search_mic, search_store_api
    sc = cfg.get("search", {})
    below, cap = int(sc.get("topup_below", 5)), int(sc.get("topup_max", 80))
    d = run_dir(cfg, run)
    key = secret("SERPER_API_KEY")
    if not below or not (d / FILES["filtered"]).exists():
        log.info("top-up skipped (off or nothing filtered yet)")
        return None
    q = read_csv(d / FILES["queue"]).to_dict("records")
    fl = read_csv(d / FILES["filtered"])
    cnt = fl.groupby("taxonomy_id")["url"].nunique().to_dict() if len(fl) else {}
    mark = d / "02_topup_done.csv"
    done = done_values(mark, "taxonomy_id")
    low = [r for r in q if cnt.get(r["taxonomy_id"], 0) < below and r["taxonomy_id"] not in done][:cap]
    log.info("top-up: %d categories under %d products", len(low), below)
    fx = _fetchers(cfg)
    f, ff = fx["iran"], fx["foreign"]
    shops_all = all_woo_shops(cfg)
    use_torob = bool(sc.get("torob_directory", True))
    n_hits = 0
    cols = ["taxonomy_id", "query", "found", "web", "all_shops", "made_in_china", "torob"]
    with Appender(d / FILES["hits"], HITS_COLS) as w, Appender(mark, cols) as m:
        for i, r in enumerate(low, 1):
            if _progress("topup", i - 1, len(low)):
                break
            leaf = r["breadcrumb"].split(">")[-1].strip()
            brand = leaf_brand(leaf)
            got = {"web": 0, "all_shops": 0, "made_in_china": 0, "torob": 0}
            hits: list[dict] = []
            # 1. web search, a few forms
            if key and r["route"] in ("iran", "both"):
                queries = [f"{r['query']} قیمت"]
                lq = latin_query(r)
                if lq:
                    queries.append(lq)
                for k, qq in enumerate(queries + [f"خرید {r['query']}"]):
                    if k == len(queries) and got["web"] >= 2:
                        break                                   # 'خرید …' only when little was found
                    shops = _serper(f, key, qq)
                    record_usage(cfg)
                    for sh in shops:
                        src = {"domain": sh["domain"], "kind": "iran"}
                        hits.append(_hit(r, src, sh["url"], sh.get("title", ""), "web_exact"))
                        got["web"] += 1
                        t = detect_type(f, cfg, sh["domain"])
                        if t in SEARCHERS:
                            try:
                                hits += SEARCHERS[t](f, {**src, "type": t}, r, 8)
                            except Exception as e:
                                log.warning("top-up shop search failed on %s: %s", sh["domain"], e)
            # 2. empty category: every WooCommerce shop met so far
            if cnt.get(r["taxonomy_id"], 0) == 0 and r["route"] in ("iran", "both") and shops_all:
                def one(src, r=r):
                    try:
                        return search_store_api(f, src, r, 5)
                    except Exception:
                        return []
                with ThreadPoolExecutor(8) as ex:
                    for hs in ex.map(one, shops_all[:60]):
                        hits += hs
                        got["all_shops"] += len(hs)
            # 3. generic category: Made-in-China too
            if not brand and r.get("english"):
                try:
                    hs = search_mic(ff, {**r, "route": "foreign"}, r["english"], 12,
                                    int(sc.get("per_supplier", 2)))
                    hits += hs
                    got["made_in_china"] = len(hs)
                except Exception as e:
                    log.warning("top-up Made-in-China failed: %s", e)
            # 4. Torob as a directory of shops
            if use_torob and r["route"] in ("iran", "both"):
                try:
                    for url, shop in torob_shops(f, r["query"]):
                        hits.append(_hit(r, {"domain": domain_of(url), "kind": "iran"}, url, shop, "torob_directory"))
                        got["torob"] += 1
                except Exception as e:
                    log.warning("torob lookup failed: %s", e)
            seen = set()
            for h in hits:
                if h["url"] not in seen:
                    seen.add(h["url"])
                    w.write(h)
            n_hits += len(seen)
            m.write({"taxonomy_id": r["taxonomy_id"], "query": r["query"], "found": len(seen), **got})
    n_retry = retry_failed(cfg, d)
    log.info("top-up: %d new links, %d failed pages to retry; crawling and filtering", n_hits, n_retry)
    cfg2 = deepcopy({k: v for k, v in cfg.items() if k != "_data_dir"})
    cfg2["_data_dir"] = cfg["_data_dir"]
    cfg2.setdefault("http", {})["timeout_seconds"] = max(60, int(cfg.get("http", {}).get("timeout_seconds", 25)))
    step_crawl(cfg2, run)
    return step_filter(cfg, run)
