"""The steps, each reading the previous step's CSV in the same run folder."""
from __future__ import annotations

import datetime as dt
import json
import re
import logging
from pathlib import Path

import pandas as pd

from . import fx as fxmod
from .config import ROOT, run_dir
from .dedupe import load_site_catalog, merge_crawl, split_against_site
from .extract import (LISTING_TITLE, extract_page, from_store_api, is_listing_url, listing_links, listing_next,
                      resolve_price_unit)
from .fetch import Fetcher, canon_url, domain_of
from .io import (CRAWLED_COLS, FILES, FILTER_COLS, HITS_COLS, Appender, done_values,
                 read_csv, write_csv)
from .normalize import extract_phones, norm_text
from .search import search_row
from .taxonomy import build_queue, load_taxonomy

log = logging.getLogger(__name__)

# Set by the web UI: STOP asks a running step to finish cleanly after the
# current row; PROGRESS is read by the UI for its progress bar.
import threading  # noqa: E402

STOP = threading.Event()
PROGRESS: dict = {"step": "", "done": 0, "total": 0}


def _progress(step: str, done: int, total: int) -> bool:
    """Update progress; return True if the user asked to stop."""
    PROGRESS.update(step=step, done=done, total=total)
    if STOP.is_set():
        log.warning("stopped by user at %d/%d — run the step again to continue", done, total)
        return True
    return False


def _fetchers(cfg: dict) -> dict[str, Fetcher]:
    return {"iran": Fetcher(cfg, "iran"), "foreign": Fetcher(cfg, "foreign")}


def _brand_names(cfg: dict, merged: pd.DataFrame) -> set[str]:
    """Brand names known to the crawler (brands.csv, the site's brand table, the brands the shops wrote on
    this run's products), normalised: for keeping Made-in-China products out of a brand's category."""
    from .normalize import norm_text
    from .routing import brands
    names = {n for n, _, _ in brands()}
    try:
        from .brands_fa import load as load_brands
        t = load_brands(cfg)
        for c in [c for c in t.columns if c in ("brand", "name_fa", "fa", "persian", "display")]:
            names |= {norm_text(x) for x in t[c].astype(str) if x}
    except Exception:                                      # noqa: BLE001 - brands.csv alone then
        pass
    if "brand" in merged:
        names |= {norm_text(x) for x in merged["brand"].astype(str) if x and x.lower() != "nan"}
    return {x for x in names if len(x) >= 3}


# ------------------------------------------------------------------ 1 queue

def step_queue(cfg: dict, run: str, **filters) -> Path:
    tax_path = ROOT / cfg["taxonomy"]["file"]
    tax = load_taxonomy(tax_path)
    q = build_queue(tax, cfg, **filters)
    out = run_dir(cfg, run) / FILES["queue"]
    write_csv(q, out)
    log.info("queue: %d rows -> %s (routes: %s)", len(q), out.name, q["route"].value_counts().to_dict())
    return out


# ------------------------------------------------------------------ 2 search

def _english_queries(cfg: dict, rows: list[dict]) -> dict[str, str]:
    """Translate foreign-route queries once and cache them in data/translations.csv."""
    need = [r for r in rows if r["route"] in ("foreign", "both")]
    if not need or not cfg["sources"].get("foreign_sites"):
        return {}
    cache_p = cfg["_data_dir"] / "translations.csv"
    cache = dict(read_csv(cache_p)[["query", "english"]].values) if cache_p.exists() else {}
    todo = [r for r in need if r["query"] not in cache]
    if todo:
        from .llm import LLM, translate
        try:
            llm = LLM(cfg)
        except RuntimeError as e:
            log.warning("foreign route skipped (needs a language model to translate): %s", e)
            return cache
        for i in range(0, len(todo), 40):
            chunk = todo[i:i + 40]
            for r, en in zip(chunk, translate(llm, [(r["breadcrumb"], r["query"]) for r in chunk])):
                cache[r["query"]] = en
        write_csv(pd.DataFrame({"query": list(cache), "english": list(cache.values())}), cache_p)
    return cache


