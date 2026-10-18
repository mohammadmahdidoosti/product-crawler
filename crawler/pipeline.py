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
from .extract import extract_page, from_store_api, is_listing_url, listing_links, resolve_price_unit
from .fetch import Fetcher, canon_url, domain_of
from .io import (CRAWLED_COLS, FILES, FILTER_COLS, HITS_COLS, Appender, done_values,
                 read_csv, write_csv)
from .normalize import extract_phones
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
        r["synonyms"] = syn.get(r["taxonomy_id"], "")
    for r in queue:           # English term for Made-in-China from the glossary when missing
        if not r.get("english"):
            g = r.get("group") or group_of(r["breadcrumb"])
            r["english"] = leaf_english(r["breadcrumb"].split(">")[-1].strip(), g.split(">")[-1].strip(),
                                        english_for(g, gl))
    f = _fetchers(cfg)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .rules import judge
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
                    return "list", links
        if rec is None and llm:
            rec = _llm_record(llm, html)
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
                nxt += [{**h, "url": canon_url(u), "title": "", "found_by": "listing", "prefilled_json": "",
                         "from_listing": "1"} for u in val if f"{h['taxonomy_id']}|{u}" not in done]
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

def step_dedupe(cfg: dict, run: str, site_export: str | None, skip_filter: bool = False) -> list[Path]:
    d = run_dir(cfg, run)
    src = d / (FILES["crawled"] if skip_filter else FILES["filtered"])
    df = read_csv(src)
    dd = cfg.get("dedupe", {})
    df = cross_assign(cfg, d, df)
    df = one_category_per_product(df)
    merged = merge_crawl(df, float(dd.get("crawl_name_threshold", 0.9)))
    merged = drop_price_outliers(merged, float(cfg.get("price", {}).get("outlier_factor", 6)))
    from .brandfix import clean_brand
    merged["brand"] = [clean_brand(b, n) for b, n in zip(merged.get("brand", ""), merged["name"])]
    need = merged.iloc[0:0]
    if cfg.get("price", {}).get("estimate_missing", True) and len(merged):
        from .pricing import estimate
        crawled = read_csv(d / FILES["crawled"])
        merged, need = estimate(cfg, merged, crawled, _fetchers(cfg)["iran"])
    write_csv(need, d / "05_needs_price.csv")
    from .ranking import select_top
    sel = cfg.get("select", {})
    n_all = len(merged)
    merged, dropped = select_top(merged, int(sel.get("max_per_category", 24)), int(sel.get("max_per_seller", 3)))
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


