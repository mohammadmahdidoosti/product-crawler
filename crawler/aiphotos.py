"""After the image step: every photo is checked by a vision model, and the photos still carrying an
advert, phone number, web address or another shop's logo (or a busy background) are fixed:

  1. a product that also has a clean photo simply drops the bad one (free);
  2. otherwise the bad photo is edited by an image model (Qwen Image Turbo, then Krea K3 Edit);
  3. if both refuse, a clean photo of the same product is looked for (image search) and checked;
  4. what still fails is left as it is and listed for review.
AI-edited photos are saved next to the originals (ai<k>_<n>.webp); the originals are kept so the
review page can switch back. 05_vision.csv keeps every check; 05_images.csv is updated.
"""
from __future__ import annotations

import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from PIL import Image

from .config import run_dir, secret
from .io import read_csv, write_csv

log = logging.getLogger(__name__)
VIS_COLS = ["url", "file", "overlay_text", "shop_logo", "white_background", "product_whole", "matches_name", "note",
            "cost", "error"]


def _bad(v: dict, busy: bool) -> bool:
    if v.get("_error"):
        return False
    return v.get("overlay_text") is True or v.get("shop_logo") is True or (busy and v.get("white_background") is False)


def fix_photos(cfg: dict, run: str) -> dict:
    from .enhance import edit, source_photo
    from .images import download, make, product_key, search_images
    from .vision import check
    ic = cfg.get("images", {})
    key = secret("OPENROUTER_API_KEY")
    if not key or not ic.get("ai_fix", True):
        log.info("photo check: off (no OPENROUTER_API_KEY or images.ai_fix false)")
        return {}
    d = run_dir(cfg, run)
    p = d / "05_images.csv"
    if not p.exists():
        return {}
    im = read_csv(p)
    for c in ("originals",):
        if c not in im.columns:
            im[c] = ""
    new = {r["url"]: r for r in read_csv(d / "05_new_products.csv").to_dict("records")}
    base = (ic.get("public_base") or os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    vmodel = ic.get("vision_model", "deepseek/deepseek-v4.1-flash")
    root = cfg["_data_dir"] / "images"

    # 1. check every photo
    jobs = []
    for r in im.to_dict("records"):
        for f in [x.strip() for x in str(r.get("files") or "").split("|") if x.strip()]:
            p0 = new.get(r["url"], {})
            jobs.append((r["url"], f, p0.get("name_fa") or p0.get("name", "")))
    # a photo checked before (same product, same file) is not sent again: re-runs check only new photos
    vp = d / "05_vision.csv"
    old = read_csv(vp) if vp.exists() else pd.DataFrame(columns=VIS_COLS)
    known = {}
    for o in old.to_dict("records"):
        if not str(o.get("error") or ""):
            known[(o["url"], o["file"])] = {**{k: (True if o.get(k) == "True" else False if o.get(k) == "False" else o.get(k))
                                              for k in VIS_COLS[2:8]}, "_cost": 0}
    todo = [j for j in jobs if (j[0], j[1]) not in known]
    log.info("photo check: %d photos with %s (%d checked before)", len(todo), vmodel, len(jobs) - len(todo))
    with ThreadPoolExecutor(int(ic.get("vision_workers", 24))) as ex:
        fresh = list(ex.map(lambda j: check(vmodel, f"{base}/img/{j[1]}", j[2], key), todo))
    vis = {**known, **{(j[0], j[1]): v for j, v in zip(todo, fresh)}}
    res = fresh
    redo = {(j[0], j[1]) for j in todo}
    keep_old = old[[(u, f) not in redo for u, f in zip(old["url"], old["file"])]] if len(old) else old
    write_csv(pd.concat([keep_old, pd.DataFrame([{"url": j[0], "file": j[1], **{k: v.get(k, "") for k in VIS_COLS[2:8]},
                                                   "cost": v.get("_cost", ""), "error": v.get("_error", "")}
                                                  for j, v in zip(todo, fresh)], columns=VIS_COLS)], ignore_index=True),
              vp)

    # 2-4. fix
    models = [ic.get("ai_model", "qwen/qwen-image-2.1-turbo")] + list(ic.get("ai_fallback_models") or [])
    stats = {"checked": len(jobs), "bad": 0, "dropped": 0, "edited": 0, "searched": 0, "left": 0}
    rows = im.to_dict("records")
    import threading
    lock = threading.Lock()

    def bump(k, n=1):
        with lock:
            stats[k] += n

    def fix_one(r):
        files = [x.strip() for x in str(r.get("files") or "").split("|") if x.strip()]
        if not files:
            return
        busy = "busy_background" in str(r.get("notes") or "")
        bad = [f for f in files if _bad(vis.get((r["url"], f), {}), busy)]
        checked = all(not vis.get((r["url"], f), {}).get("_error") for f in files)
        if not bad:
            if checked and r.get("notes"):              # the check found nothing: old flags go
                r["notes"] = " | ".join(x for x in str(r["notes"]).split(" | ")
                                        if x.strip() not in ("text_overlay", "busy_background"))
            return
        bump("bad", len(bad))
        notes = set(x.strip() for x in str(r.get("notes") or "").split("|") if x.strip()) - {"text_overlay", "busy_background"}
        clean = [f for f in files if f not in bad]
        if clean:                                            # 1. another photo is fine: drop the bad ones
            r["files"] = " | ".join(clean)
            r["originals"] = " | ".join(bad)
            notes.add("bad_photo_dropped")
            bump("dropped", len(bad))
        else:                                                # 2. edit the main photo
            k = product_key(r["url"])
            n = int(files[0].split("/")[-1].split(".")[0])
            src = source_photo(r, n, cfg["_data_dir"])
            fixed = ""
            for mi, m in enumerate(models, 1):
                if src is None:
                    break
                out, status = edit(m, src, key)
                if out is None:
                    log.info("photo fix %s with %s: %s", r["url"][-40:], m, status[:80])
                    continue
                pic, _ = make(out, None, int(ic.get("size", 1000)), cutout=False)
                rel = f"{k}/ai{mi}_{n}.webp"
                pic.save(root / rel, "WEBP", quality=88, method=4)
                fixed = rel
                notes.add(f"ai_fixed:{m.split('/')[-1]}")
                break
            if not fixed and int(ic.get("search_max", 40)) > 0:   # 3. a clean photo from elsewhere
                from .pipeline import _fetchers
                f = _fetchers(cfg)["iran"]
                for u in search_images(cfg, f, {**new.get(r["url"], {}), "name": new.get(r["url"], {}).get("name", "")}):
                    data = download(f, u, min_side=350)
                    if not data:
                        continue
                    with Image.open(io.BytesIO(data)) as one:
                        pic, _ = make(one, None, int(ic.get("size", 1000)))
                    rel = f"{k}/ai9_{n}.webp"
                    pic.save(root / rel, "WEBP", quality=85, method=4)
                    v = check(vmodel, f"{base}/img/{rel}", new.get(r["url"], {}).get("name", ""), key)
                    if not _bad(v, False) and v.get("matches_name") is not False:
                        fixed = rel
                        notes.add("found_by_search")
                        bump("searched")
                        break
            if fixed:
                r["files"] = " | ".join([fixed] + files[1:])
                r["originals"] = files[0]
                bump("edited", int(fixed.split("/")[-1].startswith("ai") and "found_by_search" not in notes))
            else:
                notes.add("text_overlay")
                bump("left")
        r["notes"] = " | ".join(sorted(notes))
    # products are fixed side by side: an image-model edit takes 20-40 s each
    with ThreadPoolExecutor(int(ic.get("fix_workers", 6))) as ex:
        list(ex.map(fix_one, rows))
    cols = list(im.columns)
    write_csv(pd.DataFrame(rows, columns=cols), p)
    cost = sum(float(v.get("_cost") or 0) for v in res)
    log.info("photo check: %s; vision cost $%.3f", stats, cost)
    return stats