def step_search(cfg: dict, run: str) -> Path:
    d = run_dir(cfg, run)
    queue = read_csv(d / FILES["queue"]).to_dict("records")
    out = d / FILES["hits"]
    done = done_values(out, "taxonomy_id") | done_values(d / "02_no_hits.csv", "taxonomy_id")
    en = {} if all(r.get("english") for r in queue if r.get("route") in ("foreign", "both")) \
        else _english_queries(cfg, queue)
    from .probe import load_probes
    from .routing_state import english_for, group_of, load_glossary
    from .terms import leaf_english
    probes = load_probes(cfg)
    gl = load_glossary()
    syn = _synonyms(cfg)
    for r in queue:
        # a run made from the site's failed searches searches its approved terms first
        r["synonyms"] = " | ".join(x for x in (r.get("extra_queries", ""), syn.get(r["taxonomy_id"], "")) if x)
    for r in queue:           # English term for Made-in-China from the glossary when missing
        if not r.get("english"):
            g = r.get("group") or group_of(r["breadcrumb"])
            r["english"] = leaf_english(r["breadcrumb"].split(">")[-1].strip(), g.split(">")[-1].strip(),
                                        english_for(g, gl))
    f = _fetchers(cfg)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .rules import judge
    from .search import SERPER_CALLS
    calls0 = SERPER_CALLS[0]
    workers = max(1, int(cfg.get("http", {}).get("workers", 8)) // 2)
    n_hits = n_pre = 0
    todo = [r for r in queue if r["taxonomy_id"] not in done]

    def run_row(row):
        hits = search_row(cfg, f, row, en.get(row["query"]), probes)
        # links whose title already shows they do not fit (wrong brand, spare part, off-topic
        # English title) are dropped here, before their page is fetched
        g = row.get("group") or group_of(row["breadcrumb"])
        leaf = row["breadcrumb"].split(">")[-1].strip()
        keep, drop = [], []
        for h in hits:
            if h.get("title") and h.get("found_by") in ("woo_store_api", "made_in_china", "woo_search"):
                v = judge(row["breadcrumb"], leaf, {"name": h["title"], "source_type": h["source_type"]},
                          row.get("english", ""), english_for(g, gl))
                # only clear cases: a short search-list title missing a category word may still fit
                if not v["relevant"] and not re.match(r"(کمتر از|هیچ|واژه‌ی اصلی)", v["reason"]):
                    drop.append({**h, "reason": v["reason"]})
                    continue
            keep.append(h)
        return row, keep, drop

    with Appender(out, HITS_COLS) as w, Appender(d / "02_no_hits.csv", ["taxonomy_id", "query", "route"]) as miss, \
            Appender(d / "02_prefiltered.csv", HITS_COLS + ["reason"]) as pre, ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(run_row, r) for r in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            if _progress("search", i - 1, len(todo)):
                ex.shutdown(wait=False, cancel_futures=True)
                break
            row, hits, dropped = fut.result()
            for h in hits:
                w.write(h)
            for h in dropped:
                pre.write(h)
            if not hits:
                miss.write(row)
            n_hits += len(hits)
            n_pre += len(dropped)
            if i % 10 == 0:
                log.info("search %d/%d, %d hits so far", i, len(todo), n_hits)
    log.info("search done: %d new hits (%d dropped by title before fetching) -> %s", n_hits, n_pre, out.name)
    calls = SERPER_CALLS[0] - calls0
    if calls:
        from .costs import add
        web = cfg["sources"].get("web_search") or {}
        add(d, "search", calls, calls * float(web.get("usd_per_query", 0.001)), "Serper web searches", estimated=True)
    return out


def _synonyms(cfg: dict) -> dict[str, str]:
    """taxonomy_id -> 'syn1 | syn2' from the cleaned taxonomy's Synonyms column (if it has one)."""
    try:
        t = read_csv(Path(cfg["taxonomy"]["file"]))
    except Exception:
        return {}
    if "Synonyms" not in t.columns or "ID" not in t.columns:
        return {}
    return {i.strip(): s for i, s in zip(t["ID"], t["Synonyms"]) if str(s).strip()}


# ------------------------------------------------------------------ 3 crawl

class SellerInfo:
    """Seller name + phones per domain: sources.yaml first, else the homepage."""

    def __init__(self, cfg: dict, fetchers: dict[str, Fetcher]):
        self.f = fetchers
        self.known = {s["domain"]: s for s in (cfg["sources"].get("iran_sites") or [])}
        self.cache: dict[str, dict] = {}

    def get(self, domain: str, route: str) -> dict:
        if domain in self.cache:
            return self.cache[domain]
        k = self.known.get(domain, {})
        info = {"seller_name": k.get("seller_name", ""), "seller_phones": k.get("phones") or []}
        if not info["seller_phones"] and route != "foreign":
            base = (k.get("base_url") or f"https://{domain}").rstrip("/")
            res = self.f["iran"].get(base + "/")
            if res and res.get("text"):
                from bs4 import BeautifulSoup
                from .extract import lighten
                soup = BeautifulSoup(lighten(res["text"]), "lxml")
                info["seller_phones"] = extract_phones(soup.get_text(" "))
                if not info["seller_name"]:
                    m = soup.find("meta", attrs={"property": "og:site_name"})
                    info["seller_name"] = m["content"].strip() if m and m.get("content") else ""
                soup.decompose()
        info["seller_name"] = info["seller_name"] or domain
        self.cache[domain] = info
        return info


def price_fields(rec: dict, rate: dict, cfg: dict) -> dict:
    p = cfg.get("price", {})
    unit, flags = resolve_price_unit(rec, p.get("default_unit", "toman"))
    amt = rec.get("price_amount")
    usd_rate = rate["usd_rate_toman"]
    toman = usd = None
    if amt:
        if unit == "usd":
            usd, toman = amt, amt * usd_rate
        else:
            toman = amt / 10 if unit == "rial" else amt
            usd = toman / usd_rate
        if toman < p.get("min_toman", 5_000):
            flags.append("too_low")
        if toman > p.get("max_toman", 50_000_000_000):
            flags.append("too_high")
    else:
        flags.append("no_price")
    return {"price_raw": amt or "", "price_unit_detected": unit,
            "price_toman": round(toman) if toman else "", "price_usd": round(usd, 2) if usd else "",
            "price_flags": " | ".join(flags), **rate}


def step_crawl(cfg: dict, run: str, use_llm_fallback: bool = False) -> Path:
    """Fetch and read every found page. Pages are processed in parallel across sites
    (each site keeps its own pace), a page found for several categories is fetched
    and read once, and every row is written as soon as its page is done, so memory
    stays flat and an interrupted run keeps what it already has."""
    import gc
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    d = run_dir(cfg, run)
    hits = read_csv(d / FILES["hits"]).to_dict("records")
    queue = {r["taxonomy_id"]: r for r in read_csv(d / FILES["queue"]).to_dict("records")}
    out = d / FILES["crawled"]
    done = {f"{r['taxonomy_id']}|{r['url']}" for r in (read_csv(out).to_dict("records") if out.exists() else [])}
    done |= {f"{r.get('taxonomy_id', '')}|{r['url']}" for r in
             (read_csv(d / "03_failed.csv").to_dict("records") if (d / "03_failed.csv").exists() else [])}
    rate = fxmod.today_rate(cfg)
    f = _fetchers(cfg)
    sellers = SellerInfo(cfg, f)
    llm = None
    if use_llm_fallback:
        from .llm import LLM
        llm = LLM(cfg)
    list_max = int(cfg.get("search", {}).get("listing_max", 12))
    list_pages = int(cfg.get("search", {}).get("listing_pages", 3))
    workers = max(1, int(cfg.get("http", {}).get("workers", 6)))
    mult = float(cfg.get("price", {}).get("foreign_multiplier", 1) or 1)
    parse_slots = threading.Semaphore(int(cfg.get("http", {}).get("parse_slots", 2)))

    def slim(rec: dict) -> dict:
        rec.pop("page_text", None)                     # the whole page text: the big memory user
        rec["description"] = (rec.get("description") or "")[:4000]
        return rec

    def process(h: dict) -> tuple[str, object]:
        """One page -> ('rec', record) | ('list', product links) | ('fail', reason)."""
        if h.get("prefilled_json"):
            return "rec", slim(from_store_api(json.loads(h["prefilled_json"])))
        foreign = h["source_type"] == "foreign"
        res = f["foreign" if foreign else "iran"].get(h["url"])
        if not res or res.get("status") != 200:
            return "fail", f"http {res.get('status') if res else 'blocked/none'}"
        html = res.pop("text", "")
        listing = not foreign and not h.get("from_listing") and is_listing_url(h["url"])
        with parse_slots:                       # downloads run in parallel, parsing a few at a time (memory)
            rec = None if listing else extract_page(html, h["url"])
            if not foreign and not h.get("from_listing") and (listing or not rec or not rec.get("price_amount")):
                links = listing_links(html, h["url"], list_max)
                if links:            # a shop's category page: its products are crawled instead
                    nxt_page = listing_next(html, h["url"]) if int(h.get("page_depth") or 0) < list_pages - 1 else ""
                    return "list", (links, nxt_page)
        if rec is None and llm:
            rec = _llm_record(llm, html)
        if rec and rec.get("name") and (LISTING_TITLE.search(rec["name"]) or
                                        norm_text(rec["name"]) == norm_text(rec.get("seller_name") or "")):
            return "fail", "list or brand page, not a product"     # 'محصولات برند اشتیل', a page titled with the shop's name
        return ("rec", slim(rec)) if rec and rec.get("name") else ("fail", "no product data")

    def ukey(h: dict) -> str:
        return f"{h['url']}|{'L' if h.get('from_listing') else ''}|{'P' if h.get('prefilled_json') else ''}"

    memo: dict[str, tuple[str, object]] = {}
    stats = {"ok": 0, "lists": 0, "pages": 0}

    def write_rows(hs: list[dict], res: tuple[str, object], w, fail) -> list[dict]:
        """Rows for every category that found this page; returns new listing hits."""
        kind, val = res
        nxt = []
        for h in hs:
            key = f"{h['taxonomy_id']}|{h['url']}"
            if key in done:
                continue
            done.add(key)
            if kind == "list":
                stats["lists"] += 1
                links, nxt_page = val
                nxt += [{**h, "url": canon_url(u), "title": "", "found_by": "listing", "prefilled_json": "",
                         "from_listing": "1"} for u in links if f"{h['taxonomy_id']}|{canon_url(u)}" not in done]
                if nxt_page and f"{h['taxonomy_id']}|{canon_url(nxt_page)}" not in done:   # page 2, 3 of the list
                    nxt.append({**h, "url": canon_url(nxt_page), "title": "", "found_by": "listing_page",
                                "prefilled_json": "", "page_depth": str(int(h.get("page_depth") or 0) + 1)})
                continue
            if kind == "fail":
                fail.write({"taxonomy_id": h["taxonomy_id"], "url": h["url"], "reason": val})
                continue
            rec = val
            dom = domain_of(h["url"])
            s = sellers.get(dom, h["route"])
            q = queue.get(h["taxonomy_id"], {})
            row = {
                "taxonomy_id": h["taxonomy_id"], "breadcrumb": q.get("breadcrumb", ""),
                "query": h["query"], "route": h["route"],
                "name": rec["name"], "brand": rec["brand"], "model": rec["model"],
                "specs_json": json.dumps(rec["specs"], ensure_ascii=False) if rec["specs"] else "",
                "short_description": rec["short_description"],
                "sale_unit": rec["sale_unit"], "in_stock": rec["in_stock"],
                "seller_name": rec.get("seller_name") or s["seller_name"],
                "seller_phones": " | ".join(rec["seller_phones"] or s["seller_phones"]),
                "claims_representative": rec["claims_representative"],
                "source_domain": dom, "source_type": h["source_type"], "url": h["url"],
                "image_urls": json.dumps(rec["image_urls"][:15], ensure_ascii=False),
                "extracted_by": rec["extracted_by"], "supplier_badges": rec.get("supplier_badges", ""),
                "description": rec.get("description", ""), "files": " | ".join(rec.get("files") or []),
                "crawled_at": dt.datetime.now().isoformat(timespec="seconds"),
                **price_fields(rec, rate, cfg),
            }
            if h["source_type"] == "foreign" and mult != 1 and row["price_toman"] != "":
                # Made-in-China prices are ex-factory in China: shipping, customs and margin on top
                row["price_toman"] = round(row["price_toman"] * mult)
                row["price_usd"] = round(row["price_usd"] * mult, 2)
                row["price_flags"] = " | ".join(x for x in (row["price_flags"], f"foreign_x{mult:g}") if x)
            w.write(row)
            stats["ok"] += 1
        return nxt

    pending = [h for h in hits if f"{h['taxonomy_id']}|{h['url']}" not in done]
    total, stopped = len(pending), False
    with Appender(out, CRAWLED_COLS) as w, Appender(d / "03_failed.csv", ["taxonomy_id", "url", "reason"]) as fail:
        while pending and not stopped:
            by_key: dict[str, list[dict]] = {}
            for h in pending:
                by_key.setdefault(ukey(h), []).append(h)
            nxt: list[dict] = []
            for k in [k for k in by_key if k in memo]:          # pages already read in an earlier round
                nxt += write_rows(by_key.pop(k), memo[k], w, fail)
            todo = list(by_key)
            with ThreadPoolExecutor(workers) as ex:
                running, it = {}, iter(todo)
                for k in it:                                    # keep only a few pages in flight at once
                    running[ex.submit(process, by_key[k][0])] = k
                    if len(running) >= workers * 2:
                        break
                while running:
                    fin, _ = wait(running, return_when=FIRST_COMPLETED)
                    for fut in fin:
                        k = running.pop(fut)
                        try:
                            res = fut.result()
                        except Exception as e:                  # one bad page must not stop the run
                            res = ("fail", f"error {type(e).__name__}")
                        memo[k] = res if res[0] != "rec" else ("rec", res[1])
                        nxt += write_rows(by_key[k], res, w, fail)
                        stats["pages"] += 1
                        if stats["pages"] % 50 == 0:
                            gc.collect()
                            import resource
                            log.info("crawl %d pages read, %d products (memory peak %d MB)", stats["pages"], stats["ok"],
                                     resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)
                        if _progress("crawl", stats["pages"], max(total, stats["pages"])):
                            stopped = True
                        nk = next(it, None) if not stopped else None
                        if nk is not None:
                            running[ex.submit(process, by_key[nk][0])] = nk
                    if stopped:
                        for fut in running:
                            fut.cancel()
                        break
            total += len(nxt)
            pending = nxt
    log.info("crawl done: %d products from %d unique pages (%d shop list pages opened) -> %s",
             stats["ok"], len(memo), stats["lists"], out.name)
    if not stopped and cfg.get("pool", {}).get("enabled", True):
        # pages earlier runs read (other models, brands, types) that fit this run's categories,
        # then this run's pages go to the pool for the runs after it
        from .pool import add_to_pool, from_pool, pool_path
        if not pool_path(cfg).exists():                  # first time: the pool starts with every earlier run
            for other in sorted((cfg["_data_dir"] / "runs").glob("*/03_crawled.csv")):
                if other.parent.name != run:
                    add_to_pool(cfg, read_csv(other))
        mine = read_csv(out) if out.exists() else pd.DataFrame(columns=CRAWLED_COLS)
        have = set(mine["taxonomy_id"] + "|" + mine["url"]) if len(mine) else set()
        extra = from_pool(cfg, list(queue.values()), have)
        if extra:
            with Appender(out, CRAWLED_COLS) as w:
                for r in extra:
                    w.write(r)
            log.info("crawl pool: %d page-category row(s) from earlier runs (%d pages)",
                     len(extra), len({r["url"] for r in extra}))
        n = add_to_pool(cfg, mine)
        log.info("crawl pool: %d page(s) of this run kept for later runs", n)
    return out


def _llm_record(llm, html: str) -> dict | None:
    from bs4 import BeautifulSoup
    from .extract import empty_record, finalize
    from .llm import extract as llm_extract
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    data = llm_extract(llm, text)
    if not data or not data.get("name"):
        return None
    rec = empty_record()
    rec.update({"name": data.get("name", ""), "brand": data.get("brand", ""), "model": data.get("model", ""),
                "specs": data.get("specs") or {}, "price_amount": data.get("price"),
                "price_unit_detected": data.get("price_unit") or "", "extracted_by": "llm",
                "page_text": text})
    finalize(rec, text)
    return rec


# ------------------------------------------------------------------ 4 filter

def step_filter(cfg: dict, run: str) -> Path:
    """AI or rule-based relevance check, per config llm.provider."""
    from .llm import LLM, relevance_batch
    from .rules import judge
    d = run_dir(cfg, run)
    df = read_csv(d / FILES["crawled"])
    try:                                    # brand countries the shops' pages state, for 'ایتالیایی' etc.
        from .origins import learn
        learn(cfg, df)
    except Exception as e:                  # noqa: BLE001 - the filter works without it
        log.warning("brand countries not learned: %s", e)
    out, rej = d / FILES["filtered"], d / FILES["rejected"]
    done = set()
    for f_ in (out, rej):
        if f_.exists():
            x = read_csv(f_)
            if len(x):
                done |= set(x["taxonomy_id"] + "|" + x["url"])
    todo = df[~(df["taxonomy_id"] + "|" + df["url"]).isin(done)] if len(df) else df
    lc = cfg.get("llm", {})
    use_rules = lc.get("provider", "rules") == "rules"
    min_conf = float(lc.get("min_confidence", 0.6)) if not use_rules else float(lc.get("rules_min_confidence", 0.5))
    batch = int(lc.get("batch_size", 15))
    llm = None if use_rules else LLM(cfg)
    log.info("filter: %s", "rule-based (free)" if use_rules else f"{llm.provider} / {llm.model}")
    kept = dropped = 0
    total = len(todo)
    from .routing_state import english_for, group_of, load_glossary
    gl = load_glossary()
    qrows = read_csv(d / FILES["queue"]).to_dict("records") if (d / FILES["queue"]).exists() else []
    en_of = {}
    for q in qrows:                      # English term of each category, for foreign product names
        g = q.get("group") or group_of(q["breadcrumb"])
        en_of[q["taxonomy_id"]] = (q.get("english") or english_for(g, gl), english_for(g, gl))
    with Appender(out, FILTER_COLS) as w_ok, Appender(rej, FILTER_COLS) as w_no:
        for tid, part in todo.groupby("taxonomy_id", sort=False):
            if _progress("filter", kept + dropped, total):
                break
            rows = part.to_dict("records")
            crumb = rows[0]["breadcrumb"]
            leaf = crumb.split(">")[-1].strip() if crumb else rows[0].get("query", "")
            for i in range(0, len(rows), batch):
                chunk = rows[i:i + batch]
                if use_rules:
                    en, gen = en_of.get(tid, ("", ""))
                    verdicts = [judge(crumb, leaf, r, en, gen) for r in chunk]
                else:
                    try:
                        verdicts = relevance_batch(llm, crumb, chunk)
                    except Exception as e:
                        log.warning("filter failed for %s: %s (will retry next run)", tid, e)
                        continue
                for r, v in zip(chunk, verdicts):
                    car = car_part(r.get("name", ""), crumb)
                    if car:
                        v = {**v, "relevant": False, "reason": f"لوازم یدکی خودرو ({car})؛ به کار سایت نمی‌آید"}
                    r.update({"relevant": v.get("relevant"), "relevance_confidence": v.get("confidence"),
                              "relevance_reason": v.get("reason", "")})
                    if not r.get("brand") and v.get("brand"):
                        r["brand"] = v["brand"]
                    if not r.get("model") and v.get("model"):
                        r["model"] = v["model"]
                    good = v.get("relevant") is True and float(v.get("confidence") or 0) >= min_conf
                    (w_ok if good else w_no).write(r)
                    kept += good
                    dropped += not good
    log.info("filter: kept %d, rejected %d%s", kept, dropped, "" if use_rules else f"; usage {llm.usage}")
    return out


# ------------------------------------------------------------------ 5 dedupe

def on_site_from_other_runs(cfg: dict, run: str) -> set[str]:
    """Source pages an EARLIER batch has already put on the site (batch-2 before batch-10). A later
    batch never takes a product away from an earlier one, so re-running batch-1 changes nothing."""
    def order(name: str):
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]
    out: set[str] = set()
    for p in (cfg["_data_dir"] / "runs").glob("*/07_published.csv"):
        if order(p.parent.name) >= order(run):
            continue
        try:
            x = read_csv(p)
        except Exception:                                # noqa: BLE001
            continue
        if "source_url" in x and "product_id" in x:
            out |= set(x.loc[x["product_id"].astype(str) != "", "source_url"])
    return out


