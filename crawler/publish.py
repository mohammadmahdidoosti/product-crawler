"""Direct publishing to the Caravan Sanat WooCommerce site through its REST API.

Takes exactly what the CSV export would send (pipeline.export_rows, so review decisions,
descriptions and photos are the same) and creates each product as a draft:

  * SKU from the source page (CRV-xxxxxxxxxx): a second publish finds the product by SKU and
    updates it instead of creating a duplicate; its status is never changed on update, so a
    product someone already published stays published
  * categories: taxonomy ID (and the extra categories from merged pages) -> WordPress term ID
    via wp_categories.csv; a product with no mapped category is skipped, never sent uncategorised
  * photos: the local WebP files are uploaded to the media library (application password), so
    the site does not need to reach this server; on update they are only sent if the product
    has none yet
  * price in dollars (the site multiplies it by its own rate), the crawl's toman price kept as meta
  * brand from brands_fa.csv ('آنتو (Antu)'): a WooCommerce Brands term (matched to an existing
    one by Persian or Latin name, created if missing) and the same text as an attribute; products
    whose brand is not in the table yet are skipped, so nothing goes out unreviewed

Results: data/runs/<run>/07_published.csv, one row per SKU, updated on every publish.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from .io import read_csv, write_csv
from .wc import WC, WCError, category_map

log = logging.getLogger(__name__)

RESULT_FILE = "07_published.csv"
RESULT_COLS = ["sku", "action", "product_id", "status", "name", "brand", "price", "categories", "images",
               "edit_url", "error", "source_url", "published_at"]


def _split(v) -> list[str]:
    return [x.strip() for x in str(v or "").split("|") if x.strip()]


def local_images(cfg: dict, images: str) -> list[Path]:
    """'https://<server>/img/abc/1.webp, …' (export column) -> local files under data/images."""
    root = Path(cfg["_data_dir"]) / "images"
    out = []
    for u in str(images or "").split(","):
        u = u.strip()
        if "/img/" not in u:
            continue
        p = root / u.split("/img/", 1)[1]
        if p.exists():
            out.append(p)
    return out


def payload(cfg: dict, rec: dict, row: dict, cats: dict[str, str]) -> tuple[dict, list[str]]:
    """WooCommerce product JSON for one export row; second value lists the unmapped taxonomy IDs."""
    tax_ids = list(dict.fromkeys([str(rec.get("taxonomy_id", "")).strip()] + _split(rec.get("extra_taxonomy_ids"))))
    tax_ids = [t for t in tax_ids if t]
    wp_ids = list(dict.fromkeys(cats[t] for t in tax_ids if t in cats))
    missing = [t for t in tax_ids if t not in cats]
    meta = [{"key": k[len("Meta: "):], "value": str(v)} for k, v in row.items()
            if k.startswith("Meta: ") and str(v or "").strip()]
    data = {
        "name": row["Name"], "type": "simple", "status": "draft", "sku": row["SKU"],
        "regular_price": str(row.get("Regular price") or ""),
        "description": row.get("Description") or "", "short_description": row.get("Short description") or "",
        "catalog_visibility": "visible",
        "categories": [{"id": int(x)} for x in wp_ids],
        "meta_data": meta,
    }
    if str(row.get("Attribute 1 value(s)") or "").strip():
        data["attributes"] = [{"name": "برند", "options": [str(row["Attribute 1 value(s)"]).strip()],
                               "visible": True, "variation": False}]
    return data, missing


class SiteBrands:
    """Brand terms on the site, matched by Persian or Latin name; missing ones are created once."""

    def __init__(self, wc: WC, dry_run: bool = False):
        self.wc, self.dry = wc, dry_run
        try:
            self.terms = wc.list_brands() if wc.ok() else []
        except WCError as e:
            log.warning("site brands not read (%s): brand kept as an attribute only", e)
            self.terms = None
        self.made = 0

    @property
    def available(self) -> bool:
        return self.terms is not None

    def id_for(self, br: dict) -> tuple[int | None, bool]:
        from .brands_fa import match_site
        if not self.available or br.get("status") != "ok":
            return None, False
        t = match_site(self.terms, br.get("fa", ""), br.get("en", ""), br["display"])
        if t:
            return t["id"], False
        if self.dry:
            return None, True
        t = self.wc.create_brand(br["display"])
        self.terms.append({"id": t["id"], "name": br["display"]})
        self.made += 1
        log.info("brand created on the site: %s (#%s)", br["display"], t["id"])
        return t["id"], True


def _results(d: Path) -> pd.DataFrame:
    p = d / RESULT_FILE
    return read_csv(p) if p.exists() else pd.DataFrame(columns=RESULT_COLS)


def _save(d: Path, done: pd.DataFrame, new_rows: list[dict]) -> None:
    if not new_rows:
        return
    df = pd.concat([done, pd.DataFrame(new_rows)], ignore_index=True)
    df = df.drop_duplicates("sku", keep="last").reindex(columns=RESULT_COLS).fillna("")
    write_csv(df, d / RESULT_FILE)


def publish(cfg: dict, run: str, limit: int = 3, skus: list[str] | None = None, dry_run: bool = False,
            republish: bool = False, wc: WC | None = None, progress=None, stop=None) -> dict:
    """Send up to `limit` products of a run (the ones not yet sent first; `skus` picks exact ones).
    dry_run: build and check every payload, upload nothing."""
    from .pipeline import export_rows, run_dir
    d = run_dir(cfg, run)
    items = export_rows(cfg, run)
    if not items:
        raise ValueError("این اجرا محصول تازه‌ی قیمت‌دار ندارد (مرحله‌ی «ادغام» را ببینید).")
    wc = wc or WC()
    if not dry_run and not wc.ok():
        raise ValueError("WC_URL / WC_KEY / WC_SECRET روی سرور تنظیم نشده")
    cats = category_map()
    if not cats:
        raise ValueError("نگاشت دسته‌های وردپرس (wp_categories.csv) پیدا نشد")
    done = _results(d)
    sent = set(done.loc[done["action"].isin(["created", "updated"]), "sku"]) if len(done) else set()
    if skus:
        want = set(skus)
        items = [x for x in items if x[1]["SKU"] in want]
    elif not republish:
        items = [x for x in items if x[1]["SKU"] not in sent]
    items = items[:max(int(limit or 0), 0)] if limit else items
    log.info("publish %s: %d product(s)%s", run, len(items), " (dry run)" if dry_run else "")

    brands = SiteBrands(wc, dry_run) if (wc.ok() or not dry_run) else None
    if brands is not None and not brands.available:
        log.warning("the site has no WooCommerce Brands endpoint: brand kept as an attribute only")
    known = {}                                       # SKU -> (product id, had photos) from earlier publishes
    if len(done):
        for r in done.to_dict("records"):
            pid = str(r.get("product_id") or "").replace(".0", "")
            if pid and r.get("action") in ("created", "updated"):
                known[r["sku"]] = (int(pid), str(r.get("images") or "0") not in ("0", ""))

    # 1. checks and brand terms, one after the other (cheap; a new brand is created exactly once)
    out, counts, work = [], {"created": 0, "updated": 0, "skipped": 0, "error": 0, "checked": 0}, []
    for rec, row in items:
        data, missing = payload(cfg, rec, row, cats)
        files = local_images(cfg, row.get("Images"))
        br = rec.get("_brand") or {}
        res = {"sku": row["SKU"], "name": row["Name"], "price": data["regular_price"], "brand": br.get("display", ""),
               "categories": ",".join(str(c["id"]) for c in data["categories"]),
               "source_url": rec.get("url", ""), "published_at": time.strftime("%Y-%m-%d %H:%M"),
               "images": len(files), "status": "", "product_id": "", "edit_url": "", "error": ""}
        problems = []
        if not data["categories"]:
            problems.append(f"دسته‌ی وردپرس برای شناسه‌ی {', '.join(missing) or '؟'} پیدا نشد")
        if not data["regular_price"]:
            problems.append("بدون قیمت دلاری")
        if br.get("status") == "missing":
            problems.append(f"برند «{br.get('source')}» هنوز در جدول برندها نیست")
        if problems:
            res.update(action="skipped", error=" · ".join(problems))
            counts["skipped"] += 1
            log.warning("publish skip %s: %s", row["SKU"], res["error"])
            out.append(res)
            continue
        if missing:
            res["error"] = f"دسته‌های بدون نگاشت حذف شدند: {', '.join(missing)}"
        try:
            if brands is not None:
                bid, new_brand = brands.id_for(br)
                if bid:
                    data["brands"] = [{"id": int(bid)}]   # the product schema validates objects, not bare IDs
                if new_brand:
                    res["brand"] += " (تازه)"
        except (WCError, OSError) as e:
            res.update(action="error", error=str(e)[:400])
            counts["error"] += 1
            out.append(res)
            if isinstance(e, WCError) and e.status in (401, 403):
                log.warning("publish stopped: the site refused the key (%s)", e.status)
                work = []
                break
            continue
        if dry_run:
            res.update(action="checked", status="draft")
            counts["checked"] += 1
            out.append(res)
            continue
        work.append((row, data, files, res))

    # 2. photos + product, several products at the same time
    lock, halt = threading.Lock(), threading.Event()
    workers = max(1, int(cfg.get("publish", {}).get("workers", 6)))
    state = {"n": 0, "since_save": 0}

    def send(row, data, files, res):
        if halt.is_set() or (stop and stop()):
            return None
        sku = row["SKU"]
        try:
            pid, had_photos = known.get(sku, (None, False))
            if files and not had_photos and wc.can_upload():
                data["images"] = [{"id": wc.upload_media(f, f"{sku.lower()}-{n}{f.suffix}", alt=row["Name"])["id"]}
                                  for n, f in enumerate(files, 1)]
            elif files and not had_photos:
                data["images"] = [{"src": u.strip()} for u in str(row.get("Images") or "").split(",") if u.strip()]
            p = None
            if pid is None:
                try:
                    p = wc.create_product(data)
                    res["action"] = "created"
                except WCError as e:
                    # the SKU is on the site already (an earlier run, or a create whose answer was lost
                    # when the connection dropped): find it and update it instead of making a duplicate
                    if e.status not in (None, 400, 500, 502, 503, 504):
                        raise
                    ex = wc.find_by_sku(sku)
                    if ex is None:
                        if e.status == 400:
                            raise
                        p = wc.create_product(data)          # nothing was made: try once more
                        res["action"] = "created"
                    else:
                        pid = ex["id"]
                        if ex.get("images"):
                            data.pop("images", None)
            if p is None:
                status = data.pop("status", None)             # never un-publish a product someone published
                try:
                    p = wc.update_product(pid, data)
                    res["action"] = "updated"
                except WCError as e:
                    if e.status != 404:
                        raise
                    log.info("publish %s: product #%s was deleted on the site, made again", sku, pid)
                    data["status"] = status or "draft"
                    if files and "images" not in data and wc.can_upload():
                        data["images"] = [{"id": wc.upload_media(f, f"{sku.lower()}-{n}{f.suffix}", alt=row["Name"])["id"]}
                                          for n, f in enumerate(files, 1)]
                    p = wc.create_product(data)
                    res["action"] = "created"
            res.update(product_id=p.get("id", ""), status=p.get("status", ""),
                       edit_url=f"{wc.base}/wp-admin/post.php?post={p.get('id')}&action=edit",
                       images=len(p.get("images") or []) or res["images"])
            log.info("publish %s %s -> #%s (%s)", res["action"], sku, res["product_id"], res["status"])
        except (WCError, OSError) as e:
            res.update(action="error", error=str(e)[:400])
            prev = done[done["sku"] == sku] if len(done) else done
            if len(prev) and str(prev.iloc[-1].get("product_id") or ""):
                res.update(product_id=prev.iloc[-1]["product_id"], edit_url=prev.iloc[-1]["edit_url"],
                           status=prev.iloc[-1]["status"])
            log.warning("publish error %s: %s", sku, e)
            if isinstance(e, WCError) and e.status in (401, 403):
                log.warning("publish stopped: the site refused the key (%s)", e.status)
                halt.set()
        with lock:
            out.append(res)
            counts[res["action"]] += 1
            state["n"] += 1
            state["since_save"] += 1
            if progress:
                progress(state["n"], len(work))
            if state["since_save"] >= 20:                     # a restart mid-way loses at most 20 rows
                _save(d, done, [x for x in out if x["action"] != "checked"])
                state["since_save"] = 0
        return res

    if work:
        log.info("publish: %d product(s) to the site, %d at a time", len(work), workers)
        with ThreadPoolExecutor(workers) as ex:
            for f in [ex.submit(send, *w) for w in work]:
                f.result()
    if progress:
        progress(len(work), len(work))
    if dry_run:
        write_csv(pd.DataFrame(out).reindex(columns=RESULT_COLS).fillna(""), d / "07_publish_check.csv")
    else:
        _save(d, done, out)
    log.info("publish %s done: %s", run, counts)
    return {"counts": counts, "rows": out}


def results(cfg: dict, run: str) -> list[dict]:
    from .pipeline import run_dir
    return _results(run_dir(cfg, run)).to_dict("records")


def set_status(cfg: dict, run: str, status: str = "publish", skus: list[str] | None = None,
               wc: WC | None = None, progress=None, stop=None) -> dict:
    """Publish (or return to draft) every product of a run that is on the site, 50 per request."""
    from .pipeline import run_dir
    if status not in ("publish", "draft", "pending", "private"):
        raise ValueError("bad status")
    wc = wc or WC()
    if not wc.ok():
        raise ValueError("WC_URL / WC_KEY / WC_SECRET روی سرور تنظیم نشده")
    d = run_dir(cfg, run)
    done = _results(d)
    on_site = done[(done["product_id"].astype(str) != "") & done["action"].isin(["created", "updated"])]
    if skus:
        on_site = on_site[on_site["sku"].isin(skus)]
    ids = [int(float(x)) for x in on_site["product_id"]]
    log.info("status %s: %d product(s) of %s", status, len(ids), run)
    changed, failed = {}, []
    for start in range(0, len(ids), 50):
        if stop and stop():
            break
        chunk = ids[start:start + 50]
        try:
            j = wc.batch_update([{"id": i, "status": status} for i in chunk])
        except WCError as e:
            failed += [{"id": i, "error": str(e)[:200]} for i in chunk]
            log.warning("status batch failed: %s", e)
            continue
        for p in j.get("update") or []:
            if p.get("error"):
                failed.append({"id": p.get("id"), "error": str(p["error"].get("message", p["error"]))[:200]})
            else:
                changed[int(p["id"])] = p.get("status", "")
        if progress:
            progress(min(start + 50, len(ids)), len(ids))
    if changed:
        df = done.copy()
        pid = df["product_id"].astype(str).str.replace(r"\.0$", "", regex=True)
        for i, st in changed.items():
            df.loc[pid == str(i), "status"] = st
        write_csv(df.reindex(columns=RESULT_COLS).fillna(""), d / RESULT_FILE)
    res = {"requested": len(ids), "changed": len(changed), "failed": failed[:20]}
    log.info("status %s done: %s", status, {k: (v if k != "failed" else len(failed)) for k, v in res.items()})
    return res
