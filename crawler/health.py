"""Site health ("سلامت سایت"), stage 1: a READ-ONLY nightly look at every product on the shop.

The whole catalog is read page by page through the WooCommerce REST API (GET requests only;
nothing on the site is changed here) and every product with a problem gets one row:

  no_image             no photo, or only WooCommerce's placeholder
  no_description       neither a description nor a short description
  no_category          no category, or only the default "uncategorized" one
  short_description    [doubt] a description shorter than health.min_description_chars
  outside_taxonomy     [doubt] categories exist, but none of them is in assets/wp_categories.csv
  price_toman_suspect  [doubt] a stored price above health.max_usd: the site stores dollars, so this is
                       probably a toman (or rial) number typed into the price field. Prices are read with
                       context=edit, as stored; the API's normal view returns them already multiplied by
                       the dollar rate by the currency plugin (WebiCurrency), which looks like toman.
  no_price             a simple product with no price

Doubtful rows are only flagged for a person; the later stages (photos, descriptions,
categories) act on the clear problems, and only after approval.

Each scan writes DATA_DIR/health/<Tehran time>/products.csv (UTF-8 with BOM, opens in Excel)
and summary.json, with how many problems are new since the previous complete scan.
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .io import write_csv

log = logging.getLogger(__name__)

PROBLEMS = {                        # code -> (Persian label, doubtful?)
    "no_image": ("بی‌عکس", False),
    "no_description": ("بی‌توضیح", False),
    "no_category": ("بی‌دسته", False),
    "short_description": ("توضیح کوتاه", True),
    "outside_taxonomy": ("دسته بیرون از تکسونومی", True),
    "price_toman_suspect": ("قیمت مشکوک به تومان", True),
    "no_price": ("بی‌قیمت", False),
}
COLS = ["id", "name", "status", "type", "sku", "problems", "problems_fa", "doubt", "note",
        "categories", "images", "description_chars", "short_description_chars", "price",
        "date_modified", "link", "edit_link"]
FIELDS = ("id,name,status,type,sku,permalink,description,short_description,categories,images,"
          "regular_price,price,date_modified")
DEFAULTS = {"nightly": True, "at": "03:30", "timezone": "Asia/Tehran", "per_page": 100, "pause_seconds": 0.5,
            "min_description_chars": 150, "max_usd": 100000, "keep_reports": 30, "window_hours": 4,
            "uncategorized": ["uncategorized", "بدون-دسته-بندی", "دسته-بندی-نشده"]}
REPORT = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}$")


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("health") or {})}


def health_dir(cfg: dict) -> Path:
    d = Path(cfg["_data_dir"]) / "health"
    d.mkdir(parents=True, exist_ok=True)
    return d


def plain_text(s: str | None) -> str:
    """Visible text of a description: no tags, shortcodes, entities or extra spaces."""
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>|\[/?[a-z_\-]+[^\]]*\]", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", htmllib.unescape(s)).strip()


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip()) if str(v or "").strip() else None
    except ValueError:
        return None


def check(p: dict, st: dict, mapped_wp_ids: set[str]) -> tuple[list[str], list[str]]:
    """Problems of one product, and notes for a person (Persian)."""
    probs, notes = [], []
    imgs = [i for i in (p.get("images") or []) if "woocommerce-placeholder" not in str(i.get("src") or "")]
    if not imgs:
        probs.append("no_image")
    desc, short = plain_text(p.get("description")), plain_text(p.get("short_description"))
    if not desc and not short:
        probs.append("no_description")
    elif len(desc) < int(st["min_description_chars"]):
        probs.append("short_description")
        notes.append(f"توضیح {len(desc)} نویسه است")
    unc = {str(x).lower() for x in st["uncategorized"]}
    cats = [c for c in (p.get("categories") or [])
            if str(c.get("slug") or "").lower() not in unc and c.get("name") not in ("Uncategorized", "دسته‌بندی نشده")]
    if not cats:
        probs.append("no_category")
    elif mapped_wp_ids and not any(str(c.get("id")) in mapped_wp_ids for c in cats):
        probs.append("outside_taxonomy")
        notes.append("دسته: " + "، ".join(str(c.get("name")) for c in cats))
    price = _num(p.get("regular_price")) if p.get("type") != "variable" else _num(p.get("price"))
    if price is None:
        if p.get("type") in ("simple", None, ""):
            probs.append("no_price")
    elif price > float(st["max_usd"]):
        probs.append("price_toman_suspect")
        notes.append(f"قیمت {price:,.0f} بالای سقف {float(st['max_usd']):,.0f} دلار است")
    return probs, notes


def row_of(p: dict, base: str, probs: list[str], notes: list[str]) -> dict:
    desc, short = plain_text(p.get("description")), plain_text(p.get("short_description"))
    return {"id": p.get("id"), "name": p.get("name") or "", "status": p.get("status") or "", "type": p.get("type") or "",
            "sku": p.get("sku") or "", "problems": "|".join(probs),
            "problems_fa": "، ".join(PROBLEMS[x][0] for x in probs),
            "doubt": "yes" if all(PROBLEMS[x][1] for x in probs) else "",
            "note": "؛ ".join(notes), "categories": " | ".join(str(c.get("name")) for c in p.get("categories") or []),
            "images": len(p.get("images") or []), "description_chars": len(desc), "short_description_chars": len(short),
            "price": p.get("regular_price") if p.get("type") != "variable" else p.get("price"),
            "date_modified": p.get("date_modified") or "", "link": p.get("permalink") or "",
            "edit_link": f"{base}/wp-admin/post.php?post={p.get('id')}&action=edit" if base else ""}


def reports(cfg: dict) -> list[str]:
    """Finished reports, newest first. The folder of a scan still running has no summary.json yet:
    it is not offered (the page asked for its summary and showed «گزارش پیدا نشد»)."""
    return sorted((d.name for d in health_dir(cfg).iterdir()
                   if d.is_dir() and REPORT.match(d.name) and (d / "summary.json").exists()), reverse=True)


def summary_of(cfg: dict, name: str) -> dict | None:
    p = health_dir(cfg) / name / "summary.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pairs(cfg: dict, name: str) -> set[tuple[str, str]]:
    f = health_dir(cfg) / name / "products.csv"
    if not f.exists():
        return set()
    d = pd.read_csv(f, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    return {(i, x) for i, ps in zip(d["id"], d["problems"]) for x in ps.split("|") if x}


def scan(cfg: dict, wc=None, progress=None, stop=None, now: datetime | None = None) -> dict:
    """Read every product (GET only) and write the report. Returns the summary."""
    from .wc import WC, category_map
    st = settings(cfg)
    wc = wc or WC(timeout=60)
    if not wc.ok():
        raise RuntimeError("WC_URL / WC_KEY / WC_SECRET روی سرور تنظیم نشده")
    tz = ZoneInfo(st["timezone"])
    t0 = now or datetime.now(tz)
    name = t0.strftime("%Y-%m-%d_%H%M")
    out = health_dir(cfg) / name
    # folders of scans that never finished (no summary.json) and are over a day old are cleared
    for old_dir in health_dir(cfg).iterdir():
        if old_dir.is_dir() and REPORT.match(old_dir.name) and not (old_dir / "summary.json").exists() \
                and time.time() - old_dir.stat().st_mtime > 86400:
            shutil.rmtree(old_dir, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    mapped = {str(v) for v in category_map().values()}
    rows, seen, page, pages, total = [], 0, 1, 1, 0
    by_status: dict[str, int] = {}
    stopped, failed = False, ""
    while page <= max(pages, 1):
        if stop and stop():
            stopped = True
            break
        try:
            items, total, pages = wc.products_page(page, int(st["per_page"]), FIELDS)
        except Exception as e:                       # noqa: BLE001 - what was read is still reported
            failed = f"page {page}: {str(e)[:200]}"
            log.warning("health: reading %s failed after its retries; the report covers what was read", failed)
            stopped = True
            break
        if not items:
            break
        for p in items:
            seen += 1
            by_status[p.get("status") or "?"] = by_status.get(p.get("status") or "?", 0) + 1
            probs, notes = check(p, st, mapped)
            if probs:
                rows.append(row_of(p, wc.base, probs, notes))
        if progress:
            progress(seen, total or seen)
        log.info("health: page %d/%d, %d products read, %d with problems", page, pages, seen, len(rows))
        page += 1
        time.sleep(float(st["pause_seconds"]))
    df = pd.DataFrame(rows, columns=COLS)
    write_csv(df, out / "products.csv", COLS)
    counts = {k: int(df["problems"].str.split("|").map(lambda xs, k=k: k in xs).sum()) if len(df) else 0
              for k in PROBLEMS}
    by_problem_status = {k: (df[df["problems"].str.split("|").map(lambda xs, k=k: k in xs)]["status"]
                             .value_counts().to_dict() if len(df) else {}) for k in PROBLEMS}
    prev = next((r for r in reports(cfg) if r != name and (summary_of(cfg, r) or {}).get("complete")), None)
    new_since = {}
    if prev:
        old, cur = _pairs(cfg, prev), {(str(i), x) for i, ps in zip(df["id"], df["problems"]) for x in ps.split("|") if x}
        new_since = {k: sum(1 for (_, x) in cur - old if x == k) for k in PROBLEMS}
        new_since["fixed"] = len({i for i, _ in old} - {i for i, _ in cur})
    summ = {"report": name, "started": t0.isoformat(timespec="seconds"),
            "ended": datetime.now(tz).isoformat(timespec="seconds"), "complete": not stopped and seen >= total,
            "failed": failed,
            "products_read": seen, "products_on_site": total, "with_problems": len(df),
            "clear_problems": int((df["doubt"] != "yes").sum()) if len(df) else 0,
            "doubtful_only": int((df["doubt"] == "yes").sum()) if len(df) else 0,
            "counts": counts, "by_problem_status": by_problem_status, "by_status": by_status,
            "previous": prev, "new_since_previous": new_since,
            "labels": {k: v[0] for k, v in PROBLEMS.items()}, "doubtful": [k for k, v in PROBLEMS.items() if v[1]],
            "settings": {k: st[k] for k in ("min_description_chars", "max_usd")}}
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), encoding="utf-8")
    for old_name in reports(cfg)[int(st["keep_reports"]):]:
        shutil.rmtree(health_dir(cfg) / old_name, ignore_errors=True)
    log.info("HEALTH_SCAN %s", json.dumps({k: summ[k] for k in ("report", "products_read", "with_problems", "counts",
                                                                 "complete")}, ensure_ascii=False))
    return summ


def in_window(cfg: dict, now: datetime | None = None) -> bool:
    """Inside tonight's scan window (health.at … + window_hours)?"""
    st = settings(cfg)
    now = now or datetime.now(ZoneInfo(st["timezone"]))
    hh, mm = (int(x) for x in str(st["at"]).split(":"))
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return start <= now < start + timedelta(hours=float(st["window_hours"]))


def due(cfg: dict, now: datetime | None = None) -> bool:
    """Is the nightly scan due? Once per local day, at or after health.at."""
    st = settings(cfg)
    if not st.get("nightly"):
        return False
    tz = ZoneInfo(st["timezone"])
    now = now or datetime.now(tz)
    hh, mm = (int(x) for x in str(st["at"]).split(":"))
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if not (start <= now < start + timedelta(hours=float(st["window_hours"]))):
        return False          # a server that was down at night does not scan in the shop's busy hours
    mark = health_dir(cfg) / "last_nightly.txt"
    return not (mark.exists() and mark.read_text(encoding="utf-8").strip() == now.strftime("%Y-%m-%d"))


def mark_nightly(cfg: dict, now: datetime | None = None) -> None:
    st = settings(cfg)
    now = now or datetime.now(ZoneInfo(st["timezone"]))
    (health_dir(cfg) / "last_nightly.txt").write_text(now.strftime("%Y-%m-%d"), encoding="utf-8")
