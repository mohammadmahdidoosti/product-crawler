"""Site fix ("اصلاح سایت"): photos and descriptions for products already on caravansanat.com that
the site-health report found without them. Built on the crawler's own steps, without changing them:

  prepare(kind="photo" | "text")       (crawl lane: never at the same time as a crawl)
    1. the products are read from the site (GET, context=edit);
    2. the same product is looked for elsewhere: first in the crawl pool (pages read before, free),
       then with one web search (Serper) whose result pages are read politely (robots, delays,
       cache) and accepted only when they are the same product (same model code, or a name that
       is clearly the same);
    3. the products become a run folder 'fix-photo-…' / 'fix-text-…' shaped like a crawl run
       (05_new_products.csv), so the image step (download, shop mark out, white background,
       watermark, image search when the source has no photo, vision check) and the description
       step (same model and rules as new products) run on them exactly as on new products;
    4. 08_fix_preview.csv: per product what is there now and what is proposed, with doubts.
  Nothing is sent to the site by prepare.

  apply(run, keys)                      (site lane, only the rows a person approved)
    * a photo only for a product that still has none; a description only where it is still
      missing / still the same short text it was;
    * only 'images', 'description' (and an empty 'short_description') are sent: never a price,
      a name, a category or a status;
    * the stored dollar price is read before and after each update: if it changed, everything stops.
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .io import FILES, read_csv, write_csv
from .normalize import extract_models, model_key, norm_text

log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Tehran")
KINDS = ("photo", "text")
RUN = re.compile(r"^fix-(photo|text)-\d{8}-\d{4,6}$")
FIELDS = "id,name,status,type,sku,permalink,description,short_description,categories,images,brands,attributes,regular_price"
NEW_COLS = ["url", "product_id", "status", "name", "breadcrumb", "brand", "model", "specs_json", "description",
            "short_description", "image_urls", "source_domain", "source_type", "seller_name", "source_url",
            "found_by", "site_images", "site_desc_chars", "site_short", "problems"]
PREVIEW_COLS = ["key", "product_id", "name", "status", "link", "edit_link", "kind", "now", "proposal", "files",
                "html", "short", "method", "source_url", "found_by", "notes", "doubt", "note", "decision",
                "decided_at", "applied", "applied_note"]
STOP_WORDS = {norm_text(w) for w in ("خرید", "قیمت", "و", "با", "برای", "مدل", "برند", "اصل", "اورجینال")}


def settings(cfg: dict) -> dict:
    return {"min_description_chars": int((cfg.get("health") or {}).get("min_description_chars", 150)),
            "web_search": True, "web_results": 5, "pages_per_product": 3, "workers": 4,
            **(cfg.get("sitefix") or {})}


def plain(s: str | None) -> str:
    from .health import plain_text
    return plain_text(s)


# ------------------------------------------------------------------ the same product elsewhere

def _words(name: str) -> list[str]:
    return [w for w in norm_text(name).split() if w not in STOP_WORDS]


UNIT = re.compile(r"^\d+(\.\d+)?(kw|w|v|hp|mm|cm|m|l|lit|bar|a|ah|kg|rpm|nm|hz|psi)$")   # '61kw' is a power


def _codes_of(name: str) -> set[str]:
    """Model codes in a name ('PM45', 'LC1D09', 'Scenery 3520V'), sizes and powers left out."""
    from .pipeline import _codes
    out = {model_key(c) for c in extract_models(name)} | {c for c in _codes(name)}
    return {c for c in out if len(c) >= 4 and re.search(r"\d", c) and re.search(r"[a-z]", c) and not UNIT.match(c)}


def same_product(ours: str, theirs: str, their_model: str = "") -> tuple[bool, str]:
    """Is the page's product ours? A model code in our name must be on the page; without one the
    names must be clearly the same thing (first word, and nearly all words)."""
    from .dedupe import similarity
    a, b = " ".join(_words(ours)), " ".join(_words(theirs))
    if not a or not b:
        return False, ""
    codes = _codes_of(ours)
    if codes:
        page = model_key(theirs + " " + their_model)
        forms = {c for c in codes} | {re.sub(r"^[a-z]{4,}", "", c) for c in codes}   # 'scenery3520v' -> '3520v'
        if any(len(c) >= 3 and re.search(r"\d", c) and c in page for c in forms):
            return similarity(a, b) >= 0.6, "model"
        return False, ""
    try:
        from rapidfuzz import fuzz
        sort_ok = fuzz.token_sort_ratio(a, b) >= 80
    except ImportError:                                   # pragma: no cover
        sort_ok = similarity(a, b) >= 0.9
    first = a.split()[0] == b.split()[0]
    return (first and similarity(a, b) >= 0.9 and sort_ok), "name"


class Pool:
    """Pages the crawler read before (DATA_DIR/crawl_pool.csv): the cheapest place to look."""

    def __init__(self, cfg: dict):
        p = Path(cfg["_data_dir"]) / "crawl_pool.csv"
        self.rows: list[dict] = []
        self.names: list[str] = []
        if p.exists():
            try:
                d = read_csv(p)
                d = d[(d["name"] != "") & (d.get("source_type", "") != "foreign")]
                self.rows = d.to_dict("records")
                self.names = [" ".join(_words(n)) for n in d["name"]]
            except Exception as e:                      # noqa: BLE001 - a bad pool only means no free match
                log.warning("site fix: crawl pool not read (%s)", e)

    def find(self, name: str, need_photo: bool) -> dict | None:
        if not self.rows:
            return None
        q = " ".join(_words(name))
        try:
            from rapidfuzz import fuzz, process
            cands = process.extract(q, self.names, scorer=fuzz.token_set_ratio, score_cutoff=85, limit=8)
            idx = [i for _, _, i in cands]
        except ImportError:                              # pragma: no cover
            idx = [i for i, n in enumerate(self.names) if q.split()[:1] == n.split()[:1]][:8]
        for i in idx:
            r = self.rows[i]
            if need_photo and r.get("image_urls") in ("", "[]"):
                continue
            ok, how = same_product(name, r["name"], r.get("model", ""))
            if ok:
                return {**r, "_how": how}
        return None


def find_on_web(cfg: dict, f, name: str, need_photo: bool) -> dict | None:
    """One web search for the product's name; the first result page that is the same product."""
    from .extract import extract_page
    from .search import search_web
    st = settings(cfg)
    hits = search_web(f, cfg, {"taxonomy_id": "", "query": name, "route": "iran"}, name, int(st["web_results"]))
    for h in hits[:int(st["pages_per_product"])]:
        res = f.get(h["url"])
        if not res or res.get("status") != 200 or not res.get("text"):
            continue
        try:
            rec = extract_page(res["text"], h["url"])
        except Exception:                                 # noqa: BLE001 - an odd page is skipped
            rec = None
        if not rec or not rec.get("name"):
            continue
        ok, how = same_product(name, rec["name"], rec.get("model", ""))
        if not ok:
            continue
        imgs = rec.get("image_urls") or []
        if need_photo and not imgs:
            continue
        return {"name": rec["name"], "brand": rec.get("brand", ""), "model": rec.get("model", ""),
                "specs_json": json.dumps(rec.get("specs") or {}, ensure_ascii=False),
                "description": rec.get("description") or "", "short_description": rec.get("short_description") or "",
                "image_urls": json.dumps(imgs if isinstance(imgs, list) else [], ensure_ascii=False),
                "source_domain": h.get("source_domain", ""), "seller_name": rec.get("seller_name") or "",
                "url": h["url"], "_how": how}
    return None


