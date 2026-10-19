"""Review: products whose photos or price need a human look, and the decisions taken on them.

The review page lists, per run, the new products with a photo problem (still an advert or shop text,
background not made white, no photo at all) and the products whose price is an estimate.
For each one the reviewer can: accept it as it is, drop it from the import, keep only some of its
photos (and choose the main one), or type the right price. Decisions are kept in
05_review_decisions.csv and applied by the export step; nothing else is changed.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import run_dir
from .io import FILES, read_csv, write_csv

DEC_COLS = ["url", "action", "images", "price", "note"]
ISSUES = {"text_overlay": "عکس هنوز آگهی یا نوشته‌ی فروشگاه دارد",
          "ai_fixed": "عکس با هوش مصنوعی اصلاح شد (با اصلی مقایسه کنید)",
          "busy_background": "پس‌زمینه سفید نشد",
          "no_image": "بدون عکس"}


def _dec_path(d: Path) -> Path:
    return d / "05_review_decisions.csv"


def decisions(d: Path) -> dict[str, dict]:
    p = _dec_path(d)
    if not p.exists():
        return {}
    return {r["url"]: r for r in read_csv(p).to_dict("records")}


def save_decision(cfg: dict, run: str, body: dict) -> dict:
    d = run_dir(cfg, run)
    url = str(body.get("url") or "")
    if not url:
        return {"error": "url missing"}
    action = body.get("action") or "ok"
    if action not in ("ok", "drop", "reset"):
        return {"error": "bad action"}
    dec = decisions(d)
    if action == "reset":
        dec.pop(url, None)
    else:
        imgs = body.get("images")
        imgs = ",".join(str(int(x)) for x in imgs) if isinstance(imgs, list) else str(imgs or "")
        price = str(body.get("price") or "").replace(",", "").replace("٬", "").strip()
        if price and not price.isdigit():
            return {"error": "price must be a number (toman)"}
        dec[url] = {"url": url, "action": action, "images": imgs, "price": price, "note": str(body.get("note") or "")}
    write_csv(pd.DataFrame(list(dec.values()), columns=DEC_COLS), _dec_path(d))
    return {"ok": True, "count": len(dec)}


def _issues(notes: str) -> list[str]:
    t = {x.strip().split(":")[0] for x in str(notes or "").split("|")}
    return [k for k in ISSUES if k in t]


def items(cfg: dict, run: str, which: str = "issues") -> dict:
    """which: 'images' (photo problems), 'price' (estimated prices), 'issues' (either), 'all'."""
    d = run_dir(cfg, run)
    if not (d / FILES["new"]).exists():
        return {"items": [], "counts": {}}
    new = read_csv(d / FILES["new"])
    imgs = {r["url"]: r for r in read_csv(d / "05_images.csv").to_dict("records")} \
        if (d / "05_images.csv").exists() else {}
    dec = decisions(d)
    out, counts = [], {"images": 0, "price": 0, "reviewed": 0, "all": 0}
    for r in new.to_dict("records"):
        im = imgs.get(r["url"], {})
        iss = _issues(im.get("notes", "")) if im else []
        if im and not im.get("files"):
            iss = iss or ["no_image"]
        est = str(r.get("price_estimated") or "")
        counts["all"] += 1
        counts["images"] += bool(iss)
        counts["price"] += bool(est)
        counts["reviewed"] += r["url"] in dec
        if which == "images" and not iss or which == "price" and not est \
                or which == "issues" and not (iss or est):
            continue
        files = [f.strip() for f in str(im.get("files") or "").split("|") if f.strip()]
        originals = [f.strip() for f in str(im.get("originals") or "").split("|") if f.strip()]
        out.append({
            "url": r["url"], "name": r.get("name_fa") or r.get("name", ""), "name_src": r.get("name", ""),
            "category": str(r.get("breadcrumb") or "").split(">")[-1].strip(), "breadcrumb": r.get("breadcrumb", ""),
            "price": r.get("price_toman", ""), "estimated": est, "brand": r.get("brand", ""),
            "seller": r.get("source_domain", ""), "images": [f"/img/{f}" for f in files + originals],
            "default_keep": list(range(1, len(files) + 1)),
            "ai": [f"/img/{f}" for f in files if f.split("/")[-1].startswith("ai")],
            "source_images": [u for u in str(im.get("source_images") or "").split(" | ") if u],
            "issues": [ISSUES[k] for k in iss], "issue_keys": iss, "notes": im.get("notes", ""),
            "decision": dec.get(r["url"]),
        })
    return {"items": out, "counts": counts}


def apply(d: Path, rows: list[dict], imgs: dict[str, str]) -> tuple[list[dict], dict[str, str]]:
    """Export side: drop products, keep/reorder photos, set prices as decided."""
    dec = decisions(d)
    if not dec:
        return rows, imgs
    kept = []
    for r in rows:
        x = dec.get(r["url"])
        if not x:
            kept.append(r)
            continue
        if x.get("action") == "drop":
            continue
        if x.get("price"):
            r = {**r, "price_toman": x["price"], "price_estimated": "reviewed"}
        if x.get("images") and imgs.get(r["url"]):
            have = [u.strip() for u in imgs[r["url"]].split(",") if u.strip()]
            order = [int(n) for n in str(x["images"]).split(",") if n.strip().isdigit()]
            pick = [have[n - 1] for n in order if 0 < n <= len(have)]
            imgs = {**imgs, r["url"]: ", ".join(pick)}
        kept.append(r)
    return kept, imgs


def issues_file(cfg: dict, d: Path) -> Path | None:
    """06_image_issues.csv: photo problems not yet reviewed, for the final package."""
    if not (d / "05_images.csv").exists() or not (d / FILES["new"]).exists():
        return None
    res = items(cfg, d.name, "images")["items"]
    from .images import public_urls
    rows = [{"نام": x["name"], "دسته": x["breadcrumb"], "مشکل": " / ".join(x["issues"]),
             "عکس‌های فعلی": public_urls(cfg, " | ".join(i.replace("/img/", "") for i in x["images"])),
             "فروشگاه": x["seller"], "صفحه‌ی منبع": x["url"], "عکس‌های اصلی منبع": " , ".join(x["source_images"])}
            for x in res if not x["decision"]]
    return write_csv(pd.DataFrame(rows, columns=["نام", "دسته", "مشکل", "عکس‌های فعلی", "فروشگاه", "صفحه‌ی منبع",
                                                 "عکس‌های اصلی منبع"]), d / "06_image_issues.csv")