def models_on_site_from_other_runs(cfg: dict, run: str) -> dict[str, str]:
    """{kind+model key: product id} of what EARLIER batches put on the site, so a later batch does not
    add the same model again from another shop (129 such groups had to be merged by hand)."""
    from .dedupe import _cross_key

    def order(name: str):
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]
    out: dict[str, str] = {}
    for p in (cfg["_data_dir"] / "runs").glob("*/07_published.csv"):
        if order(p.parent.name) >= order(run):
            continue
        try:
            x = read_csv(p)
        except Exception:                                # noqa: BLE001
            continue
        for r in x.to_dict("records"):
            if str(r.get("product_id") or "").strip() and r.get("action") in ("created", "updated"):
                k = _cross_key({"name": r.get("name", ""), "model": "", "brand": r.get("brand", "")})
                if k:
                    out.setdefault(k, str(r["product_id"]))
    return out


def step_dedupe(cfg: dict, run: str, site_export: str | None, skip_filter: bool = False) -> list[Path]:
    d = run_dir(cfg, run)
    src = d / (FILES["crawled"] if skip_filter else FILES["filtered"])
    df = read_csv(src)
    dd = cfg.get("dedupe", {})
    crawled = read_csv(d / FILES["crawled"])
    from .pricing import fix_units
    crawled = fix_units(crawled)                         # rial read as toman (or the reverse), from other shops
    df = fix_units(df, crawled)
    df = cross_assign(cfg, d, df)
    if dd.get("harvest", True):                          # second chance across the whole site tree
        from .harvest import harvest
        df = harvest(cfg, df, crawled, progress=lambda a, b: _progress("dedupe", a, b))
    if dd.get("place_up", True):                         # what still fits nowhere: the nearest category above
        from .harvest import place_up
        df = place_up(cfg, df, crawled)
    from .catmake import apply_placements                # pages a person placed in a new category
    df = apply_placements(cfg, df, crawled)
    try:                                                 # what fits nowhere: the waiting list for new categories
        from .nocat import write as no_category
        pub = d / "07_published.csv"
        on_site = set(read_csv(pub)["source_url"]) if pub.exists() else set()
        no_category(cfg, d, run, df, crawled, on_site)
    except Exception as e:                               # noqa: BLE001
        log.warning("no-category list not written: %s", e)
    df = one_category_per_product(df)
    merged = merge_crawl(df, float(dd.get("crawl_name_threshold", 0.9)))
    merged = drop_price_outliers(merged, float(cfg.get("price", {}).get("outlier_factor", 6)))
    same = models_on_site_from_other_runs(cfg, run)
    if same and len(merged):                            # the same model, from another shop, already on the site
        from .dedupe import _cross_key
        keys = [_cross_key(r) for r in merged.to_dict("records")]
        again = pd.Series([bool(k) and k in same for k in keys], index=merged.index)
        if again.any():
            x = merged[again].copy()
            x["on_site_as"] = [same[k] for k, a in zip(keys, again) if a]
            write_csv(x, d / "05_same_model_on_site.csv")
            log.info("dedupe: %d product(s) are a model an earlier batch already put on the site, left out",
                     int(again.sum()))
            merged = merged[~again]
    elsewhere = on_site_from_other_runs(cfg, run)
    own = d / "07_published.csv"                         # what this run itself already put on the site stays
    if elsewhere and own.exists():                       # (a re-run must not take it away and park it)
        x = read_csv(own)
        elsewhere -= set(x.loc[x["product_id"].astype(str) != "", "source_url"])
    if elsewhere and len(merged):                       # already on the site from an earlier batch: no second
        again = merged["url"].isin(elsewhere)           # photo work, description or slot (35 in batch-3)
        if again.any():
            write_csv(merged[again], d / "05_on_site_already.csv")
            log.info("dedupe: %d product(s) already on the site from another batch, left out", int(again.sum()))
            merged = merged[~again]
    from .brandfix import clean_brand
    if len(merged):                                      # a crawl that found nothing has no columns
        merged["brand"] = [clean_brand(b, n) for b, n in zip(merged.get("brand", ""), merged["name"])]
    need = merged.iloc[0:0]
    if cfg.get("price", {}).get("estimate_missing", True) and len(merged):
        from .pricing import estimate
        merged, need = estimate(cfg, merged, crawled, _fetchers(cfg)["iran"])
    (d / "05_needs_price.csv").unlink(missing_ok=True)   # no separate list: counted in 05_low_categories.csv
    from .ranking import photo_bad_rates, select_top
    sel = cfg.get("select", {})
    n_all = len(merged)
    if len(merged) and sel.get("prefer_clean_photos", True):
        rates = photo_bad_rates(cfg["_data_dir"])
        hosts = merged["url"].astype(str).str.extract(r"https?://(?:www\.)?([^/]+)", expand=False).str.lower()
        merged["photo_bad_rate"] = hosts.map(rates).fillna(0)
    merged, dropped = select_top(merged, int(sel.get("max_per_category", 24)), int(sel.get("max_per_seller", 3)),
                                 int(sel.get("max_foreign_per_category", 0)), str(sel.get("foreign_skip_words") or ""),
                                 float(sel.get("foreign_max_ratio", 0)), _brand_names(cfg, merged))
    write_csv(dropped, d / "05_not_selected.csv")
    log.info("select: best %s per category -> kept %d of %d (rest in 05_not_selected.csv)",
             sel.get("max_per_category", 24), len(merged), n_all)
    low_categories(cfg, d, merged, need)
    site = None
    path = site_export or cfg.get("site_catalog", {}).get("file")
    if path:
        p = Path(path) if Path(path).is_absolute() else ROOT / path
        site = load_site_catalog(p, cfg["site_catalog"]["columns"])
    else:
        log.warning("no site export given: every product counts as new (use --site-export)")
    new, upd, rev = split_against_site(merged, site, float(dd.get("same_threshold", 0.92)),
                                       float(dd.get("review_threshold", 0.8)))
    from .brandfix import fill_brands
    new, upd, rev = (fill_brands(add_persian(cfg, x)) for x in (new, upd, rev))   # after matching: site names are compared in the source wording
    paths = [write_csv(new, d / FILES["new"]), write_csv(upd, d / FILES["updates"]),
             write_csv(rev, d / FILES["review"])]
    log.info("dedupe: %d rows -> %d products; new %d, updates %d, review %d",
             len(df), len(merged), len(new), len(upd), len(rev))
    return paths