# ------------------------------------------------------------------ prepare

def run_name(kind: str) -> str:
    return f"fix-{kind}-" + datetime.now(TZ).strftime("%Y%m%d-%H%M%S")


def candidates(cfg: dict, kind: str, report: str | None = None) -> pd.DataFrame:
    """Products of a health report that need this fix, published ones first."""
    from . import health as H
    names = H.reports(cfg)
    report = report or (names[0] if names else None)
    if not report:
        raise ValueError("اول یک گزارش سلامت سایت بسازید")
    f = H.health_dir(cfg) / report / "products.csv"
    if not f.exists():
        raise ValueError(f"گزارش {report} پیدا نشد")
    d = read_csv(f)
    want = {"photo": ("no_image",), "text": ("no_description", "short_description")}[kind]
    d = d[d["problems"].str.split("|").map(lambda xs: any(w in xs for w in want))]
    d = d.assign(_pub=(d["status"] != "publish").astype(int), _id=pd.to_numeric(d["id"], errors="coerce"))
    return d.sort_values(["_pub", "_id"], ascending=[True, False]).drop(columns=["_pub", "_id"])


def _breadcrumbs(cfg: dict) -> dict[str, str]:
    """WordPress category ID -> the taxonomy's full path (for the description's facts)."""
    from .wc import category_map
    tp = Path(cfg.get("taxonomy", {}).get("file") or "")
    if not tp.exists():
        return {}
    from .taxonomy import load_taxonomy
    t = load_taxonomy(tp)
    crumbs = dict(zip(t["taxonomy_id"], t["breadcrumb"]))
    return {wp: crumbs[tid] for tid, wp in category_map().items() if tid in crumbs}


