"""Photo check with a vision model (reads the photo, answers in JSON): does the final photo still carry an
advert, phone number, web address or a shop's logo/watermark, is the background white, is the whole
product visible, and does it show the product named? Used to find photos to fix; it changes nothing."""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

from .config import run_dir, secret
from .io import read_csv

log = logging.getLogger(__name__)
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
ASK = ("You check product photos for an online store. The small 'Caravan Sanat' camel logo in the bottom-right "
       "corner is the store's own watermark: ignore it. Product name: {name}\n"
       "Answer JSON only: {{\"overlay_text\": true/false (advert text, prices, phone numbers, web addresses or "
       "captions added over or around the photo; text printed on the product itself does not count), "
       "\"shop_logo\": true/false (another shop's logo or watermark ANYWHERE EXCEPT the bottom-right corner; "
       "anything in the bottom-right corner is our own watermark and is never a shop logo; the brand printed "
       "on the product itself is not a shop logo either), \"white_background\": true/false, "
       "\"product_whole\": true/false (the whole product is visible, not cut off), \"matches_name\": true/false "
       "(the photo shows the product named), \"note\": \"<few words>\"}}")


# a photo found by search is someone else's photo of maybe another thing: it must prove itself
ASK_FOUND = ("You check a photo found on another shop's site before it is used for this product in our store. "
             "Product name: {name}\n"
             "Be strict: when unsure, answer against the photo. Answer JSON only: {{"
             "\"overlay_text\": true/false (any added text, price, phone, web address, caption, banner or label "
             "strip over or around the photo; text printed on the product itself does not count), "
             "\"shop_logo\": true/false (any shop's logo or watermark anywhere), "
             "\"frame\": true/false (a border, frame, coloured band, badge or other added graphic), "
             "\"diagram\": true/false (an exploded view, parts drawing, line drawing, spec sheet or collage "
             "instead of a plain photo of one product), "
             "\"white_background\": true/false, \"product_whole\": true/false, "
             "\"is_part\": true/false (the product NAMED is a spare part or accessory of a tool, e.g. a brush "
             "holder, switch, armature, blade, gear), "
             "\"shows_whole_tool\": true/false (the photo shows a whole tool or machine), "
             "\"matches_name\": true/false (true only if the photo clearly shows exactly the product named: same "
             "kind of item, same brand and model when they are visible; a part named must be shown as that part "
             "alone, not the tool it belongs to), \"note\": \"<few words>\"}}")


def found_ok(v: dict) -> bool:
    """A searched photo is used only when the strict check is sure of it."""
    if v.get("_error"):
        return False
    if any(v.get(k) is True for k in ("overlay_text", "shop_logo", "frame", "diagram")):
        return False
    if v.get("white_background") is False or v.get("product_whole") is False:
        return False
    if v.get("is_part") is True and v.get("shows_whole_tool") is True:
        return False
    return v.get("matches_name") is True


def check(model: str, img_url: str, name: str, key: str, ask: str = ASK) -> dict:
    body = {"model": model, "max_tokens": 300, "temperature": 0, "reasoning": {"enabled": False},
            "messages": [{"role": "user", "content": [{"type": "text", "text": ask.format(name=name)},
                                                      {"type": "image_url", "image_url": {"url": img_url}}]}]}
    for attempt in range(2):
        if attempt and "reasoning" in body:                  # some models cannot switch thinking off
            pass
        try:
            r = requests.post(OPENROUTER, json=body, timeout=90, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "HTTP-Referer": "https://caravansanat.com", "X-Title": "Caravan crawler"})
        except requests.RequestException as e:
            err = str(e)
            time.sleep(3)
            continue
        if r.status_code != 200:
            err = f"HTTP {r.status_code}: {r.text[:150]}"
            if "Reasoning is mandatory" in r.text:
                body.pop("reasoning", None)
                body["max_tokens"] = 2000
                continue
            time.sleep(3)
            continue
        j = r.json()
        text = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        m = re.search(r"\{.*\}", text, re.S)
        try:
            out = json.loads(m.group(0)) if m else {}
        except ValueError:
            out = {}
        note = str(out.get("note") or "").lower()
        if out.get("shop_logo") is True and re.search(r"caravan|camel|bottom[- ]right|کاروان", note):
            out["shop_logo"] = False                     # our own watermark, named in the model's note
        out["_cost"] = (j.get("usage") or {}).get("cost", "")
        return out or {"_error": "unparsable: " + text[:100]}
    return {"_error": err}


def compare(cfg: dict, run: str, models: list[str], n_flagged: int = 25, n_clean: int = 25) -> Path:
    key = secret("OPENROUTER_API_KEY")
    d = run_dir(cfg, run)
    im = read_csv(d / "05_images.csv")
    new = {r["url"]: r for r in read_csv(d / "05_new_products.csv").to_dict("records")}
    im = im[im["files"] != ""]
    flagged = im[im["notes"].str.contains("text_overlay|busy_background")].head(n_flagged)
    clean = im[~im["notes"].str.contains("text_overlay|busy_background")].sample(min(n_clean, len(im)), random_state=1)
    env = __import__("os").environ
    base = (cfg.get("images", {}).get("public_base") or env.get("PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
    jobs = []
    for grp, part in (("flagged", flagged), ("clean", clean)):
        for r in part.to_dict("records"):
            f = r["files"].split("|")[0].strip()
            p = new.get(r["url"], {})
            jobs.append({"group": grp, "url": r["url"], "img": f"{base}/img/{f}",
                         "name": p.get("name_fa") or p.get("name", ""), "notes": r["notes"]})
    rows = []
    for m in models:
        t0 = time.time()
        with ThreadPoolExecutor(6) as ex:
            res = list(ex.map(lambda j: check(m, j["img"], j["name"], key), jobs))
        for j, x in zip(jobs, res):
            rows.append({**j, "model": m, **{k: x.get(k, "") for k in
                         ("overlay_text", "shop_logo", "white_background", "product_whole", "matches_name", "note",
                          "_cost", "_error")}})
        log.info("vision %s: %d photos in %.0fs, %d errors", m, len(jobs), time.time() - t0,
                 sum(1 for x in res if x.get("_error")))
    p = d / "06_vision_compare.csv"
    pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8-sig")
    return p
