"""Slow Torob price check of the site's published products, run by a userscript in the owner's Chrome.

Torob answers Iranian browsers, not the server, and must be asked slowly (the owner's own Torob
account and panels must never be blocked). So the server only keeps the queue and judges; the
asking is done by static/torob-checker.user.js in a torob.com tab of the owner's Chrome:

  1. GET  /api/torob/next            -> {"id", "q"} the next product to look up (or {"wait": s})
  2. the script searches Torob for q (one request) and posts the top results:
     POST /api/torob/result {"id", "items": [{"name1", "price", "random_key"}]}
     -> {"details": prk} when one result is the same model (or {"done": true})
  3. the script reads that product's offers (one more request) and posts them:
     POST /api/torob/result {"id", "prk", "offers": [{"shop", "price"}]} -> {"done": true, "verdict"}
  then it waits a random 60-150 s before the next product, and stops at the daily cap.

Both endpoints take the token from GET /api/torob/token (behind the crawler's password), so the
script never needs the password. Nothing on the site changes: DATA_DIR/torob/checks.csv is a
report (verdict ok / high / low / no_match per product) for a person to act on.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from pathlib import Path
from statistics import median

import pandas as pd

from .io import read_csv, write_csv

log = logging.getLogger(__name__)
COLS = ["id", "name", "our_toman", "checked_at", "verdict", "ratio", "median_toman", "min_toman", "offers",
        "torob_name", "prk", "note"]
LOCK = threading.Lock()
LEASE: dict[str, float] = {}                    # id -> time it was handed out (one script, but a reload asks again)
DEFAULTS = {"daily_cap": 300, "recheck_days": 30, "tolerance": 0.15, "lease_seconds": 600, "min_offers": 2}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("torob_check") or {})}


def tdir(cfg: dict) -> Path:
    d = Path(cfg["_data_dir"]) / "torob"
    d.mkdir(parents=True, exist_ok=True)
    return d


def token(password: str) -> str:
    """The script's key: derived from the crawler's password, so there is nothing new to keep secret."""
    return hmac.new((password or "").encode(), b"torob-check", hashlib.sha256).hexdigest()[:32]


def token_ok(given: str) -> bool:
    pw = os.environ.get("APP_PASSWORD", "")
    return bool(pw) and hmac.compare_digest(str(given or ""), token(pw))


def checks(cfg: dict) -> pd.DataFrame:
    p = tdir(cfg) / "checks.csv"
    return read_csv(p).reindex(columns=COLS, fill_value="").fillna("") if p.exists() else pd.DataFrame(columns=COLS)


def _save(cfg: dict, df: pd.DataFrame) -> None:
    write_csv(df, tdir(cfg) / "checks.csv", COLS)


def products(cfg: dict) -> pd.DataFrame:
    """Products with a price to check: those held as 'pending' for a doubtful price first (31 Oct: 174 of
    them waited for a person), then the published ones (id, name, our toman, status). The pending ones
    come from the batches' publish results, so today's batch is in even before the nightly site scan."""
    from . import health as H
    from .fx import today_rate
    rate = float(today_rate(cfg)["usd_rate_toman"])
    parts = []
    names = H.reports(cfg)
    if names:
        d = read_csv(H.health_dir(cfg) / names[0] / "products.csv")
        d = d[d["status"].isin(["publish", "pending"])]
        parts.append(d.assign(our_toman=(pd.to_numeric(d["price"], errors="coerce") * rate).round(-3))
                     [["id", "name", "our_toman", "status"]])
    pend = []
    for f in sorted(Path(cfg["_data_dir"]).glob("runs/*/07_published.csv")):
        try:
            x = read_csv(f)
        except Exception:                                    # noqa: BLE001
            continue
        if "status" in x and "product_id" in x:
            x = x[(x["status"] == "pending") & (x["product_id"].astype(str) != "")]
            pend.append(pd.DataFrame({"id": x["product_id"].astype(str).str.replace(r"\.0$", "", regex=True),
                                      "name": x["name"], "status": "pending",
                                      "our_toman": (pd.to_numeric(x["price"], errors="coerce") * rate).round(-3)}))
    if pend:
        parts.append(pd.concat(pend, ignore_index=True))
    if not parts:
        return pd.DataFrame(columns=["id", "name", "our_toman", "status"])
    d = pd.concat(parts, ignore_index=True).astype({"id": str})
    d = d[d["our_toman"] > 0]
    # a product published since (by a person or by a Torob verdict) is no longer pending
    pub = set(d.loc[d["status"] == "publish", "id"])
    d = d[~((d["status"] == "pending") & d["id"].isin(pub))]
    d = d.sort_values("status", key=lambda s: s != "pending", kind="stable")
    return d.drop_duplicates("id", keep="first")[["id", "name", "our_toman", "status"]]


def _today_count(df: pd.DataFrame) -> int:
    today = time.strftime("%Y-%m-%d")
    return int(df["checked_at"].astype(str).str.startswith(today).sum())


def next_item(cfg: dict) -> dict:
    st = settings(cfg)
    with LOCK:
        done = checks(cfg)
        if _today_count(done) >= int(st["daily_cap"]):
            return {"wait": 3600, "why": "سقف روزانه پر شد"}
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400 * int(st["recheck_days"])))
        fresh = set(done.loc[done["checked_at"].astype(str) >= cutoff, "id"].astype(str))
        now = time.time()
        for k in [k for k, t in LEASE.items() if now - t > float(st["lease_seconds"])]:
            LEASE.pop(k, None)
        p = products(cfg)
        p = p[~p["id"].isin(fresh) & ~p["id"].isin(set(LEASE))]
        if p.empty:
            return {"wait": 3600, "why": "همه‌ی محصولات به‌تازگی بررسی شده‌اند"}
        # products held for a doubtful price first, then the dearer ones (a wrong price costs more there)
        if "status" not in p:
            p = p.assign(status="publish")
        p = p.assign(_w=(p["status"] != "pending")).sort_values(["_w", "our_toman"], ascending=[True, False])
        r = p.iloc[0]
        LEASE[r["id"]] = now
        from .names import clean_name
        return {"id": r["id"], "q": clean_name(str(r["name"]))[:120]}


def _record(cfg: dict, row: dict) -> None:
    df = checks(cfg)
    df = df[df["id"].astype(str) != str(row["id"])]
    _save(cfg, pd.concat([df, pd.DataFrame([row])], ignore_index=True))


def result(cfg: dict, body: dict) -> dict:
    from .detective import _same_model
    st = settings(cfg)
    pid = str(body.get("id") or "")
    p = products(cfg)
    hit = p[p["id"] == pid]
    if hit.empty:
        LEASE.pop(pid, None)
        return {"done": True, "verdict": "unknown_product"}
    ours = hit.iloc[0]
    base = {"id": pid, "name": ours["name"], "our_toman": str(int(ours["our_toman"])),
            "checked_at": time.strftime("%Y-%m-%d %H:%M"), "verdict": "", "ratio": "", "median_toman": "",
            "min_toman": "", "offers": "", "torob_name": "", "prk": "", "note": ""}
    if "offers" not in body:                                   # step 2: the search results
        rec = {"name": ours["name"]}
        for it in (body.get("items") or [])[:8]:
            name = str(it.get("name1") or it.get("name") or "")
            if name and _same_model(rec, name) and it.get("random_key"):
                with LOCK:
                    LEASE[pid] = time.time()
                return {"details": it["random_key"], "torob_name": name}
        with LOCK:
            _record(cfg, {**base, "verdict": "no_match", "note": f"{len(body.get('items') or [])} نتیجه، هیچ‌کدام همین مدل نبود"})
            LEASE.pop(pid, None)
        return {"done": True, "verdict": "no_match"}
    prices = []
    for o in body.get("offers") or []:                         # step 3: the offers of the matched product
        try:
            v = float(o.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if v > 0 and o.get("available", True) is not False:
            prices.append(v)
    row = {**base, "prk": str(body.get("prk") or ""), "torob_name": str(body.get("torob_name") or "")[:150],
           "offers": str(len(prices))}
    if len(prices) < int(st["min_offers"]):
        row.update(verdict="no_match", note="فروشنده‌ی کافی در ترب نیست")
    else:
        med, low = median(prices), min(prices)
        ratio = float(ours["our_toman"]) / med
        tol = float(st["tolerance"])
        row.update(median_toman=str(int(med)), min_toman=str(int(low)), ratio=f"{ratio:.2f}",
                   verdict="high" if ratio > 1 + tol else "low" if ratio < 1 - tol else "ok")
        if ratio > 5 or ratio < 0.2:
            row["note"] = "اختلاف خیلی زیاد؛ شاید محصول یا واحد قیمت فرق دارد"
    if str(ours.get("status")) == "pending" and row.get("median_toman") and st.get("auto_pending", True):
        row["note"] = (row["note"] + " | " if row["note"] else "") + apply_pending(cfg, pid, row, float(st["tolerance"]))
    with LOCK:
        _record(cfg, row)
        LEASE.pop(pid, None)
    log.info("TOROB %s %s ratio=%s offers=%s", pid, row["verdict"], row["ratio"], row["offers"])
    return {"done": True, "verdict": row["verdict"]}


def apply_pending(cfg: dict, pid: str, row: dict, tol: float, wc=None) -> str:
    """A product held for a doubtful price, now seen on Torob (the owner's rule, 31 Oct):
    (a) Torob's median matches our price -> published as it is;
    (b) it is exactly 10x or a tenth of ours (a rial/toman slip) -> Torob's median is set and published;
    anything else stays pending for a person. Returns what was done, for the report."""
    try:
        ratio = float(row["ratio"])
        med = float(row["median_toman"])
    except (KeyError, TypeError, ValueError):
        return "در انتظار ماند"
    data = None
    if 1 - tol <= ratio <= 1 + tol:
        data, what = {"status": "publish"}, "قیمت با ترب جور بود: منتشر شد"
    elif any(abs(ratio * k - 1) <= tol for k in (0.1, 10.0)):
        from .fx import today_rate
        usd = round(med / float(today_rate(cfg)["usd_rate_toman"]), 2)
        data = {"status": "publish", "regular_price": f"{usd:.2f}"}
        what = f"خطای ۱۰ برابری: قیمت به میانه‌ی ترب ({int(med):,} تومان) اصلاح و منتشر شد"
    else:
        return "در انتظار ماند (قیمت ترب فرق دارد)"
    try:
        from .wc import WC
        wc = wc or WC()
        if not wc.ok():
            return "در انتظار ماند (اتصال سایت تنظیم نیست)"
        wc.update_product(int(float(pid)), data)
    except Exception as e:                                   # noqa: BLE001 - the report still records it
        log.warning("TOROB pending %s not applied: %s", pid, e)
        return "در انتظار ماند (خطای سایت)"
    log.info("TOROB pending %s: %s", pid, what)
    return what


def summary(cfg: dict) -> dict:
    df = checks(cfg)
    p = products(cfg)
    return {"products": len(p), "checked": len(df), "today": _today_count(df),
            "verdicts": df["verdict"].value_counts().to_dict(), "cap": settings(cfg)["daily_cap"]}