def _site_products(wc, ids: list[int]) -> list[dict]:
    out = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        j = wc._call(lambda: wc._wc("GET", "products", params={
            "include": ",".join(map(str, chunk)), "per_page": 100, "status": "any", "context": "edit",
            "_fields": FIELDS}), "site fix: read products")
        out += j if isinstance(j, list) else []
    return out


def prepare(cfg: dict, kind: str, limit: int = 20, report: str | None = None, ids: list[int] | None = None,
            run: str | None = None, wc=None, progress=None, stop=None) -> dict:
    """Build a fix run (nothing is sent to the site). Returns a summary."""
    from copy import deepcopy

    from .config import run_dir
    from .fetch import Fetcher
    from .wc import WC
    if kind not in KINDS:
        raise ValueError("kind: photo | text")
    st = settings(cfg)
    wc = wc or WC(timeout=60)
    if not wc.ok():
        raise RuntimeError("WC_URL / WC_KEY / WC_SECRET روی سرور تنظیم نشده")
    cand = candidates(cfg, kind, report)
    if ids:
        cand = cand[cand["id"].isin({str(i) for i in ids})]
    chosen = [int(x) for x in cand["id"].head(int(limit) if limit else len(cand))]
    if not chosen:
        raise ValueError("محصولی برای این اصلاح در گزارش نیست")
    run = run or run_name(kind)
    d = run_dir(cfg, run)
    prods = {p["id"]: p for p in _site_products(wc, chosen)}
    crumbs = _breadcrumbs(cfg)
    pool = Pool(cfg)
    f = Fetcher(cfg, "iran")
    rows, found = [], {"pool": 0, "web": 0, "none": 0}
    problems = dict(zip(cand["id"], cand["problems"]))
    for n, pid in enumerate(chosen, 1):
        if stop and stop():
            break
        p = prods.get(pid)
        if not p:
            continue
        need_photo = kind == "photo"
        name = htmllib.unescape(p.get("name") or "")
        src = pool.find(name, need_photo)
        how = "pool"
        if not src and st.get("web_search"):
            src = find_on_web(cfg, f, name, need_photo)
            how = "web"
        found["none" if not src else how] += 1
        cats = p.get("categories") or []
        bc = next((crumbs[str(c["id"])] for c in cats if str(c.get("id")) in crumbs), "")
        bc = bc or " > ".join(htmllib.unescape(c.get("name") or "") for c in cats[:1])
        brand = ", ".join(htmllib.unescape(b.get("name") or "") for b in (p.get("brands") or []))
        attrs = {a.get("name"): " / ".join(a.get("options") or []) for a in (p.get("attributes") or [])
                 if a.get("name") and a.get("options")}
        specs = {}
        if src:
            try:
                specs = json.loads(src.get("specs_json") or "{}") or {}
            except ValueError:
                specs = {}
        specs = {**specs, **attrs}
        site_desc = plain(p.get("description"))
        # the text kind: the product's own short text is a source too (rewritten, never copied)
        desc = " ".join(x for x in ((src or {}).get("description", ""), site_desc if kind == "text" else "") if x)
        rows.append({"url": p.get("permalink") or f"{wc.base}/?p={pid}", "product_id": pid,
                     "status": p.get("status") or "", "name": name,
                     "breadcrumb": bc, "brand": brand or (src or {}).get("brand", ""),
                     "model": (src or {}).get("model", ""), "specs_json": json.dumps(specs, ensure_ascii=False),
                     "description": desc[:4000], "short_description": plain(p.get("short_description"))[:600],
                     "image_urls": (src or {}).get("image_urls", "[]") if need_photo else "[]",
                     "source_domain": (src or {}).get("source_domain", ""), "source_type": "iran",
                     "seller_name": (src or {}).get("seller_name", ""), "source_url": (src or {}).get("url", ""),
                     "found_by": f"{how}:{src.get('_how', '')}" if src else "",
                     "site_images": len(p.get("images") or []), "site_desc_chars": len(site_desc),
                     "site_short": plain(p.get("short_description"))[:300], "problems": problems.get(str(pid), "")})
        if progress:
            progress(n, len(chosen) * 2)
    write_csv(pd.DataFrame(rows, columns=NEW_COLS), d / FILES["new"], NEW_COLS)
    (d / "fix.json").write_text(json.dumps({"kind": kind, "report": report, "products": len(rows), "site": wc.base,
                                            "found": found, "made": datetime.now(TZ).isoformat(timespec="seconds")},
                                           ensure_ascii=False), encoding="utf-8")
    log.info("site fix %s: %d products, source pages: %s", run, len(rows), found)
    if kind == "photo":
        from .images import step_images
        icfg = deepcopy({k: v for k, v in cfg.items() if k != "_data_dir"})
        icfg["_data_dir"] = cfg["_data_dir"]
        # every product here has no photo on the site: image search may be needed for each one
        icfg.setdefault("images", {})["search_max"] = max(int(icfg["images"].get("search_max", 40)), len(rows))
        step_images(icfg, run)
    else:
        from .describe import step_describe
        step_describe(cfg, run)
    return {"run": run, **build_preview(cfg, run)}


