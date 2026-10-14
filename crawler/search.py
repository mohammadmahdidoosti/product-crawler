"""Step 2: find candidate product pages for each queue row.

Source types (set per site in sources.yaml):
  woo_store_api  /wp-json/wc/store/v1/products?search=…  -> full product JSON,
                 no page crawl needed. Many Iranian WooCommerce shops have it.
  woo_search     /?s=…&post_type=product  -> product links from result page
  url_template   any search URL + a CSS selector for product links
  web            a web-search API (Serper) across the whole web, minus the
                 excluded domains (Torob, Digikala…); used to discover sites
"""
from __future__ import annotations

import json
import logging
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from .config import secret
from .fetch import Fetcher, domain_of
from .normalize import norm_text

log = logging.getLogger(__name__)


def _overlap_ok(query: str, title: str) -> bool:
    q = {t for t in norm_text(query).split() if len(t) > 2}
    return not q or bool(q & set(norm_text(title).split()))


def _base(src: dict) -> str:
    return (src.get("base_url") or f"https://{src['domain']}").rstrip("/")


def _hit(row: dict, src: dict, url: str, title: str, found_by: str, prefilled: dict | None = None) -> dict:
    return {
        "taxonomy_id": row["taxonomy_id"], "query": row["query"], "route": row["route"],
        "source_domain": domain_of(url) or src.get("domain", ""),
        "source_type": src.get("kind", "iran"), "url": url, "title": title,
        "found_by": found_by,
        "prefilled_json": json.dumps(prefilled, ensure_ascii=False) if prefilled else "",
    }


def search_store_api(f: Fetcher, src: dict, row: dict, n: int) -> list[dict]:
    base = _base(src)
    res = f.get(f"{base}/wp-json/wc/store/v1/products",
                params={"search": row["query"], "per_page": n}, as_json=True)
    if not res or res.get("status") != 200 or not isinstance(res.get("json"), list):
        return []
    out = []
    for item in res["json"][:n]:
        url, title = item.get("permalink", ""), item.get("name", "")
        if url and _overlap_ok(row["query"], title):
            out.append(_hit(row, src, url, title, "woo_store_api", item))
    return out


def _links_from_page(html: str, base: str, selector: str, n: int) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    seen, out = set(), []
    for a in soup.select(selector):
        href = a.get("href")
        if not href:
            continue
        url = urljoin(base, href).split("#")[0]
        if url in seen:
            continue
        seen.add(url)
        out.append((url, a.get_text(" ", strip=True) or a.get("title", "")))
        if len(out) >= n:
            break
    return out


def search_woo(f: Fetcher, src: dict, row: dict, n: int) -> list[dict]:
    base = _base(src) + "/"
    res = f.get(base, params={"s": row["query"], "post_type": "product"})
    if not res or res.get("status") != 200:
        return []
    sel = src.get("link_selector", "li.product a.woocommerce-LoopProduct-link, .products .product a[href*='/product/']")
    return [_hit(row, src, u, t, "woo_search") for u, t in _links_from_page(res["text"], base, sel, n)
            if _overlap_ok(row["query"], t)]


def search_template(f: Fetcher, src: dict, row: dict, n: int) -> list[dict]:
    url = src["search_url"].format(query=quote_plus(row["query"]))
    res = f.get(url)
    if not res or res.get("status") != 200:
        return []
    return [_hit(row, src, u, t, "url_template") for u, t in
            _links_from_page(res["text"], url, src["link_selector"], n) if _overlap_ok(row["query"], t)]


def search_web(f: Fetcher, cfg: dict, row: dict, query: str, n: int, site: str | None = None) -> list[dict]:
    web = cfg["sources"].get("web_search") or {}
    key = secret(web.get("api_key_env", "SERPER_API_KEY"))
    if not key:
        return []
    excl = web.get("exclude_domains") or []
    q = f"site:{site} {query}" if site else query + "".join(f" -site:{d}" for d in excl)
    res = f.post_json("https://google.serper.dev/search",
                      {"q": q, "num": n, **({"gl": "us", "hl": "en"} if site else
                                            {"gl": web.get("gl", "ir"), "hl": web.get("hl", "fa")})},
                      {"X-API-KEY": key, "Content-Type": "application/json"})
    if not res:
        return []
    src = {"kind": "foreign" if site else "iran"}
    out = []
    for o in res["json"].get("organic", [])[:n]:
        url = o.get("link", "")
        if not url or any(domain_of(url).endswith(d) for d in excl):
            continue
        out.append(_hit(row, src, url, o.get("title", ""), "web_search"))
    return out


SEARCHERS = {"woo_store_api": search_store_api, "woo_search": search_woo, "url_template": search_template}


def sources_for(cfg: dict, row: dict) -> list[dict]:
    """Iranian site sources whose `level1` filter (if any) matches the row."""
    out = []
    for s in cfg["sources"].get("iran_sites") or []:
        cats = s.get("level1") or []
        if not cats or any(norm_text(c) in norm_text(row["breadcrumb"]) for c in cats):
            out.append({**s, "kind": "iran"})
    return out


def search_row(cfg: dict, fetchers: dict[str, Fetcher], row: dict, english_query: str | None = None) -> list[dict]:
    n = int(cfg.get("search", {}).get("max_hits_per_source", 8))
    hits: list[dict] = []
    if row["route"] in ("iran", "both"):
        f = fetchers["iran"]
        for src in sources_for(cfg, row):
            fn = SEARCHERS.get(src.get("type", "woo_search"))
            if not fn:
                log.warning("unknown source type %s", src.get("type"))
                continue
            try:
                hits += fn(f, src, row, n)
            except Exception as e:  # one broken site must not stop the run
                log.warning("search failed on %s: %s", src.get("domain"), e)
        if cfg.get("search", {}).get("use_web_search", False):
            hits += search_web(f, cfg, row, row["query"], n)
    if row["route"] in ("foreign", "both") and english_query:
        f = fetchers["foreign"]
        for site in cfg["sources"].get("foreign_sites") or []:
            hits += search_web(f, cfg, row, english_query, n, site=site["domain"])
    # same URL found twice for the same row -> keep one
    seen, uniq = set(), []
    for h in hits:
        if h["url"] not in seen:
            seen.add(h["url"])
            uniq.append(h)
    return uniq