def category_kind(cfg: dict, breadcrumb: str, target: int) -> tuple[str, int]:
    """What a category can hold: one model ('اره عمود بر بوش gst 75 be': the same model from twenty
    shops is merged into one product, so 1 is full), one brand, or a general kind (24)."""
    from .routing import leaf_brand
    from .rules import model_tokens
    leaf = breadcrumb.split(">")[-1].strip()
    from .normalize import fa_digits_to_en
    if model_tokens(breadcrumb, leaf):
        lf = fa_digits_to_en(leaf).lower()
        unit = r"\s*(وات|ولت|سانت|سانتی|کیلو|کیلویی|نفره|اینچ|میلی|متر|لیتر|تن|حالته|کاره)"
        model = re.search(r"[a-z]", lf) or any(not re.match(unit, lf[m.end():])
                                                for m in re.finditer(r"\d{3,}", lf))
        if model:                                        # 'gst 75 be', 'hr2630t', 'رونیکس 4111'
            return "مدل", int(cfg.get("select", {}).get("model_target", 1))
        return "اندازه/توان", int(cfg.get("select", {}).get("size_target", 6))   # '1200 وات', '30 سانتی'
    if leaf_brand(leaf):
        return "برند", target
    return "عمومی", target


def low_categories(cfg: dict, d: Path, kept: pd.DataFrame, need: pd.DataFrame) -> Path:
    """Categories below the target count: the list for finding and inviting suppliers."""
    target = int(cfg.get("select", {}).get("max_per_category", 24))
    q = read_csv(d / FILES["queue"])
    ids = []
    for t, extra in zip(kept.get("taxonomy_id", []), kept.get("extra_taxonomy_ids", [""] * len(kept))):
        ids += [t] + [x for x in str(extra or "").split(" | ") if x]      # extra categories count too
    n = pd.Series(ids).value_counts() if ids else pd.Series(dtype=int)
    m = need["taxonomy_id"].value_counts() if len(need) else pd.Series(dtype=int)
    def counts(name, col="taxonomy_id"):
        p_ = d / name
        x = read_csv(p_) if p_.exists() else pd.DataFrame()
        return (x[col].value_counts().to_dict() if len(x) and col in x.columns else {}), x
    hits, _ = counts(FILES["hits"])
    crawled, _ = counts(FILES["crawled"])
    failed, _ = counts("03_failed.csv")
    _, rej = counts(FILES["rejected"])
    top_rej = {}
    if len(rej):
        for tid, g in rej.groupby("taxonomy_id"):
            vc = g["relevance_reason"].str.split(":").str[0].value_counts()
            top_rej[tid] = f"{vc.index[0]} ({vc.iloc[0]})"
    rows = []
    for r in q.to_dict("records"):
        t = r["taxonomy_id"]
        k, w = int(n.get(t, 0)), int(m.get(t, 0))
        kind, goal = category_kind(cfg, r["breadcrumb"], target)
        if k < goal:
            h_, c_, f_ = int(hits.get(t, 0)), int(crawled.get(t, 0)), int(failed.get(t, 0))
            if h_ == 0:
                why = "جستجو هیچ صفحه‌ای پیدا نکرد"
            elif c_ == 0 and f_:
                why = f"{f_} صفحه پیدا شد ولی باز نشد (سایت کند یا خطا)"
            elif k == 0 and w:
                why = "محصول پیدا شد ولی قیمت ندارد"
            elif k == 0:
                why = "پیدا شد ولی همه رد شدند: " + top_rej.get(t, "")
            else:
                why = "بازار همین‌قدر محصول داشت" + (f"؛ بیشترین رد: {top_rej[t]}" if t in top_rej else "")
            rows.append({"taxonomy_id": t, "breadcrumb": r["breadcrumb"], "route": r["route"],
                         "kind": kind, "target": goal, "products": k, "without_price": w, "missing": goal - k,
                         "status": "خالی" if k == 0 and w == 0 else "فقط بی‌قیمت" if k == 0 else "کم‌محصول",
                         "found_links": h_, "crawled": c_, "failed": f_, "why": why})
    out = pd.DataFrame(rows, columns=["taxonomy_id", "breadcrumb", "route", "kind", "target", "products",
                                      "without_price", "missing",
                                      "status", "found_links", "crawled", "failed", "why"])
    out = out.sort_values(["products", "breadcrumb"]) if len(out) else out
    log.info("categories below their target (model 1, brand/general %d): %d of %d (%d empty) -> 05_low_categories.csv",
             target, len(out), len(q),
             int((out["products"] == 0).sum()) if len(out) else 0)
    return write_csv(out, d / "05_low_categories.csv")


