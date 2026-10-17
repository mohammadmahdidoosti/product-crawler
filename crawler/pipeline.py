"""The steps, each reading the previous step's CSV in the same run folder."""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pandas as pd

from . import fx as fxmod
from .config import ROOT, run_dir
from .dedupe import load_site_catalog, merge_crawl, split_against_site
from .extract import extract_page, from_store_api, is_listing_url, listing_links, resolve_price_unit
from .fetch import Fetcher, domain_of
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
    for r in queue:           # English term for Made-in-China from the glossary when missing
        if not r.get("english"):
            g = r.get("group") or group_of(r["breadcrumb"])
            r["english"] = leaf_english(r["breadcrumb"].split(">")[-1].strip(), g.split(">")[-1].strip(),
                                        english_for(g, gl))
    f = _fetchers(cfg)
    n_hits = 0
    with Appender(out, HITS_COLS) as w, Appender(d / "02_no_hits.csv", ["taxonomy_id", "query", "route"]) as miss:
        for i, row in enumerate(queue, 1):
            if _progress("search", i - 1, len(queue)):
                break
            if row["taxonomy_id"] in done:
                continue
            hits = search_row(cfg, f, row, en.get(row["query"]), probes)
            for h in hits:
                w.write(h)
            if not hits:
                miss.write(row)
            n_hits += len(hits)
            if i % 10 == 0:
                log.info("search %d/%d, %d hits so far", i, len(queue), n_hits)
    log.info("search done: %d new hits -> %s", n_hits, out.name)
    return out


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
                soup = BeautifulSoup(res["text"], "lxml")
                info["seller_phones"] = extract_phones(soup.get_text(" "))
                if not info["seller_name"]:
                    m = soup.find("meta", attrs={"property": "og:site_name"})
                    info["seller_name"] = m["content"].strip() if m and m.get("content") else ""
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
    d = run_dir(cfg, run)
    hits = read_csv(d / FILES["hits"]).to_dict("records")
    queue = {r["taxonomy_id"]: r for r in read_csv(d / FILES["queue"]).to_dict("records")}
    out = d / FILES["crawled"]
    # a page found for two categories is checked for each (the AI filter
    # decides where it belongs); the disk cache means it is fetched only once
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
    ok = n_listing = 0
    list_max = int(cfg.get("search", {}).get("listing_max", 12))
    with Appender(out, CRAWLED_COLS) as w, Appender(d / "03_failed.csv", ["taxonomy_id", "url", "reason"]) as fail:
        for i, h in enumerate(hits, 1):
            if _progress("crawl", i - 1, len(hits)):
                break
            key = f"{h['taxonomy_id']}|{h['url']}"
            if key in done:
                continue
            done.add(key)
            rec = None
            if h.get("prefilled_json"):
                rec = from_store_api(json.loads(h["prefilled_json"]))
            else:
                fetcher = f["foreign" if h["source_type"] == "foreign" else "iran"]
                res = fetcher.get(h["url"])
                if not res or res.get("status") != 200:
                    fail.write({"taxonomy_id": h["taxonomy_id"], "url": h["url"], "reason": f"http {res.get('status') if res else 'blocked/none'}"})
                    continue
                listing = h["source_type"] != "foreign" and not h.get("from_listing") and is_listing_url(h["url"])
                rec = None if listing else extract_page(res["text"], h["url"])
                if (listing or not rec or not rec.get("price_amount")) and h["source_type"] != "foreign" \
                        and not h.get("from_listing"):
                    links = listing_links(res["text"], h["url"], list_max)
                    if links and (listing or not rec or not rec.get("price_amount")):
                        # a shop's category page: crawl the products listed on it instead
                        hits += [{**h, "url": u, "title": "", "found_by": "listing", "prefilled_json": "",
                                  "from_listing": "1"} for u in links]
                        n_listing += 1
                        continue
                if rec is None and llm:
                    rec = _llm_record(llm, res["text"])
            if not rec or not rec.get("name"):
                fail.write({"taxonomy_id": h["taxonomy_id"], "url": h["url"], "reason": "no product data"})
                continue
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
                "image_urls": json.dumps(rec["image_urls"][:6], ensure_ascii=False),
                "extracted_by": rec["extracted_by"], "supplier_badges": rec.get("supplier_badges", ""),
                "crawled_at": dt.datetime.now().isoformat(timespec="seconds"),
                **price_fields(rec, rate, cfg),
            }
            mult = float(cfg.get("price", {}).get("foreign_multiplier", 1) or 1)
            if h["source_type"] == "foreign" and mult != 1 and row["price_toman"] != "":
                # Made-in-China prices are ex-factory in China: shipping, customs and margin on top
                row["price_toman"] = round(row["price_toman"] * mult)
                row["price_usd"] = round(row["price_usd"] * mult, 2)
                row["price_flags"] = " | ".join(x for x in (row["price_flags"], f"foreign_x{mult:g}") if x)
            w.write(row)
            ok += 1
            if i % 25 == 0:
                log.info("crawl %d/%d, %d products", i, len(hits), ok)
    log.info("crawl done: %d products (%d shop list pages opened) -> %s", ok, n_listing, out.name)
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
    done = done_values(out, "url") | done_values(rej, "url")
    todo = df[~df["url"].isin(done)] if len(df) else df
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


def one_category_per_product(df: pd.DataFrame) -> pd.DataFrame:
    """A page found for several categories stays only in the one it fits best
    (highest relevance confidence; ties go to the longer, more specific category)."""
    if df.empty or "url" not in df.columns:
        return df
    df = df.copy()
    df["_c"] = pd.to_numeric(df.get("relevance_confidence", 0), errors="coerce").fillna(0)
    df["_l"] = df["breadcrumb"].str.len()
    best = df.sort_values(["_c", "_l"], ascending=False, kind="stable").drop_duplicates("url")
    if len(best) < len(df):
        log.info("same page in several categories: kept %d of %d rows (best-fitting category)", len(best), len(df))
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
    from .routing import find_brand
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
        b = find_brand(leaf)
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
            "Categories": r["breadcrumb"],
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
