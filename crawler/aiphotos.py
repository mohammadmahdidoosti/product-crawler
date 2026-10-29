"""After the image step: every photo is checked by a vision model, and the photos still carrying an
advert, phone number, web address or another shop's logo (or a busy background) are fixed:

  1. a product that also has a clean photo simply drops the bad one (free);
  2. otherwise a free fix is tried (crawler.freefix: cut-out, white fill or LaMa, strictly checked),
     then the bad photo is edited by an image model (Qwen Image Turbo, then the fallbacks);
  3. if both refuse, a clean photo of the same product is looked for (image search) and checked;
  4. what still fails is left as it is and listed for review.
AI-edited photos are saved next to the originals (ai<k>_<n>.webp); the originals are kept so the
review page can switch back. 05_vision.csv keeps every check; 05_images.csv is updated.
"""
from __future__ import annotations

import io
import re
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
            "cost", "error", "found_ok"]


def _bad(v: dict, busy: bool) -> bool:
    if v.get("_error"):
        return False
    if v.get("found_ok") is False:                 # a searched photo the strict check is not sure of
        return True
    return v.get("overlay_text") is True or v.get("shop_logo") is True or (busy and v.get("white_background") is False)


def worth_editing(v: dict) -> bool:
    """A paid edit removes marks from a photo of the product; it cannot make a product appear in a
    placeholder ('no image'), a factory or a spec table (both models were paid for one on 29 Oct)."""
    note = str(v.get("note") or "").lower()
    if v.get("matches_name") is False or "placeholder" in note or "no product" in note:
        return False
    return v.get("product_whole") is not False or v.get("matches_name") is True


def best_photo(files: list[str], verdict: dict) -> str:
    """Of a product's photos (all bad), the one worth the one paid fix: it is the product, the whole of
    it, on a plain background, with the fewest marks; on a tie the shop's own first photo. (Sharpness
    is not compared: every photo is already made 1000x1000 by the image step.)"""
    def rank(f):
        v = verdict.get(f) or {}
        return (v.get("matches_name") is not False, v.get("product_whole") is True,
                v.get("white_background") is True,
                -(int(v.get("overlay_text") is True) + int(v.get("shop_logo") is True)),
                -files.index(f))
    return max(files, key=rank)


def image_query(p: dict) -> str:
    """'دریل رونیکس 2513T' finds the maker's clean photo more often than a shop's whole title."""
    from .pipeline import _codes
    name = str(p.get("name_fa") or p.get("name") or "")
    codes = [c for c in _codes(name) if re.search(r"\d", c) and re.search(r"[a-z]", c)]
    brand = str(p.get("brand") or "").split("/")[0].strip()
    if codes and brand:
        return f"{name.split()[0]} {brand} {codes[0]}"
    return name


def _candidates(cfg: dict, f, p: dict, per: int):
    """Photos to try, cheapest first: the other shops' photos of the same product (merged rows: no
    search, nothing to pay), then an image search by kind + brand + model code, then by full name."""
    import json as _j
    try:
        own = [u for u in _j.loads(p.get("image_urls") or "[]") if isinstance(u, str)]
    except ValueError:
        own = []
    seen = set(own[:per])
    for u in own[per:]:
        seen.add(u)
        yield u, False
    from .images import search_images
    q = image_query(p)
    for qq in dict.fromkeys([q, str(p.get("name") or "")]):
        hits = search_images(cfg, f, {**p, "image_query": qq})
        for u in hits:
            if u not in seen:
                seen.add(u)
                yield u, True
        if hits:                                      # the full-name search only when the short one found nothing
            break