def cross_assign(cfg: dict, d: Path, df: pd.DataFrame) -> pd.DataFrame:
    """Every product is also checked against the other categories of its group: a page
    found while searching «موتور آسانسور بهران گیرلس» that is a «بهران 8 نفره» joins that
    category too. Stricter than the filter (most of the category's words must be in the name)."""
    if df.empty:
        return df
    from .routing_state import english_for, group_of, load_glossary
    from .rules import judge
    min_conf = float(cfg.get("dedupe", {}).get("cross_assign_confidence", 0.9))
    q = read_csv(d / FILES["queue"]).to_dict("records")
    gl = load_glossary()
    by_group: dict[str, list[dict]] = {}
    for r in q:
        by_group.setdefault(group_of(r["breadcrumb"]), []).append(r)
    have = set(df["taxonomy_id"] + "|" + df["url"])
    add = []
    for r in df.drop_duplicates("url").to_dict("records"):
        g = group_of(r["breadcrumb"])
        for c in by_group.get(g, []):
            if f"{c['taxonomy_id']}|{r['url']}" in have:
                continue
            leaf = c["breadcrumb"].split(">")[-1].strip()
            v = judge(c["breadcrumb"], leaf, r, c.get("english", ""), english_for(g, gl))
            if v["relevant"] and float(v["confidence"]) >= min_conf:
                add.append({**r, "taxonomy_id": c["taxonomy_id"], "breadcrumb": c["breadcrumb"], "query": c["query"],
                            "relevance_confidence": str(v["confidence"]),
                            "relevance_reason": "از دسته‌ی هم‌خانواده: " + v["reason"]})
                have.add(f"{c['taxonomy_id']}|{r['url']}")
    if add:
        log.info("sibling categories: %d more category matches for products found under a sibling", len(add))
        df = pd.concat([df, pd.DataFrame(add)], ignore_index=True).fillna("")
    return df


def step_topup(cfg: dict, run: str, round_: int = 1) -> Path | None:
    from .topup import step_topup as _t
    return _t(cfg, run, round_=round_)


def one_category_per_product(df: pd.DataFrame) -> pd.DataFrame:
    """A page found for several categories becomes one product: its main category is the
    best fit (highest relevance; ties go to the more specific category) and every other
    category it passed the filter for is kept in extra_taxonomy_ids / extra_breadcrumbs,
    so WooCommerce lists it in all of them without a duplicate product."""
    if df.empty or "url" not in df.columns:
        return df
    df = df.copy()
    df["_c"] = pd.to_numeric(df.get("relevance_confidence", 0), errors="coerce").fillna(0)
    df["_l"] = df["breadcrumb"].str.len()
    df = df.sort_values(["_c", "_l"], ascending=False, kind="stable")
    extra_ids, extra_bc = {}, {}
    for url, g in df.groupby("url", sort=False):
        extra_ids[url] = " | ".join(dict.fromkeys(g["taxonomy_id"].iloc[1:]))
        extra_bc[url] = " | ".join(dict.fromkeys(g["breadcrumb"].iloc[1:]))
    best = df.drop_duplicates("url").copy()
    best["extra_taxonomy_ids"] = best["url"].map(extra_ids).fillna("")
    best["extra_breadcrumbs"] = best["url"].map(extra_bc).fillna("")
    if len(best) < len(df):
        log.info("same page in several categories: %d products from %d rows (listed in all fitting categories)",
                 len(best), len(df))
    return best.drop(columns=["_c", "_l"]).sort_index()


def drop_price_outliers(df: pd.DataFrame, factor: float = 6) -> pd.DataFrame:
    """A price far from the others in its category (e.g. a 'US$1' bait price) is flagged; the
    export holds it back until a person confirms or corrects it on the review page."""
    if df.empty or not factor:
        return df
    df = df.copy()
    p = pd.to_numeric(df["price_toman"], errors="coerce")
    n = 0
    for _, idx in df.groupby(["taxonomy_id", "source_type"]).groups.items():
        vals = p.loc[idx].dropna()
        if len(vals) < 4:
            continue
        med = vals.median()
        bad = [i for i in vals.index if vals[i] < med / factor or vals[i] > med * factor]
        for i in bad:
            # kept, not emptied: the product stays (nothing good is thrown away) and its price is
            # held back for the review page at export (price_sanity), where a person decides
            df.at[i, "price_flags"] = " | ".join(x for x in (str(df.at[i, "price_flags"]), "price_outlier") if x)
            if df.at[i, "source_type"] == "foreign":      # a supplier's 'US$1' bait price: no price at all
                df.at[i, "price_toman"] = ""
                df.at[i, "price_usd"] = ""
        n += len(bad)
    if n:
        log.info("price outliers flagged for review: %d (far from their category's median)", n)
    return df


