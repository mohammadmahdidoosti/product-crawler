"""Promising shops ("فروشگاه‌های مستعد"): a READ-ONLY look at the shops past crawls have met.

Every product page a crawl has read is already on the server's disk: the crawl pool
(DATA_DIR/crawl_pool.csv) and each run's 03_crawled.csv. Grouped by shop, they show which
shops sell many products Caravan does not have yet; those are worth a full crawl of their
catalog, which happens only after a person approves it (the next stage). This report changes
nothing on the site and starts no crawl.

Is a product already on our site? Decided per page (URL), first rule that applies:
  on_site  the URL is in a run's 05_updates_existing.csv (matched to a site product by
           crawler/dedupe.py), 05_on_site_already.csv, 05_same_model_on_site.csv, or a run put it on
           the site itself (07_published.csv with a product id)
  site export (DATA_DIR/site_export.csv), when there is one: the same matcher as the dedupe step
           (dedupe.SiteIndex): >= dedupe.same_threshold -> on_site, >= review_threshold (or the page
           was in a run's 05_review.csv) -> unknown, below -> missing
  without a site export: in a run's 05_new_products.csv of a run that was compared with the site
           (it has matched or review rows) -> missing; in a 05_review.csv -> unknown
  anything else (a page only in the pool, filtered out, not selected...) -> unknown
Unknown is never counted as missing.

Optionally the top shops are probed with GET requests only, like the «تست» button of the
sources page: robots.txt, the WooCommerce Store API (its X-WP-Total header = catalog size) or
the Woo search page. The Fetcher's robots rules, per-site delay and dead-site list apply.

Each report: DATA_DIR/shops/<Tehran time>/shops.csv (UTF-8 with BOM) + summary.json.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .fetch import Fetcher, domain_of
from .io import FILES, read_csv, write_csv

log = logging.getLogger(__name__)

DEFAULTS = {"min_missing": 10, "check_top": 30, "keep_reports": 20, "timezone": "Asia/Tehran",
            "unknown_doubt_share": 0.5}
REPORT = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}$")
SCHEME = "https"                     # tests point this at a local fake shop (http)
COLS = ["rank", "domain", "seller", "phones_count", "seen", "on_site", "missing", "unknown",
        "missing_priced_photo", "missing_share", "categories", "runs_count", "runs", "source",
        "checked", "robots_ok", "store_api", "woo_search", "catalog_size", "crawlable",
        "doubt", "note", "score", "sample_missing_url"]
FILTERS = ("all", "crawlable", "doubt", "source")


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("shops") or {})}


def shops_dir(cfg: dict) -> Path:
    d = Path(cfg["_data_dir"]) / "shops"
    d.mkdir(parents=True, exist_ok=True)
    return d


def reports(cfg: dict) -> list[str]:
    return sorted((d.name for d in shops_dir(cfg).iterdir() if d.is_dir() and REPORT.match(d.name)), reverse=True)


def summary_of(cfg: dict, name: str) -> dict | None:
    try:
        return json.loads((shops_dir(cfg) / name / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def rows_of(cfg: dict, name: str, which: str = "all", q: str = "") -> pd.DataFrame:
    f = shops_dir(cfg) / name / "shops.csv"
    if not f.exists():
        raise FileNotFoundError(name)
    df = read_csv(f)
    if df.empty:
        return df
    if which == "crawlable":
        df = df[df["crawlable"] == "yes"]
    elif which == "doubt":
        df = df[df["doubt"] == "yes"]
    elif which == "source":
        df = df[df["source"] != ""]
    if q.strip():
        needle = q.strip().lower()
        df = df[df.apply(lambda r: needle in " ".join(map(str, r.values)).lower(), axis=1)]
    return df


# ------------------------------------------------------------------ reading what is on disk

def _split_urls(v) -> list[str]:
    return [u.strip() for u in str(v or "").split("|") if u.strip()]


def _urls_of(df: pd.DataFrame) -> set[str]:
    out: set[str] = set()
    if df is None or df.empty:
        return out
    if "url" in df:
        out |= {u for u in df["url"] if u}
    if "urls" in df:
        for v in df["urls"]:
            out |= set(_split_urls(v))
    return out


def _runs(cfg: dict) -> list[Path]:
    d = Path(cfg["_data_dir"]) / "runs"
    return sorted((x for x in d.iterdir() if x.is_dir()), key=lambda x: x.name) if d.exists() else []


def _has_image(v) -> bool:
    s = str(v or "").strip()
    if not s or s in ("[]", "nan"):
        return False
    try:
        return bool(json.loads(s))
    except ValueError:
        return True


def pages(cfg: dict) -> tuple[pd.DataFrame, dict[str, set[str]], dict[str, Counter]]:
    """Every product page read (pool + runs), one row per URL (newest wins), plus the runs each
    URL appeared in and the leaf categories it was found under."""
    keep = ["url", "source_domain", "source_type", "name", "model", "brand", "price_toman", "image_urls",
            "seller_name", "seller_phones", "crawled_at"]
    parts = []
    runs_of: dict[str, set[str]] = defaultdict(set)
    leaves: dict[str, Counter] = defaultdict(Counter)
    pool = Path(cfg["_data_dir"]) / "crawl_pool.csv"
    if pool.exists():
        parts.append(read_csv(pool).reindex(columns=keep, fill_value=""))
    for rd in _runs(cfg):
        f = rd / FILES["crawled"]
        if not f.exists():
            continue
        c = read_csv(f)
        if c.empty or "url" not in c:
            continue
        c = c.fillna("")
        for u, b in zip(c["url"], c.get("breadcrumb", pd.Series("", index=c.index))):
            runs_of[u].add(rd.name)
            leaf = str(b).split(">")[-1].strip()
            if leaf:
                leaves[u][leaf] += 1
        parts.append(c.reindex(columns=keep, fill_value=""))
    if not parts:
        return pd.DataFrame(columns=keep), runs_of, leaves
    allp = pd.concat(parts, ignore_index=True).fillna("")
    allp = allp[(allp["url"] != "") & (allp["name"].astype(str).str.strip() != "")]
    allp = allp.sort_values("crawled_at", kind="stable").drop_duplicates("url", keep="last")
    allp["source_domain"] = [d or domain_of(u) for d, u in zip(allp["source_domain"], allp["url"])]
    allp["source_domain"] = allp["source_domain"].str.lower().str.replace(r"^www\.", "", regex=True)
    return allp.reset_index(drop=True), runs_of, leaves


def evidence(cfg: dict) -> dict:
    """What earlier runs already decided about pages vs our site."""
    on_site, new_compared, review = set(), set(), set()
    compared_runs = []
    for rd in _runs(cfg):
        upd = read_csv(rd / FILES["updates"]) if (rd / FILES["updates"]).exists() else pd.DataFrame()
        rev = read_csv(rd / FILES["review"]) if (rd / FILES["review"]).exists() else pd.DataFrame()
        new = read_csv(rd / FILES["new"]) if (rd / FILES["new"]).exists() else pd.DataFrame()
        on_site |= _urls_of(upd)
        for extra in ("05_on_site_already.csv", "05_same_model_on_site.csv"):
            if (rd / extra).exists():
                on_site |= _urls_of(read_csv(rd / extra))
        pub = rd / "07_published.csv"
        if pub.exists():
            x = read_csv(pub)
            if "source_url" in x and "product_id" in x:
                live = x[(x["product_id"].astype(str).str.strip() != "")
                         & (x.get("action", pd.Series("", index=x.index)) != "removed")]
                on_site |= set(live["source_url"])
        review |= _urls_of(rev)
        if len(upd) or len(rev):                     # this run's dedupe really compared with a site catalog
            compared_runs.append(rd.name)
            new_compared |= _urls_of(new)
    return {"on_site": on_site, "new": new_compared, "review": review, "compared_runs": compared_runs}


def _site_index(cfg: dict):
    from .dedupe import SiteIndex, load_site_catalog
    sc = cfg.get("site_catalog") or {}
    path = sc.get("file")
    if not path or not Path(path).exists():
        return None, None
    try:
        site = load_site_catalog(Path(path), sc.get("columns") or {})
    except Exception as e:                           # noqa: BLE001 - a bad export means "unknown", not a crash
        log.warning("shops: site export not readable (%s); matches from runs only", e)
        return None, None
    info = {"rows": len(site), "modified": datetime.fromtimestamp(Path(path).stat().st_mtime).isoformat(timespec="minutes")}
    return (SiteIndex(site) if len(site) else None), info


def classify(cfg: dict, df: pd.DataFrame, ev: dict, idx, progress=None, stop=None) -> list[str]:
    """'on_site' | 'missing' | 'unknown' for each page (see the module docstring)."""
    dd = cfg.get("dedupe") or {}
    same, maybe = float(dd.get("same_threshold", 0.92)), float(dd.get("review_threshold", 0.8))
    out = []
    recs = df.to_dict("records")
    for n, r in enumerate(recs):
        u = r["url"]
        if u in ev["on_site"]:
            out.append("on_site")
        elif idx is not None:
            i, score, _ = idx.match(r)
            out.append("on_site" if i is not None and score >= same else
                       "unknown" if (i is not None and score >= maybe) or u in ev["review"] else "missing")
        elif u in ev["review"]:
            out.append("unknown")
        elif u in ev["new"]:
            out.append("missing")
        else:
            out.append("unknown")
        if progress and n % 500 == 0:
            progress(n, len(recs))
        if stop and n % 500 == 0 and stop():
            break
    out += ["unknown"] * (len(recs) - len(out))
    return out


# ------------------------------------------------------------------ marketplaces, sources

def not_shops(cfg: dict) -> set[str]:
    from .probe import NOT_SHOPS
    web = (cfg.get("sources") or {}).get("web_search") or {}
    return {d.lower() for d in NOT_SHOPS} | {str(d).lower() for d in (web.get("exclude_domains") or [])}


def _in(dom: str, doms: set[str]) -> bool:
    return any(dom == d or dom.endswith("." + d) for d in doms)


def own_domains(cfg: dict) -> set[str]:
    import os
    out = {"caravansanat.com"}
    if os.environ.get("WC_URL"):
        out.add(domain_of(os.environ["WC_URL"]))
    return out


def known_sources(cfg: dict) -> dict[str, str]:
    """{domain: 'configured' | 'discovered'}: shops the crawler already searches."""
    out: dict[str, str] = {}
    p = Path(cfg["_data_dir"]) / "discovered.csv"
    if p.exists():
        d = read_csv(p)
        if "domain" in d and "type" in d:
            for dom, t in zip(d["domain"], d["type"]):
                if t in ("woo_store_api", "woo_search"):
                    out[str(dom).lower().removeprefix("www.")] = "discovered"
    for s in (cfg.get("sources") or {}).get("iran_sites") or []:
        if isinstance(s, dict) and s.get("domain"):
            out[str(s["domain"]).lower().removeprefix("www.")] = "configured"
    return out


# ------------------------------------------------------------------ crawlability (GET only)

def probe(f: Fetcher, domain: str) -> dict:
    """Read-only check of one shop: robots, Store API (+ catalog size), Woo search."""
    base = f"{SCHEME}://{domain}"
    out = {"robots_ok": "", "store_api": "", "woo_search": "", "catalog_size": "", "crawlable": "", "probe_note": ""}
    if f.is_dead(base + "/"):
        out.update(crawlable="no", probe_note="سایت در چند روز اخیر جواب نداده (فهرست سایت‌های مرده)")
        return out
    ok = f.allowed(base + "/")
    out["robots_ok"] = "yes" if ok else "no"
    if not ok:
        out.update(crawlable="no", probe_note="robots.txt اجازه‌ی خواندن نمی‌دهد")
        return out
    r = f.get(base + "/wp-json/wc/store/v1/products", params={"per_page": 1}, as_json=True, use_cache=False)
    if r and r.get("status") == 200 and isinstance(r.get("json"), list):
        out.update(store_api="yes", crawlable="yes", catalog_size=str(r.get("wp_total") or ""))
        return out
    out["store_api"] = "no"
    r2 = f.get(base + "/", params={"s": "a", "post_type": "product"}, use_cache=False)
    if r2 and r2.get("status") == 200 and "woocommerce" in (r2.get("text") or "").lower():
        out.update(woo_search="yes", crawlable="yes")
    elif r2 is None or (r and r.get("status") in (403, 451)) or (r2 and r2.get("status") in (403, 451)):
        out.update(woo_search="no", crawlable="no", probe_note="سایت جواب نداد یا سرور را مسدود کرد")
    else:
        out.update(woo_search="no", crawlable="no", probe_note="ووکامرس نیست؛ برای کراول کامل باید آدرس جستجو دستی داده شود")
    return out


# ------------------------------------------------------------------ the report

def score_of(r: dict) -> float:
    """0-100: how worth a full crawl the shop looks (ranking is by missing_priced_photo first)."""
    s = 40 * min(r["missing_priced_photo"] / 50, 1)                  # volume of usable products we lack
    s += 20 * (r["missing"] / r["seen"] if r["seen"] else 0)         # how much of what it sells we lack
    s += 15 * (r["missing_priced_photo"] / r["missing"] if r["missing"] else 0)   # quality of those pages
    s += 10 * min(r["runs_count"] / 3, 1)                            # met again and again
    s += {"yes": 15}.get(r.get("store_api"), 8 if r.get("woo_search") == "yes" else 0)
    if r.get("doubt") == "yes":
        s -= 30
    return round(max(0.0, min(100.0, s)), 1)


SCORE_TEXT = ("امتیاز ۰ تا ۱۰۰: ۴۰ برای تعداد محصولات نداشته‌ی قیمت‌دار و عکس‌دار (۵۰ به بالا = کامل)، "
              "۲۰ برای سهم نداشته‌ها از کل دیده‌شده‌ها، ۱۵ برای سهم قیمت‌دار و عکس‌دار در نداشته‌ها، "
              "۱۰ برای تکرار در اجراها (۳ اجرا = کامل)، ۱۵ برای Store API (۸ برای جستجوی ووکامرس)، "
              "۳۰- برای مشکوک. رتبه اول با تعداد نداشته‌ی قیمت‌دار و عکس‌دار است، بعد امتیاز.")


def report(cfg: dict, progress=None, stop=None, check: bool = True, now: datetime | None = None,
           fetcher: Fetcher | None = None) -> dict:
    st = settings(cfg)
    tz = ZoneInfo(st["timezone"])
    t0 = now or datetime.now(tz)
    name = t0.strftime("%Y-%m-%d_%H%M")
    df, runs_of, leaves = pages(cfg)
    ev = evidence(cfg)
    idx, site_info = _site_index(cfg)
    stopped = False
    df["status"] = classify(cfg, df, ev, idx, progress=progress, stop=stop) if len(df) else []
    if stop and stop():
        stopped = True
    df["good"] = (df["price_toman"].astype(str).str.strip() != "") & df["image_urls"].map(_has_image)
    excluded, own = not_shops(cfg), own_domains(cfg)
    sources = known_sources(cfg)
    rows, below = [], 0
    for dom, g in df.groupby("source_domain", sort=False):
        if not dom or _in(dom, own):
            continue
        st_counts = g["status"].value_counts()
        miss = g[g["status"] == "missing"]
        r = {"domain": dom, "seen": len(g), "on_site": int(st_counts.get("on_site", 0)),
             "missing": int(st_counts.get("missing", 0)), "unknown": int(st_counts.get("unknown", 0)),
             "missing_priced_photo": int(miss["good"].sum())}
        if r["missing"] < int(st["min_missing"]):
            below += 1
            continue
        names = [n for n in g["seller_name"] if n and n != dom]
        r["seller"] = Counter(names).most_common(1)[0][0] if names else dom
        r["phones_count"] = len({p for v in g["seller_phones"] for p in str(v).split(" | ") if p.strip()})
        r["missing_share"] = round(r["missing"] / r["seen"], 3)
        lc: Counter = Counter()
        for u in (miss["url"] if len(miss) else g["url"]):
            for leaf in leaves.get(u, {}):
                lc[leaf] += 1
        r["categories"] = " | ".join(x for x, _ in lc.most_common(5))
        rn = sorted({x for u in g["url"] for x in runs_of.get(u, ())})
        r["runs_count"], r["runs"] = len(rn), " | ".join(rn)
        r["source"] = sources.get(dom, "")
        r["sample_missing_url"] = (miss[miss["good"]]["url"].iloc[0] if miss["good"].any()
                                   else miss["url"].iloc[0] if len(miss) else "")
        notes = []
        if _in(dom, excluded):
            notes.append("بازارگاه یا جمع‌کننده‌ی قیمت است، نه فروشگاه")
        if (g["source_type"] == "foreign").mean() > 0.5:
            notes.append("سایت خارجی است")
        if r["unknown"] / r["seen"] > float(st["unknown_doubt_share"]):
            notes.append(f"برای {r['unknown']} از {r['seen']} محصول معلوم نیست در سایت ما هست یا نه")
        r["_notes"] = notes
        rows.append(r)
    # crawlability of the top shops (GET only); marketplaces and foreign sites are not probed
    rows.sort(key=lambda r: (-r["missing_priced_photo"], -r["missing"]))
    to_check = [r for r in rows if not any("بازارگاه" in n or "خارجی" in n for n in r["_notes"])][:int(st["check_top"])] \
        if check else []
    if to_check:
        f = fetcher or Fetcher(cfg, "iran")
        for n, r in enumerate(to_check):
            if stop and stop():
                stopped = True
                break
            try:
                res = probe(f, r["domain"])
            except Exception as e:                    # noqa: BLE001 - one shop must not end the report
                log.warning("shops: probe of %s failed: %s", r["domain"], e)
                res = {"crawlable": "no", "probe_note": "بررسی ناموفق بود"}
            r.update(checked="yes", **{k: v for k, v in res.items() if k != "probe_note"})
            if res.get("robots_ok") == "no":
                r["_notes"].append("robots.txt اجازه‌ی خواندن نمی‌دهد")
            elif res.get("probe_note"):
                r["_notes"].append(res["probe_note"])
            if progress:
                progress(n + 1, len(to_check))
    for r in rows:
        hard = [n for n in r["_notes"] if "ووکامرس نیست" not in n and "جواب نداد" not in n
                and "مرده" not in n and "ناموفق" not in n]
        r["doubt"] = "yes" if hard else ""
        r["note"] = "؛ ".join(r.pop("_notes"))
        r["score"] = score_of(r)
    rows.sort(key=lambda r: (-r["missing_priced_photo"], -r["score"], r["domain"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    out_df = pd.DataFrame(rows).reindex(columns=COLS).fillna("")
    out = shops_dir(cfg) / name
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out_df, out / "shops.csv", COLS)
    tot = df["status"].value_counts() if len(df) else {}
    summ = {"report": name, "started": t0.isoformat(timespec="seconds"),
            "ended": datetime.now(tz).isoformat(timespec="seconds"), "complete": not stopped,
            "pages": len(df), "shops_seen": int(df["source_domain"].nunique()) if len(df) else 0,
            "listed": len(rows), "below_min": below,
            "on_site": int(tot.get("on_site", 0)), "missing": int(tot.get("missing", 0)),
            "unknown": int(tot.get("unknown", 0)),
            "missing_priced_photo": int(out_df["missing_priced_photo"].astype(int).sum()) if len(out_df) else 0,
            "counts": {"all": len(rows), "crawlable": int((out_df["crawlable"] == "yes").sum()) if len(out_df) else 0,
                       "doubt": int((out_df["doubt"] == "yes").sum()) if len(out_df) else 0,
                       "source": int((out_df["source"] != "").sum()) if len(out_df) else 0},
            "checked": len([r for r in rows if r.get("checked") == "yes"]),
            "match": {"site_export": site_info, "compared_runs": ev["compared_runs"],
                      "on_site_evidence_urls": len(ev["on_site"])},
            "settings": {k: st[k] for k in ("min_missing", "check_top", "keep_reports")}, "check": bool(check),
            "score_formula": SCORE_TEXT}
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), encoding="utf-8")
    for old in reports(cfg)[int(st["keep_reports"]):]:
        shutil.rmtree(shops_dir(cfg) / old, ignore_errors=True)
    log.info("SHOPS_REPORT %s", json.dumps({k: summ[k] for k in ("report", "pages", "listed", "missing", "unknown",
                                                                  "checked", "complete")}, ensure_ascii=False))
    return summ