# ------------------------------------------------------------------ preview and decisions

def build_preview(cfg: dict, run: str) -> dict:
    from .config import run_dir
    from .images import public_urls
    d = run_dir(cfg, run)
    meta = json.loads((d / "fix.json").read_text(encoding="utf-8"))
    kind = meta["kind"]
    new = read_csv(d / FILES["new"])
    old = {r["key"]: r for r in (read_csv(d / "08_fix_preview.csv").to_dict("records")
                                if (d / "08_fix_preview.csv").exists() else [])}
    imgs = {r["url"]: r for r in read_csv(d / "05_images.csv").to_dict("records")} if (d / "05_images.csv").exists() else {}
    vis = read_csv(d / "05_vision.csv") if (d / "05_vision.csv").exists() else pd.DataFrame()
    desc = {r["url"]: r for r in read_csv(d / "05_descriptions.csv").to_dict("records")} \
        if (d / "05_descriptions.csv").exists() else {}
    base = meta.get("site", "")
    rows = []
    for r in new.to_dict("records"):
        key = str(r["product_id"])
        doubt, files, html, short, method, notes = [], "", "", "", "", ""
        if kind == "photo":
            im = imgs.get(r["url"]) or {}
            files, notes = im.get("files", ""), im.get("notes", "")
            if not files:
                doubt.append("عکسی ساخته نشد")
            if "found_by_search" in notes:
                doubt.append("عکس از جستجوی تصویر آمده؛ ببینید همان محصول است")
            if len(vis) and "url" in vis.columns:
                v = vis[vis["url"] == r["url"]]
                if len(v) and (v["matches_name"].astype(str).str.lower() == "false").any():
                    doubt.append("مدل بینایی مطمئن نیست عکس همین محصول باشد")
            proposal = public_urls(cfg, files)
            now = f"{r['site_images']} عکس"
        else:
            ds = desc.get(r["url"]) or {}
            html, short, method = ds.get("html", ""), ds.get("short", ""), ds.get("method", "")
            if not html:
                doubt.append("توضیحی نوشته نشد")
            elif method != "ai":
                doubt.append("مدل زبانی جواب درست نداد؛ متن از قالب ساده است")
            proposal = f"{len(plain(html).split())} کلمه" if html else ""
            now = f"{r['site_desc_chars']} نویسه"
        if not r.get("source_url") and not (kind == "text" and int(r.get("site_desc_chars") or 0) > 0):
            doubt.append("صفحه‌ی همین محصول جای دیگری پیدا نشد؛ فقط از روی نام و دسته")
        prev = old.get(key, {})
        rows.append({"key": key, "product_id": r["product_id"], "name": r["name"], "status": r.get("status", ""),
                     "link": r["url"], "edit_link": f"{base}/wp-admin/post.php?post={r['product_id']}&action=edit" if base else "",
                     "kind": kind, "now": now, "proposal": proposal, "files": files, "html": html, "short": short,
                     "method": method, "source_url": r.get("source_url", ""), "found_by": r.get("found_by", ""),
                     "notes": notes, "doubt": "yes" if doubt else "", "note": "؛ ".join(doubt),
                     "decision": prev.get("decision", ""), "decided_at": prev.get("decided_at", ""),
                     "applied": prev.get("applied", ""), "applied_note": prev.get("applied_note", "")})
    pv = pd.DataFrame(rows, columns=PREVIEW_COLS)
    write_csv(pv, d / "08_fix_preview.csv", PREVIEW_COLS)
    return summary(pv, meta)