def add_persian(cfg: dict, df: pd.DataFrame) -> pd.DataFrame:
    """Persian name and specs for English (foreign) products; Persian rows are left as they are."""
    from collections import Counter
    from .fa_name import is_foreign, persian_name, save_missing, translate_specs
    from .normalize import norm_text
    from .routing import leaf_brand
    if df.empty:
        return df
    df = df.copy()
    missing: Counter = Counter()
    names_fa, specs_fa, src = [], [], []
    used: Counter = Counter()
    for r in df.to_dict("records"):
        if not is_foreign(r.get("name", "")):
            names_fa.append(r.get("name", ""))
            specs_fa.append(r.get("specs_json", ""))
            src.append("")
            continue
        specs = json.loads(r.get("specs_json") or "{}")
        leaf = (r.get("breadcrumb") or r.get("query") or "").split(">")[-1].strip()
        b = leaf_brand(leaf)
        if b:                                              # a Chinese product never carries a brand's name
            leaf = " ".join(w for w in leaf.split() if norm_text(w) not in norm_text(b[0]).split()).strip() or leaf
        n = persian_name(leaf, r.get("model", ""), specs, r.get("name", ""))
        used[(r.get("taxonomy_id"), n)] += 1
        if used[(r.get("taxonomy_id"), n)] > 1:            # same name twice in one category
            n = f"{n} - طرح {used[(r.get('taxonomy_id'), n)]}"
        tr = translate_specs(specs, missing)
        if "made-in-china" in (r.get("source_domain") or ""):
            tr["کشور سازنده"] = "چین"
        names_fa.append(n)
        specs_fa.append(json.dumps(tr, ensure_ascii=False))
        src.append(r.get("name", ""))
    df["name_fa"], df["specs_fa_json"], df["source_name"] = names_fa, specs_fa, src
    df["name"] = df["name_fa"]                    # the Persian name is the name; English stays in source_name
    if missing:
        save_missing(cfg["_data_dir"] / "untranslated.csv", missing)
        log.info("persian names: %d foreign products; %d spec texts not in the dictionary yet (data/untranslated.csv)",
                 sum(1 for x in src if x), len(missing))
    return df


# ------------------------------------------------------------------ 6 export

def _round_price(x, step: int) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return str(int(round(v / step) * step)) if step else str(int(v))


def _spec_html(r: dict) -> str:
    """Specs table built only from extracted facts, never the source's text."""
    from .describe import clean_specs
    facts = {"برند": r.get("brand"), "مدل": r.get("model"), **clean_specs(r)}
    rows = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in facts.items() if v)
    return f"<h3>مشخصات فنی</h3><table>{rows}</table>" if rows else ""


def caravan_sku(url: str) -> str:
    """Stable SKU from the source page: the same product always gets the same SKU, so a second
    import or publish updates it instead of creating a duplicate."""
    import hashlib
    return "CRV-" + hashlib.sha1(str(url).strip().encode("utf-8")).hexdigest()[:10].upper()


def _send_rate(cfg: dict) -> dict:
    """Today's rate for sending; a rate source that fails stops the export (never a guessed price).
    Only when no rate is set up at all (tests, a fresh install) the crawl day's prices are sent."""
    from .fx import today_rate
    fx = cfg.get("fx", {})
    if fx.get("provider", "manual") == "manual" and not fx.get("manual_rate_toman"):
        log.warning("no dollar rate set: prices go out at the crawl day's rate")
        return {"usd_rate_toman": None, "usd_rate_date": ""}
    return today_rate(cfg)


def _usd_price(r: dict, rate: float | None = None) -> str:
    """The site stores prices in dollars and multiplies them by its own rate. A toman price is turned
    into dollars with the rate of the day it is SENT (`rate`, the site's rate in settings), not the
    rate of the day it was crawled: batches crawled at 270,000 went out 26% cheap on a site that
    multiplies by 200,000. Foreign prices (in dollars from the start) are sent as they are."""
    foreign = str(r.get("source_type", "")) == "foreign" or str(r.get("price_unit_detected", "")) == "usd"
    v = float("nan")
    if rate and not foreign:
        try:
            v = float(r.get("price_toman")) / float(rate)
        except (TypeError, ValueError, ZeroDivisionError):
            v = float("nan")
    if v != v:
        try:
            v = float(r.get("price_usd") or "nan")
        except (TypeError, ValueError):
            v = float("nan")
    if v != v:                                            # NaN: no dollar price, derive it
        try:
            v = float(r.get("price_toman")) / float(r.get("usd_rate_toman"))
        except (TypeError, ValueError, ZeroDivisionError):
            return ""
    return f"{v:.2f}".rstrip("0").rstrip(".") if v > 0 else ""


def price_peers(cfg: dict, d: Path) -> list[dict]:
    """Priced pages of Iranian shops (this run's crawl and the crawl pool): other shops' prices for
    the same model, to settle a price that looks wrong without a person."""
    parts = []
    for p in (d / FILES["crawled"], cfg["_data_dir"] / "crawl_pool.csv"):
        if p.exists():
            x = read_csv(p)
            parts.append(x[["url", "name", "price_toman", "source_type", "source_domain"]])
    if not parts:
        return []
    mem_p = cfg["_data_dir"] / "price_memory.csv"            # prices earlier checks confirmed or found
    if mem_p.exists():
        m = read_csv(mem_p)
        if len(m):
            parts.append(pd.DataFrame({"url": "mem:" + m["dom"] + ":" + m["key"], "name": m["name"],
                                       "price_toman": m["price"], "source_type": "iran", "source_domain": m["dom"]}))
    allp = pd.concat(parts, ignore_index=True).drop_duplicates("url")
    allp = allp[(allp["price_toman"] != "") & (allp["source_type"] != "foreign")]
    out = []
    for r in allp.to_dict("records"):
        try:
            v = float(r["price_toman"])
        except ValueError:
            continue
        if v > 0:
            out.append({"url": r["url"], "dom": r.get("source_domain", ""), "price": v, "codes": _codes(r["name"]),
                        "name": r["name"],
                        "head": (norm_text(r["name"]).split() or [""])[0]})
    return out


def _codes(name: str) -> set[str]:
    """Model codes of a name ('HR2630T' -> 'hr2630t', 'مدل 5403' -> '5403'): Latin/digit runs
    with a digit, 3+ characters, sizes like '650' followed by وات left out."""
    from .rules import _flat
    toks = [t for t in _flat(name).split("|") if t]
    units = re.compile(r"(وات|ولت|سانت|میلی|متر|کیلو|اینچ|نفره|آمپر|لیتر)")
    nm = norm_text(name)
    out = set()
    for t in toks:
        if len(t) >= 3 and re.search(r"\d", t) and not re.search(rf"{re.escape(t)}\s*{units.pattern}", nm.lower()):
            # an English word glued after the code is not part of it:
            # '5425 Sliding Miter' -> '5425', 'GHG 16-50 Professional' -> 'ghg1650'
            t = re.sub(r"(?<=\d)[a-z]{5,}$", "", t)
            out.add(t)
    return out


def confirm_price(r: dict, v: float, peers: list[dict], tol: float = 0.35) -> tuple[float, str] | None:
    """Other shops' prices for the same model (a shared model code, the same kind of thing):
    (price, note) when they settle it — the price itself, or a 10x slip of it — else None."""
    codes = _codes(str(r.get("name") or ""))
    if not codes or not peers:
        return None
    head = (norm_text(str(r.get("name") or "")).split() or [""])[0]
    from statistics import median
    from .rules import code_in
    others = {}
    for p in peers:
        if p["url"] == r.get("url") or p["head"] != head:
            continue
        if any(code_in(c, k) or code_in(k, c) for c in codes for k in p["codes"] if min(len(c), len(k)) >= 3):
            others.setdefault(p["dom"] or p["url"], []).append(p["price"])
    if not others:
        return None
    ref = median([median(v_) for v_ in others.values()])
    n = len(others)
    for cand, how in ((v, "همین قیمت"), (v / 10, "قیمت ÷۱۰"), (v * 10, "قیمت ×۱۰")):
        if abs(cand / ref - 1) <= tol:
            return cand, f"تأیید خودکار با {n} فروشگاه دیگر ({how}؛ میانه‌ی آن‌ها {ref:,.0f} تومان)"
    return None


PRICE_PART_LEADS = ("دیمر", "کلید", "آرمیچر", "ارمیچر", "محافظ تیغ", "پوسته", "دنده", "ذغال", "زغال",
                    "قطعه", "یدکی", "درب", "کاربراتور", "سیلندر", "اگزوز", "کوئل", "لنت")


def is_part(name: str) -> bool:
    """'کلید بلوور رونیکس 1205' is a part of a tool: its price is not held against the whole tools of
    the category it sits in (the dimmer was flagged next to 6-million blowers)."""
    n = " " + " ".join(str(name or "").split()) + " "
    return any(n.startswith(f" {w} ") for w in PRICE_PART_LEADS)