def low_categories(cfg: dict, d: Path, kept: pd.DataFrame, need: pd.DataFrame) -> Path:
    """Categories below the target count: the list for finding and inviting suppliers."""
    target = int(cfg.get("select", {}).get("max_per_category", 24))
    q = read_csv(d / FILES["queue"])
    ids = []
    for t, extra in zip(kept.get("taxonomy_id", []), kept.get("extra_taxonomy_ids", [""] * len(kept))):
        ids += [t] + [x for x in str(extra or "").split(" | ") if x]      # extra categories count too
    n = pd.Series(ids).value_counts() if ids else pd.Series(dtype=int)
    m = need["taxonomy_id"].value_counts() if len(need) else pd.Series(dtype=int)
    rows = []
    for r in q.to_dict("records"):
        k, w = int(n.get(r["taxonomy_id"], 0)), int(m.get(r["taxonomy_id"], 0))
        if k < target:
            rows.append({"taxonomy_id": r["taxonomy_id"], "breadcrumb": r["breadcrumb"], "route": r["route"],
                         "products": k, "without_price": w, "missing": target - k,
                         "status": "خالی" if k == 0 and w == 0 else "فقط بی‌قیمت" if k == 0 else "کم‌محصول"})
    out = pd.DataFrame(rows, columns=["taxonomy_id", "breadcrumb", "route", "products", "without_price", "missing", "status"])
    out = out.sort_values(["products", "breadcrumb"]) if len(out) else out
    log.info("categories below %d products: %d of %d (%d empty) -> 05_low_categories.csv", target, len(out), len(q),
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


def step_topup(cfg: dict, run: str) -> Path | None:
    """Second pass for categories still (nearly) empty after the filter: one Google search
    with the category's exact name (what a person would type), its shop pages crawled and
    filtered. Costs one Serper search per such category, capped by search.topup_max."""
    from .config import secret
    from .probe import classify_results, record_usage
    from .search import SEARCHERS, _hit, detect_type
    sc = cfg.get("search", {})
    below, cap = int(sc.get("topup_below", 5)), int(sc.get("topup_max", 80))
    d = run_dir(cfg, run)
    key = secret("SERPER_API_KEY")
    if not below or not key or not (d / FILES["filtered"]).exists():
        log.info("top-up skipped (%s)", "no SERPER_API_KEY" if not key else "off or nothing filtered yet")
        return None
    q = read_csv(d / FILES["queue"]).to_dict("records")
    fl = read_csv(d / FILES["filtered"])
    cnt = fl.groupby("taxonomy_id")["url"].nunique().to_dict() if len(fl) else {}
    mark = d / "02_topup_done.csv"
    done = done_values(mark, "taxonomy_id")
    low = [r for r in q if cnt.get(r["taxonomy_id"], 0) < below and r["route"] in ("iran", "both")
           and r["taxonomy_id"] not in done][:cap]
    log.info("top-up: %d categories under %d products get an exact web search", len(low), below)
    f = _fetchers(cfg)["iran"]
    n_hits = 0
    with Appender(d / FILES["hits"], HITS_COLS) as w, Appender(mark, ["taxonomy_id", "query", "found"]) as m:
        for i, r in enumerate(low, 1):
            if _progress("topup", i - 1, len(low)):
                break
            res = f.post_json("https://google.serper.dev/search", {"q": f"{r['query']} قیمت", "gl": "ir", "hl": "fa", "num": 10},
                              {"X-API-KEY": key, "Content-Type": "application/json"})
            record_usage(cfg)
            shops = classify_results((res or {}).get("json", {}).get("organic", []))["domains"] if res else []
            hits = []
            for sh in shops:
                src = {"domain": sh["domain"], "kind": "iran"}
                hits.append(_hit(r, src, sh["url"], sh.get("title", ""), "web_exact"))
                t = detect_type(f, cfg, sh["domain"])
                if t in SEARCHERS:
                    try:
                        hits += SEARCHERS[t](f, {**src, "type": t}, r, 8)
                    except Exception as e:
                        log.warning("top-up shop search failed on %s: %s", sh["domain"], e)
            seen = set()
            for h in hits:
                if h["url"] not in seen:
                    seen.add(h["url"])
                    w.write(h)
            n_hits += len(seen)
            m.write({"taxonomy_id": r["taxonomy_id"], "query": r["query"], "found": len(seen)})
    log.info("top-up: %d new links; crawling and filtering them", n_hits)
    step_crawl(cfg, run)
    return step_filter(cfg, run)


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
    """A price far from the others in its category (e.g. a 'US$1' bait price) is removed;
    the product then has no price and is not exported until it gets a real one."""
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
            df.at[i, "price_flags"] = " | ".join(x for x in (str(df.at[i, "price_flags"]), "price_outlier") if x)
            df.at[i, "price_toman"] = ""
            df.at[i, "price_usd"] = ""
        n += len(bad)
    if n:
        log.info("price outliers removed: %d (far from their category's median)", n)
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
    """Description built only from extracted facts, never the source's text."""
    specs = json.loads(r.get("specs_fa_json") or r.get("specs_json") or "{}")
    facts = {"برند": r.get("brand"), "مدل": r.get("model"), **specs}
    lis = "".join(f"<li><strong>{k}:</strong> {v}</li>" for k, v in facts.items() if v)
    return f"<ul>{lis}</ul>" if lis else ""


def step_export(cfg: dict, run: str) -> list[Path]:
    d = run_dir(cfg, run)
    step = int(cfg.get("price", {}).get("round_to_toman", 1000))
    out = []
    new = read_csv(d / FILES["new"])
    new = new[new["price_toman"] != ""] if cfg.get("export", {}).get("require_price", True) else new
    rows = []
    for r in new.to_dict("records"):
        rows.append({
            "Type": "simple", "SKU": "", "Name": r.get("name_fa") or r["name"],
            "Published": cfg.get("export", {}).get("published", 0),
            "Visibility in catalog": "visible",
            "Description": _spec_html(r), "Short description": "",
            "Regular price": _round_price(r["price_toman"], step),
            "Categories": ", ".join(dict.fromkeys(
                [r["breadcrumb"]] + [b for b in str(r.get("extra_breadcrumbs") or "").split(" | ") if b])),
            "Images": "",
            "Attribute 1 name": "برند" if r.get("brand") else "", "Attribute 1 value(s)": r.get("brand", ""),
            "Attribute 1 visible": 1 if r.get("brand") else "", "Attribute 1 global": 1 if r.get("brand") else "",
            "Meta: _caravan_price_date": r.get("usd_rate_date", ""),
            "Meta: _caravan_usd_rate": r.get("usd_rate_toman", ""),
            "Meta: _caravan_price_usd": r.get("price_usd", ""),
            "Meta: _caravan_taxonomy_id": r.get("taxonomy_id", ""),
            "Meta: _caravan_source_name": r.get("source_name", ""),
            "Meta: _caravan_price_estimated": r.get("price_estimated", ""),
        })
    out.append(write_csv(pd.DataFrame(rows), d / FILES["woo"]))
    upd = read_csv(d / FILES["updates"]) if (d / FILES["updates"]).exists() else pd.DataFrame()
    if len(upd):
        upd = upd[~upd["site_product_id"].astype(str).str.startswith("name-")]   # name-only export: no IDs to update
    if len(upd):
        u = pd.DataFrame({
            "ID": upd["site_product_id"],
            "Regular price": upd["price_toman"].map(lambda x: _round_price(x, step)),
            "Meta: _caravan_price_date": upd["usd_rate_date"],
            "Meta: _caravan_usd_rate": upd["usd_rate_toman"],
            "Meta: _caravan_price_usd": upd["price_usd"],
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