def summary(pv: pd.DataFrame, meta: dict) -> dict:
    ready = pv[(pv["proposal"] != "") & (pv["decision"] == "approved") & (pv["applied"] == "")]
    return {**meta, "rows": len(pv), "with_proposal": int((pv["proposal"] != "").sum()),
            "doubt": int((pv["doubt"] == "yes").sum()), "approved": int((pv["decision"] == "approved").sum()),
            "rejected": int((pv["decision"] == "rejected").sum()), "ready": len(ready),
            "applied": int((pv["applied"] == "yes").sum()), "skipped": int((pv["applied"] == "skipped").sum()),
            "failed": int((pv["applied"] == "error").sum())}


def runs(cfg: dict) -> list[dict]:
    root = Path(cfg["_data_dir"]) / "runs"
    out = []
    for p in sorted(root.glob("fix-*"), key=lambda x: x.name, reverse=True):
        if RUN.match(p.name) and (p / "fix.json").exists():
            pv = read_csv(p / "08_fix_preview.csv") if (p / "08_fix_preview.csv").exists() else pd.DataFrame(columns=PREVIEW_COLS)
            out.append({"run": p.name, **summary(pv.reindex(columns=PREVIEW_COLS, fill_value=""),
                                                  json.loads((p / "fix.json").read_text(encoding="utf-8")))})
    return out


def preview(cfg: dict, run: str) -> pd.DataFrame:
    from .config import run_dir
    p = run_dir(cfg, run) / "08_fix_preview.csv"
    return read_csv(p).reindex(columns=PREVIEW_COLS, fill_value="").fillna("") if p.exists() else pd.DataFrame(columns=PREVIEW_COLS)


def decide(cfg: dict, run: str, items: list[dict]) -> dict:
    """[{key, decision: approved | rejected | ''}]; a row already sent to the site does not change."""
    from .config import run_dir
    if not RUN.match(run):
        raise ValueError("bad run")
    d = run_dir(cfg, run)
    pv = preview(cfg, run)
    now = datetime.now(TZ).isoformat(timespec="seconds")
    by = {str(i.get("key")): i.get("decision") for i in items}
    bad = [v for v in by.values() if v not in ("approved", "rejected", "")]
    if bad:
        raise ValueError(f"decision نامعتبر: {bad[0]}")
    for i, k in enumerate(pv["key"]):
        if k in by and pv.loc[i, "applied"] == "" and not (by[k] == "approved" and pv.loc[i, "proposal"] == ""):
            pv.loc[i, "decision"], pv.loc[i, "decided_at"] = by[k], now if by[k] else ""
    write_csv(pv, d / "08_fix_preview.csv", PREVIEW_COLS)
    return summary(pv, json.loads((d / "fix.json").read_text(encoding="utf-8")))