def price_history(cfg: dict, d: Path, low: float = 150_000, high: float = 150_000_000) -> dict[str, list[float]]:
    """Toman prices of the other runs' new products by category ('leaf:<breadcrumb>' and
    'parent:<breadcrumb minus its leaf>'): the reference for a category this run has too few
    products of (a Ronix router at 407,000 toman went out because its batch had no other routers)."""
    out: dict[str, list[float]] = {}
    runs = Path(cfg["_data_dir"]) / "runs"
    if not runs.exists():
        return out
    for rd in runs.iterdir():
        f = rd / FILES["new"]
        if rd == d or not f.exists() or rd.name.startswith("fix-"):
            continue
        try:
            x = read_csv(f)
        except Exception:                                    # noqa: BLE001 - one bad file is skipped
            continue
        if "price_toman" not in x or "breadcrumb" not in x:
            continue
        for p, bc in zip(x["price_toman"], x["breadcrumb"]):
            try:
                v = float(p)
            except (TypeError, ValueError):
                continue
            if low <= v <= high and bc:
                out.setdefault("leaf:" + bc, []).append(v)
                out.setdefault("parent:" + bc.rsplit(" > ", 1)[0], []).append(v)
    return out


CAR_WORDS = ("سوزوکی", "ویتارا", "پژو", "پراید", "سمند", "تیبا", "ساینا", "کوییک", "پیکان", "رانا", "نیسان",
             "تویوتا", "هیوندای", "هیوندا", "مزدا", "رنو", "ام وی ام", "لیفان", "هایما", "صندوق عقب",
             "درب موتور", "کاپوت", "سپر عقب", "سپر جلو")       # not 'خودرو': car jacks and pumps are tools


def car_part(name: str, crumb: str = "") -> str:
    """The car word in a product name ('جک گازی صندوق عقب ویتارا'), or ''. An industrial shop does not
    sell car spare parts (29 Oct: four car gas springs came with the search 'جک گازی'). Only when the
    category is not about cars itself."""
    if "خودرو" in str(crumb) or "ماشین" in str(crumb).split(">")[-1]:
        return ""
    n = " " + norm_text(str(name or "")) + " "
    for w in CAR_WORDS:
        if f" {norm_text(w)} " in n:
            return w
    return ""


METER_NOTE = "<p><strong>قیمت درج‌شده برای هر متر است.</strong></p>"


def per_meter(r: dict) -> bool:
    """The price is for one metre (hoses, cables): the page or the name says so."""
    return str(r.get("sale_unit") or "").strip() == "متر"


def price_sanity(recs: list[dict], factor: float = 8.0, low: float = 150_000, high: float = 150_000_000,
                 peers: list[dict] | None = None, history: dict[str, list[float]] | None = None) -> list[dict]:
    """Prices that look 10x off (rial read as toman, or the reverse) are held back, never changed:
    far from the median of the other products of the category in this run (else of its parent),
    or outside an absolute range. The note says what /10 or x10 would give, for the review page,
    where a person sets the right price (that price is then trusted). Foreign prices are left alone."""
    from statistics import median

    def num(x):
        try:
            v = float(x)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None
    groups: dict[str, list[float]] = {}
    for r in recs:
        v = num(r.get("price_toman"))
        # the reference is made of ordinary toman prices only: no dollar prices, nothing out of range
        if v and low <= v <= high and "foreign_x" not in str(r.get("price_flags") or "") \
                and r.get("price_estimated") != "reviewed":
            leaf = str(r.get("breadcrumb") or "")
            groups.setdefault("leaf:" + leaf, []).append(v)
            groups.setdefault("parent:" + leaf.rsplit(" > ", 1)[0], []).append(v)
    # every price of a category, in range or not: a category where most prices are above the
    # range (elevator motors: 200-600 million) is expensive, not wrong
    above: dict[str, list[float]] = {}
    for r in recs:
        v = num(r.get("price_toman"))
        if v and "foreign_x" not in str(r.get("price_flags") or ""):
            lf = str(r.get("breadcrumb") or "")
            above.setdefault(lf, []).append(v)
            above.setdefault("parent:" + lf.rsplit(" > ", 1)[0], []).append(v)
    # one price copied onto several different products: 6 cartridge pumps of 5 makers all at exactly
    # 12,200,000 (29 Oct). Different brands almost never cost the very same; a person decides.
    # Counted per shop (31 Oct): the shop that pasted one price on three brands is held, while another
    # shop that sells one of those products at the same price is judged like any other price (batches
    # 9-10 held 81 such prices, many of them right).
    def shop(r):
        m = re.match(r"https?://(?:www\.)?([^/]+)", str(r.get("url") or ""))
        return m.group(1).lower() if m else ""
    makers: dict[tuple, set] = {}
    for r in recs:
        v = num(r.get("price_toman"))
        if v and "foreign_x" not in str(r.get("price_flags") or "") and r.get("price_estimated") != "reviewed":
            b = norm_text(str(r.get("brand") or "")) or " ".join(re.findall(r"[A-Za-z]{3,}", str(r.get("name") or ""))[:1]).lower()
            if b:                                        # unbranded items often share a round price
                makers.setdefault((v, shop(r)), set()).add(b)
    copied = {k for k, b in makers.items() if len(b) >= 3}
    out = []
    for r in recs:
        v = num(r.get("price_toman"))
        if not v or "foreign_x" in str(r.get("price_flags") or "") or r.get("price_estimated") == "reviewed":
            out.append(r)
            continue
        if (v, shop(r)) in copied:
            r = {**r, "price_check": f"suspicious: {v:,.0f} تومان؛ همین قیمت روی {len(makers[(v, shop(r))])} محصول با برند جدا"
                                    " (احتمالاً یک قیمت روی همه کپی شده)"}
            log.warning("price held back %s: %s", r.get("url"), r["price_check"])
            out.append(r)
            continue
        leaf = str(r.get("breadcrumb") or "")
        ref = None
        for g in ("leaf:" + leaf, "parent:" + leaf.rsplit(" > ", 1)[0]):
            others = list(groups.get(g, []))
            if v in others:
                others.remove(v)
            if len(others) >= 3:
                ref = median(others)
                break
        if ref is None and history:                      # too few in this run: the other batches decide
            for g in ("leaf:" + leaf, "parent:" + leaf.rsplit(" > ", 1)[0]):
                if len(history.get(g, [])) >= 5:
                    ref = median(history[g])
                    break
        why = ""
        if "price_outlier" in str(r.get("price_flags") or "") and r.get("price_estimated") != "reviewed":
            why = f"{v:,.0f} تومان؛ خیلی دور از قیمت بقیه‌ی دسته"
        dear = False
        for g in (leaf, "parent:" + leaf.rsplit(" > ", 1)[0]):     # the category, else its parent
            same = list(above.get(g, []))
            if v in same:
                same.remove(v)
            if len(same) >= 3:
                dear = median(same) > high / 3 and 1 / factor <= v / median(same) <= factor
                break
        if why:
            pass
        elif v > high and dear:
            pass                                         # an expensive category: in line with the others
        elif v > high or v < (low / 20 if per_meter(r) else low):
            why = f"{v:,.0f} تومان بیرون از بازه‌ی {low:,.0f} تا {high:,.0f}"
        elif ref and not (1 / factor <= v / ref <= factor) and not (v < ref and is_part(r.get("name", ""))) \
                and not (per_meter(r) and v < ref):        # a metre of hose is cheaper than a hose
            why = f"{v:,.0f} تومان؛ میانه‌ی دسته {ref:,.0f}"
        if why and peers:                    # other shops sell the same model: they decide
            ok = confirm_price(r, v, peers)
            if ok:
                newv, note = ok
                if newv != v:
                    r = {**r, "price_toman": str(round(newv)), "price_usd": ""}
                r = {**r, "price_check": "auto: " + note}
                log.info("price settled %s: %s", r.get("url"), note)
                out.append(r)
                continue
        if why:
            hint = f"شاید {v / 10:,.0f}" if (v > high or (ref and v > ref)) else f"شاید {v * 10:,.0f}"
            r = {**r, "price_check": f"suspicious: {why} ({hint})"}
            log.warning("price held back %s: %s", r.get("url"), r["price_check"])
        out.append(r)
    return out


def keep_published_url(d: Path, new: pd.DataFrame) -> pd.DataFrame:
    """After the merge is run again, a product already on the site may come out with another of
    its pages as the main one; the page it was published with is put back as the main page, so
    its SKU (made from that page) stays the same and no second copy is made on the site."""
    p = d / "07_published.csv"
    if not p.exists() or "urls" not in new.columns:
        return new
    pub = read_csv(p)
    sent = set(pub.loc[pub["action"].isin(["created", "updated"]) & (pub["product_id"].astype(str) != ""), "source_url"])
    if not sent:
        return new
    new = new.copy()
    for i, r in new.iterrows():
        if r["url"] in sent:
            continue
        members = [u.strip() for u in str(r.get("urls") or "").split(" | ") if u.strip()]
        hit = next((u for u in members if u in sent), None)
        if hit:
            new.at[i, "url"] = hit
    return new