def fix_photos(cfg: dict, run: str) -> dict:
    from .enhance import edit, source_photo
    from .images import download, make, product_key, search_images
    from .vision import ASK_FOUND, check, found_ok
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

    # 0. photos dropped earlier as 'bad' come back to be judged again (the checker used to take our
    #    own watermark for a shop's logo); the bad ones are dropped again below
    for i, r in im.iterrows():
        if "bad_photo_dropped" in str(r.get("notes") or "") and str(r.get("originals") or "").strip():
            back = [f.strip() for f in str(r["originals"]).split("|")
                    if f.strip() and not f.strip().split("/")[-1].startswith("ai") and (root / f.strip()).exists()]
            files = [f.strip() for f in str(r.get("files") or "").split("|") if f.strip()]
            if back:
                im.at[i, "files"] = " | ".join(files + [f for f in back if f not in files])
                im.at[i, "originals"] = ""
                im.at[i, "notes"] = " | ".join(x.strip() for x in str(r["notes"]).split("|")
                                               if x.strip() and x.strip() != "bad_photo_dropped")

    # 0b. redo_found: photos found by search under the old loose check go back to the originals and
    #     are looked for again with the strict check
    if ic.get("redo_found"):
        for i, r in im.iterrows():
            nts = [x.strip() for x in str(r.get("notes") or "").split("|") if x.strip()]
            files = [f.strip() for f in str(r.get("files") or "").split("|") if f.strip()]
            if "found_by_search" in nts and files and files[0].split("/")[-1].startswith("ai9_") \
                    and str(r.get("originals") or "").strip():
                orig = str(r["originals"]).split("|")[0].strip()
                im.at[i, "files"] = " | ".join([orig] + files[1:])
                im.at[i, "originals"] = ""
                im.at[i, "notes"] = " | ".join(x for x in nts if x != "found_by_search")

    # 1. check every photo
    jobs, found = [], set()
    for r in im.to_dict("records"):
        fs = [x.strip() for x in str(r.get("files") or "").split("|") if x.strip()]
        for f in fs:
            p0 = new.get(r["url"], {})
            jobs.append((r["url"], f, p0.get("name_fa") or p0.get("name", "")))
        # the image step may have put a photo found by search first: it gets the strict check too
        if "found_by_search" in str(r.get("notes") or "") and fs:
            found.add((r["url"], fs[0]))
    # a photo checked before (same product, same file) is not sent again: re-runs check only new photos
    vp = d / "05_vision.csv"
    old = read_csv(vp) if vp.exists() else pd.DataFrame(columns=VIS_COLS)
    known = {}
    for o in old.to_dict("records"):
        if not str(o.get("error") or ""):
            known[(o["url"], o["file"])] = {**{k: (True if o.get(k) == "True" else False if o.get(k) == "False" else o.get(k))
                                              for k in VIS_COLS[2:8] + ["found_ok"]}, "_cost": 0}
    # an AI-edited photo judged bad may have been edited again since (same file name): check again;
    # a 'shop logo' with no text was mostly our own watermark (asked again with the clearer question)
    known = {k: v for k, v in known.items() if not (k[1].split("/")[-1].startswith("ai") and _bad(v, True))
             and not (v.get("shop_logo") is True and v.get("overlay_text") is not True)}
    known = {k: v for k, v in known.items() if k not in found or v.get("found_ok") in (True, False)}
    todo = [j for j in jobs if (j[0], j[1]) not in known]
    log.info("photo check: %d photos with %s (%d checked before)", len(todo), vmodel, len(jobs) - len(todo))
    with ThreadPoolExecutor(int(ic.get("vision_workers", 24))) as ex:
        def look(j):
            if (j[0], j[1]) not in found:
                return check(vmodel, f"{base}/img/{j[1]}", j[2], key)
            v = check(vmodel, f"{base}/img/{j[1]}", j[2], key, ASK_FOUND)
            return {**v, "found_ok": found_ok(v)} if not v.get("_error") else v
        fresh = list(ex.map(look, todo))
    vis = {**known, **{(j[0], j[1]): v for j, v in zip(todo, fresh)}}
    res = fresh
    redo = {(j[0], j[1]) for j in todo}
    keep_old = old[[(u, f) not in redo for u, f in zip(old["url"], old["file"])]] if len(old) else old
    write_csv(pd.concat([keep_old, pd.DataFrame([{"url": j[0], "file": j[1], **{k: v.get(k, "") for k in VIS_COLS[2:8]},
                                                   "cost": v.get("_cost", ""), "error": v.get("_error", ""),
                                                   "found_ok": v.get("found_ok", "")}
                                                  for j, v in zip(todo, fresh)], columns=VIS_COLS)], ignore_index=True),
              vp)

    # 2-4. fix
    models = [ic.get("ai_model", "qwen/qwen-image-2.1-turbo")] + list(ic.get("ai_fallback_models") or [])
    stats = {"checked": len(jobs), "bad": 0, "dropped": 0, "edited": 0, "searched": 0, "left": 0,
             "wrong": 0, "wrong_left": 0, "search_tried": 0, "edit_tried": 0, "free_tried": 0, "free": 0,
             "free_cost": 0.0}
    rows = im.to_dict("records")
    import threading
    lock = threading.Lock()
    model_lock = threading.Lock()

    rewritten: set[tuple[str, str]] = set()

    def bump(k, n=1):
        with lock:
            stats[k] += n

    from .review import decisions
    dropped = {u for u, x in decisions(d).items() if x.get("action") == "drop"}

    def fix_one(r):
        files = [x.strip() for x in str(r.get("files") or "").split("|") if x.strip()]
        if not files or r["url"] in dropped:         # a product taken out on review gets no paid fix
            return
        busy = "busy_background" in str(r.get("notes") or "")
        # a photo of something else (a supplier's factory, a spec table, another product) goes when
        # the product has another photo; when all are like that the product is marked for a look
        wrong = [f for f in files if vis.get((r["url"], f), {}).get("matches_name") is False]
        if wrong and len(wrong) < len(files):
            files = [f for f in files if f not in wrong]
            r["files"] = " | ".join(files)
            r["originals"] = " | ".join(x for x in (str(r.get("originals") or ""), " | ".join(wrong)) if x.strip())
            r["notes"] = " | ".join(sorted(set(x.strip() for x in str(r.get("notes") or "").split("|") if x.strip())
                                           | {"wrong_photo_dropped"}))
            bump("wrong", len(wrong))
        elif wrong:
            r["notes"] = " | ".join(sorted(set(x.strip() for x in str(r.get("notes") or "").split("|") if x.strip())
                                           | {"wrong_photo"}))
            bump("wrong_left", len(wrong))
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
        else:                                                # 2. a photo from another shop, 3. an AI edit
            # the one photo to fix is the best of them, not just the first (the rest go)
            pick = best_photo(files, {f: vis.get((r["url"], f), {}) for f in files})
            files = [pick] + [f for f in files if f != pick]
            k = product_key(r["url"])
            n = int(re.findall(r"\d+", files[0].split("/")[-1].split(".")[0])[-1])   # '2.webp', 'ai1_2.webp
            src = source_photo(r, n, cfg["_data_dir"])
            fixed = ""
            if any(x.startswith("ai_fixed") for x in notes):   # edited before and still not clean: no second edit
                src = None
            if not worth_editing(vis.get((r["url"], files[0]), {})):   # an edit cannot make the product appear
                src = None
                notes.add("no_product_in_photo")
            # 2a. free first: the product cut out, or small edge marks painted white / filled by LaMa;
            #     used only when the strict check passes it (else on to search and the paid models)
            if not fixed and src is not None and ic.get("free_fix", True) \
                    and not any(x.startswith("free_fixed") for x in notes):
                from .freefix import free_fix
                bump("free_tried")
                ours = not (0 < n <= len([u for u in str(r.get("source_images") or "").split(" | ") if u]))
                out, how, c = free_fix(src, new.get(r["url"], {}).get("name", ""), vmodel, key, lock=model_lock,
                                       check=check, size=int(ic.get("size", 1000)), ours=ours)
                bump("free_cost", c)
                if out is not None:
                    rel = f"{k}/ai0_{n}.webp"
                    out.save(root / rel, "WEBP", quality=88, method=4)
                    fixed = rel
                    notes.add(f"free_fixed:{how}")
                    bump("free")
                else:
                    log.info("photo fix %s free: %s", r["url"][-40:], how[:80])
            if not fixed and int(ic.get("search_max", 40)) > 0:   # 2. a clean photo from another shop first:
                bump("search_tried")                              # a real photo, and cheaper than an AI edit
                from .pipeline import _fetchers
                f = _fetchers(cfg)["iran"]
                looked = 0
                for u, paid in _candidates(cfg, f, new.get(r["url"], {}), int(ic.get("per_product", 3))):
                    if looked >= int(ic.get("search_checks", 4)):   # each look is a vision call: past 4 an
                        break                                      # AI edit is the cheaper way
                    data = download(f, u, min_side=350)
                    if not data:
                        continue
                    with Image.open(io.BytesIO(data)) as one:
                        with model_lock:
                            pic, _ = make(one, None, int(ic.get("size", 1000)))
                    rel = f"{k}/ai9_{n}.webp"
                    pic.save(root / rel, "WEBP", quality=85, method=4)
                    v = check(vmodel, f"{base}/img/{rel}", new.get(r["url"], {}).get("name", ""), key, ASK_FOUND)
                    looked += 1
                    if found_ok(v):                      # strict: unsure is a no (batch 5: 8 of 44 were wrong)
                        fixed = rel
                        notes.add("found_by_search" if paid else "found_other_source")
                        bump("searched")
                        break
            if not fixed and src is not None:
                bump("edit_tried")
            for mi, m in enumerate(models, 1):
                if fixed or src is None:
                    break
                out, status = edit(m, src, key)
                if out is None:
                    log.info("photo fix %s with %s: %s", r["url"][-40:], m, status[:80])
                    continue
                with model_lock:                         # the local image models are not thread-safe
                    pic, _ = make(out, None, int(ic.get("size", 1000)), cutout=False)
                rel = f"{k}/ai{mi}_{n}.webp"
                pic.save(root / rel, "WEBP", quality=88, method=4)
                # the edit is looked at before it is used: an edit of a 'فروخته شد' stamp is still a stamp
                v = check(vmodel, f"{base}/img/{rel}", new.get(r["url"], {}).get("name", ""), key)
                if _bad(v, False) or v.get("matches_name") is False:
                    log.info("photo fix %s with %s: the edit still fails the check (%s)", r["url"][-40:], m,
                             str(v.get("note") or "")[:60])
                    continue
                fixed = rel
                notes.add(f"ai_fixed:{m.split('/')[-1]}")
                break
            if fixed:
                r["files"] = " | ".join([fixed] + [f for f in files[1:] if f not in bad])
                with lock:
                    rewritten.add((r["url"], fixed))             # same file name, new picture: check it again
                r["originals"] = files[0]
                bump("edited", int(fixed.split("/")[-1].startswith("ai") and "found_by_search" not in notes))
            else:
                notes.add("text_overlay")
                bump("left")
        r["notes"] = " | ".join(sorted(notes))
    # products are fixed side by side: an image-model edit takes 20-40 s each
    def safe(r):
        try:
            fix_one(r)
        except Exception as e:                       # noqa: BLE001 - one product must not stop the rest
            log.warning("photo fix %s: %s", r.get("url", "")[-50:], e)
    with ThreadPoolExecutor(int(ic.get("fix_workers", 6))) as ex:
        list(ex.map(safe, rows))
    cols = list(im.columns)
    write_csv(pd.DataFrame(rows, columns=cols), p)
    if rewritten and vp.exists():                        # the old verdict on a rewritten file is stale
        vv = read_csv(vp)
        write_csv(vv[[(u, f) not in rewritten for u, f in zip(vv["url"], vv["file"])]], vp)
    cost = sum(float(v.get("_cost") or 0) for v in res)
    log.info("photo check: %s; vision cost $%.3f", stats, cost)
    return stats