# ------------------------------------------------------------------ apply (only approved rows)

def _price(p: dict) -> str:
    return str(p.get("regular_price") or "")


def apply(cfg: dict, run: str, wc=None, progress=None, stop=None) -> dict:
    """Send the approved photos / descriptions. Each product is re-read first (context=edit) and
    only changed if it still needs it; its stored price is compared before and after."""
    from .config import run_dir
    from .publish import local_images
    from .wc import WC
    if not RUN.match(run):
        raise ValueError("bad run")
    st = settings(cfg)
    wc = wc or WC(timeout=60)
    if not wc.ok():
        raise RuntimeError("WC_URL / WC_KEY / WC_SECRET روی سرور تنظیم نشده")
    d = run_dir(cfg, run)
    meta = json.loads((d / "fix.json").read_text(encoding="utf-8"))
    kind = meta["kind"]
    pv = preview(cfg, run)
    todo = [i for i in range(len(pv)) if pv.loc[i, "decision"] == "approved" and pv.loc[i, "applied"] == ""
            and pv.loc[i, "proposal"] != ""]
    log.info("site fix apply %s: %d approved", run, len(todo))
    halted = ""
    for n, i in enumerate(todo, 1):
        if stop and stop():
            break
        r = pv.loc[i]
        pid = int(r["product_id"])
        res, note = "skipped", ""
        try:
            cur = wc._call(lambda: wc._wc("GET", f"products/{pid}", params={"context": "edit"}), f"product {pid}")
            before = _price(cur)
            data = {}
            if kind == "photo":
                if cur.get("images"):
                    note = "در این فاصله عکس گرفته؛ دست نخورد"
                else:
                    paths = local_images(cfg, r["proposal"])
                    if not paths:
                        raise RuntimeError("فایل عکس روی سرور نیست")
                    ids = []
                    for k, p in enumerate(paths, 1):
                        m = wc.upload_media(p, f"caravan-{pid}-{k}.webp", alt=str(r["name"]))
                        ids.append({"id": m["id"]})
                    data = {"images": ids}
            else:
                now_chars = len(plain(cur.get("description")))
                was = int(re.sub(r"\D", "", str(r["now"])) or 0)
                if now_chars >= int(st["min_description_chars"]) or now_chars != was:
                    note = "توضیح در این فاصله تغییر کرده؛ دست نخورد"
                else:
                    data = {"description": r["html"]}
                    if not plain(cur.get("short_description")) and r["short"]:
                        data["short_description"] = r["short"]
            if data:
                wc.update_product(pid, data)
                after = _price(wc._call(lambda: wc._wc("GET", f"products/{pid}", params={"context": "edit"}),
                                        f"product {pid}"))
                if after != before:
                    halted = f"قیمت محصول {pid} بعد از به‌روزرسانی عوض شد ({before} ← {after}); همه‌چیز متوقف شد"
                    res, note = "error", halted
                else:
                    res = "yes"
        except Exception as e:                            # noqa: BLE001 - one product's failure is recorded
            res, note = "error", str(e)[:200]
        pv.loc[i, "applied"], pv.loc[i, "applied_note"] = res, note
        write_csv(pv, d / "08_fix_preview.csv", PREVIEW_COLS)
        if progress:
            progress(n, len(todo))
        if halted:
            log.error("SITE_FIX_HALT %s", halted)
            break
        time.sleep(float((cfg.get("publish") or {}).get("pause_seconds", 0.5)))
    out = summary(pv, meta)
    out["halted"] = halted
    log.info("SITE_FIX_APPLY %s %s", run, {k: out[k] for k in ("applied", "skipped", "failed")})
    return out