def _split_ids(v) -> list[str]:
    return [x.strip() for x in str(v or "").split("|") if x.strip()]


def export_rows(cfg: dict, run: str) -> list[tuple[dict, dict]]:
    """(product record, WooCommerce row) for every new product, after the review page's decisions.
    Shared by the CSV export and direct publishing, so both send exactly the same thing."""
    d = run_dir(cfg, run)
    step = int(cfg.get("price", {}).get("round_to_toman", 1000))
    if not (d / FILES["new"]).exists():
        return []
    new = read_csv(d / FILES["new"])
    if new.empty:
        return []
    send = _send_rate(cfg)
    new = keep_published_url(d, new)
    new = new[new["price_toman"] != ""] if cfg.get("export", {}).get("require_price", True) else new
    from .images import public_urls
    imgs = {}
    if (d / "05_images.csv").exists():
        imgs = {r["url"]: public_urls(cfg, r["files"]) for r in read_csv(d / "05_images.csv").to_dict("records")}
    from .review import apply as apply_review
    recs, imgs = apply_review(d, new.to_dict("records"), imgs)        # decisions taken on the review page
    pc = cfg.get("price", {})
    if pc.get("category_check", True):
        recs = price_sanity(recs, float(pc.get("check_factor", 8)), float(pc.get("check_min_toman", 150_000)),
                            float(pc.get("check_max_toman", 150_000_000)), peers=price_peers(cfg, d),
                            history=price_history(cfg, d, float(pc.get("check_min_toman", 150_000)),
                                                  float(pc.get("check_max_toman", 150_000_000))))
    desc = {}
    if (d / "05_descriptions.csv").exists():
        desc = {x["url"]: x for x in read_csv(d / "05_descriptions.csv").to_dict("records")}
    from . import brands_fa as BF
    from .names import clean_name
    from .publish import category_drops
    bt = BF.table(cfg)
    drops = category_drops(d)
    dropped_bc: dict[str, set[str]] = {}                    # the same drops, as breadcrumbs for the CSV
    if drops:
        bc_of = {}
        for r0 in recs:
            ids_ = _split_ids(r0.get("extra_taxonomy_ids"))
            bcs_ = [b.strip() for b in str(r0.get("extra_breadcrumbs") or "").split(" | ") if b.strip()]
            if len(ids_) == len(bcs_):
                bc_of.update(zip(ids_, bcs_))
        for u, ts in drops.items():
            dropped_bc[u] = {bc_of[t] for t in ts if t in bc_of}
    rows = []
    for r in recs:
        ds = desc.get(r["url"]) or {}
        shops = [x.strip() for x in str(r.get("sellers") or r.get("seller_name") or "").split(" | ")] + \
                [str(r.get("source_domain") or "")]
        base = clean_name(r.get("name_fa") or r["name"], shops)          # no 'قیمت و خرید', '(( اقساط ))', shop names
        br = BF.resolve(r, bt)
        if br["status"] == "none":                                      # no brand on file: the name may say it
            fn = BF.from_name(base, bt)
            if fn:
                br = {**fn, "original": br.get("original", "")}
        r = {**r, "_brand": br, "_dropped_tax": drops.get(r["url"], set())}
        if r["_dropped_tax"]:                                        # categories taken away by a person / repair
            r["extra_taxonomy_ids"] = " | ".join(t for t in _split_ids(r.get("extra_taxonomy_ids"))
                                                 if t not in r["_dropped_tax"])
        brand = br["display"] if br["status"] == "ok" else (r.get("brand", "") if br["status"] == "missing" else "")
        rows.append((r, {
            "Type": "simple", "SKU": caravan_sku(r["url"]),
            "Name": BF.name_with_brand(base, br),
            "Published": cfg.get("export", {}).get("published", 0),
            "Visibility in catalog": "visible",
            "Description": (ds.get("html") or "") + _spec_html(r), "Short description": (METER_NOTE if per_meter(r) else "") + ds.get("short", ""),
            "Regular price": _usd_price(r, send["usd_rate_toman"]),
            "Categories": ", ".join(dict.fromkeys(
                [r["breadcrumb"]] + [b for b in str(r.get("extra_breadcrumbs") or "").split(" | ")
                                     if b and b not in dropped_bc.get(r["url"], set())])),
            "Images": imgs.get(r["url"], ""),
            "Brands": brand,
            "Attribute 1 name": "برند" if brand else "", "Attribute 1 value(s)": brand,
            "Attribute 1 visible": 1 if brand else "", "Attribute 1 global": 1 if brand else "",
            "Meta: _caravan_price_date": send["usd_rate_date"] or r.get("usd_rate_date", ""),
            "Meta: _caravan_usd_rate": send["usd_rate_toman"] or r.get("usd_rate_toman", ""),
            "Meta: _caravan_price_usd": _usd_price(r, send["usd_rate_toman"]),
            "Meta: _caravan_taxonomy_id": r.get("taxonomy_id", ""),
            "Meta: _caravan_source_name": r.get("source_name", ""),
            "Meta: _caravan_price_estimated": r.get("price_estimated", ""),
            "Meta: _caravan_source_url": r.get("url", ""),
            "Meta: _caravan_price_toman": r.get("price_toman", ""),
            "Meta: _caravan_sale_unit": "متر" if per_meter(r) else "",
            "Meta: _caravan_price_check": r.get("price_check", ""),
            "Meta: _caravan_brand_source": br.get("source", ""),
            "Meta: _caravan_supplier": br.get("original", ""),
        }))
    return rows


def step_export(cfg: dict, run: str) -> list[Path]:
    d = run_dir(cfg, run)
    step = int(cfg.get("price", {}).get("round_to_toman", 1000))
    out = []
    rows = [row for _, row in export_rows(cfg, run)]
    from .review import issues_file
    issues_file(cfg, d)
    out.append(write_csv(pd.DataFrame(rows), d / FILES["woo"]))
    upd = read_csv(d / FILES["updates"]) if (d / FILES["updates"]).exists() else pd.DataFrame()
    if len(upd):
        upd = upd[~upd["site_product_id"].astype(str).str.startswith("name-")]   # name-only export: no IDs to update
    if len(upd):
        send = _send_rate(cfg)
        prices = [_usd_price(x, send["usd_rate_toman"]) for x in upd.to_dict("records")]
        u = pd.DataFrame({
            "ID": upd["site_product_id"],
            "Regular price": prices,
            "Meta: _caravan_price_date": send["usd_rate_date"] or upd["usd_rate_date"],
            "Meta: _caravan_usd_rate": send["usd_rate_toman"] or upd["usd_rate_toman"],
            "Meta: _caravan_price_usd": prices,
        })
        u = u[u["Regular price"] != ""]
        out.append(write_csv(u, d / "06_woo_update_prices.csv"))
    log.info("export: %d new products for import%s", len(rows),
             f", {len(upd)} price updates" if len(upd) else "")
    return out


# ------------------------------------------------------------------ stats

def stats(cfg: dict, run: str) -> str:
    d = run_dir(cfg, run)
    lines = [f"run: {run}"]
    for k, name in FILES.items():
        p = d / name
        if p.exists():
            n = len(read_csv(p))
            lines.append(f"  {name:<28} {n:>7,} rows")
    crawled = d / FILES["crawled"]
    if crawled.exists():
        df = read_csv(crawled)
        if len(df):
            priced = (df["price_toman"] != "").mean()
            lines.append(f"  priced share in crawl: {priced:.0%}")
            lines.append("  extracted by: " + ", ".join(f"{k} {v}" for k, v in df["extracted_by"].value_counts().items()))
            flags = df["price_flags"].str.split(" \\| ").explode()
            flags = flags[flags != ""]
            if len(flags):
                lines.append("  price flags: " + ", ".join(f"{k} {v}" for k, v in flags.value_counts().items()))
            lines.append("  top domains: " + ", ".join(f"{k} {v}" for k, v in df["source_domain"].value_counts().head(8).items()))
    return "\n".join(lines)
