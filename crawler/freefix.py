"""Free photo fix, tried before a paid image model (29 Oct test: 8 of 20 photos the paid model had
fixed came out as good for free, and the paid model had drawn another product on 2 of them):

  1. the product cut out of the photo (U2-Net) and put on white: marks around it go with the background;
  2. small marks at the edges of the photo: painted white when they sit on white, else filled by LaMa.

A vision model first finds the marks' boxes (one cheap call). Every free result must pass three locks
before it is used, otherwise the photo goes on to the paid model as before:
  - the vision check finds no shop text/logo AND no faint watermark laid over the product (29 Oct, #16:
    a see-through logo over a tool case survived the cut-out and the plain check called it clean);
  - the background is white (a cluttered photo with a hand in it is not a store photo, #05);
  - the photo still holds at least 80% of what it showed (a fill that ate part of the product fails).
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import time

import numpy as np
import requests
from PIL import Image, ImageDraw

log = logging.getLogger(__name__)
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

BOX = ("This is a product photo for an online store. Find every mark ADDED on top of the photo by a shop: "
       "logos, watermarks, website addresses, phone numbers, prices, stamps, banner text. Do NOT include text "
       "printed on the product itself, and do NOT include the small camel logo in the bottom-right corner. "
       'Answer JSON only: {"boxes": [[x0, y0, x1, y1], ...]} with coordinates from 0 to 1000 (x right, y down). '
       'If there is none: {"boxes": []}')

ASK_FREE = ("You check a cleaned product photo for an online store. The small 'Caravan Sanat' camel logo in the "
            "bottom-right corner is the store's own watermark: ignore it. Product name: {name}\n"
            "Look closely, also for faint or see-through marks. Answer JSON only: {{"
            "\"overlay_text\": true/false (advert text, prices, phone numbers, web addresses or captions added over "
            "or around the photo; text printed on the product itself does not count), "
            "\"shop_logo\": true/false (another shop's logo or stamp anywhere except the bottom-right corner; the "
            "brand printed on the product is not a shop logo), "
            "\"faint_watermark\": true/false (a faint, semi-transparent or ghost logo, text or pattern laid over the "
            "product or the background), "
            "\"white_background\": true/false (plain white or near-white background, nothing else around the "
            "product: no hands, tables, boxes or scenery), "
            "\"product_whole\": true/false, \"matches_name\": true/false, \"note\": \"<few words>\"}}")

WM_BOX = [880, 900, 1000, 1000]          # our camel mark, bottom right (0-1000)


def data_url(im: Image.Image, side: int = 768) -> str:
    """The photo sent inline, made small: the check reads marks as well at 768 px and pays less."""
    im = im.convert("RGB")
    if max(im.size) > side:
        im = im.copy()
        im.thumbnail((side, side), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


def find_boxes(model: str, im: Image.Image, key: str) -> tuple[list[list[float]], float]:
    body = {"model": model, "max_tokens": 400, "temperature": 0, "reasoning": {"enabled": False},
            "messages": [{"role": "user", "content": [{"type": "text", "text": BOX},
                                                      {"type": "image_url", "image_url": {"url": data_url(im)}}]}]}
    try:
        r = requests.post(OPENROUTER, json=body, timeout=90, headers={"Authorization": f"Bearer {key}"})
        j = r.json()
        t = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        m = re.search(r"\{.*\}", t, re.S)
        bx = json.loads(m.group(0)).get("boxes", []) if m else []
    except Exception as e:                                   # noqa: BLE001 - no boxes: the cut-out still runs
        log.info("mark boxes: %s", e)
        return [], 0.0
    ok = []
    for b in bx if isinstance(bx, list) else []:
        if isinstance(b, list) and len(b) == 4 and all(isinstance(x, (int, float)) for x in b):
            x0, y0, x1, y1 = (max(0.0, min(1000.0, float(x))) for x in b)
            if x1 > x0 and y1 > y0:
                ok.append([x0, y0, x1, y1])
    return ok, float((j.get("usage") or {}).get("cost") or 0)


def safe_boxes(bx: list) -> tuple[bool, str]:
    """A fill only for small marks at the edges: each under 5% of the photo, all under 8%, no centre in
    the middle of the photo (where the product sits)."""
    if not bx:
        return False, "no marks found"
    areas = [(x1 - x0) * (y1 - y0) / 1e6 for x0, y0, x1, y1 in bx]
    if max(areas) > 0.05 or sum(areas) > 0.08:
        return False, f"mark too big ({max(areas):.0%})"
    for x0, y0, x1, y1 in bx:
        if 200 < (x0 + x1) / 2 < 800 and 200 < (y0 + y1) / 2 < 800:
            return False, "mark in the middle"
    return True, "small marks at the edges"


def ink(im: Image.Image, bx=()) -> float:
    """Share of the photo that is 'something' (not near-white), boxes left out."""
    a = np.asarray(im.convert("L").resize((250, 250)), dtype=np.uint8) < 235
    for x0, y0, x1, y1 in bx:
        a[int(y0 / 4):int(y1 / 4) + 1, int(x0 / 4):int(x1 / 4) + 1] = False
    return float(a.mean())


def white_fill(im: Image.Image, bx: list) -> Image.Image | None:
    """Marks on a white margin painted white; None when any mark's surroundings are not white."""
    W, H = im.size
    out = im.convert("RGB").copy()
    a = np.asarray(out)
    for x0, y0, x1, y1 in bx:
        X0, Y0, X1, Y1 = int(x0 / 1000 * W), int(y0 / 1000 * H), int(x1 / 1000 * W), int(y1 / 1000 * H)
        pad = 12
        ring = np.concatenate([a[max(0, Y0 - pad):Y0, X0:X1].reshape(-1, 3), a[Y1:Y1 + pad, X0:X1].reshape(-1, 3),
                               a[Y0:Y1, max(0, X0 - pad):X0].reshape(-1, 3), a[Y0:Y1, X1:X1 + pad].reshape(-1, 3)])
        if not len(ring) or (ring.min(axis=1) < 235).mean() > 0.03:
            return None
    dr = ImageDraw.Draw(out)
    p = 0.015
    for x0, y0, x1, y1 in bx:
        dr.rectangle([(x0 / 1000 - p) * W, (y0 / 1000 - p) * H, (x1 / 1000 + p) * W, (y1 / 1000 + p) * H], fill="white")
    return out


def lama(im: Image.Image, bx: list) -> Image.Image | None:
    from .images import _session
    sess = _session("lama")
    if sess is None:
        return None
    W, H = im.size
    mask = Image.new("L", (W, H), 0)
    dr = ImageDraw.Draw(mask)
    p = 0.02
    for x0, y0, x1, y1 in bx:
        dr.rectangle([(x0 / 1000 - p) * W, (y0 / 1000 - p) * H, (x1 / 1000 + p) * W, (y1 / 1000 + p) * H], fill=255)
    a = np.asarray(im.convert("RGB").resize((512, 512)), dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
    m = (np.asarray(mask.resize((512, 512)), dtype=np.float32) > 127).astype(np.float32)[None, None]
    o = sess.run(None, {sess.get_inputs()[0].name: a, sess.get_inputs()[1].name: m})[0][0]
    if o.max() <= 1.5:
        o = o * 255
    filled = Image.fromarray(np.clip(o.transpose(1, 2, 0), 0, 255).astype(np.uint8)).resize((W, H), Image.LANCZOS)
    res = im.convert("RGB").copy()
    res.paste(filled, (0, 0), mask)                          # only the marked parts change
    return res


def passes(v: dict) -> tuple[bool, str]:
    """The three locks on the vision verdict (the ink lock is checked apart)."""
    if v.get("_error"):
        return False, "check failed"
    if v.get("overlay_text") is True or v.get("shop_logo") is True:
        return False, "marks left"
    if v.get("faint_watermark") is True:
        return False, "faint watermark left"
    if v.get("white_background") is not True:
        return False, "background not white"
    if v.get("product_whole") is False or v.get("matches_name") is False:
        return False, "product cut or not shown"
    return True, "clean"


def free_fix(im: Image.Image, name: str, model: str, key: str, lock=None, check=None, size: int = 1000,
             ) -> tuple[Image.Image | None, str, float]:
    """(photo, how, cost): the fixed photo (1000 px, our watermark on) or None when no free way passes.
    `lock` guards the local models (not thread-safe); `check(model, img_url, name, key, ask)` is the vision
    call (crawler.vision.check)."""
    from contextlib import nullcontext
    from .images import flatten, remove_background, square, trim, watermark
    if check is None:
        from .vision import check
    lock = lock or nullcontext()
    im = im.convert("RGB")
    if max(im.size) > 1200:
        im.thumbnail((1200, 1200), Image.LANCZOS)
    cost = 0.0
    bx, c = find_boxes(model, im, key)
    cost += c
    before = ink(im, bx + [WM_BOX])
    why = []

    def finish(x):
        return watermark(square(trim(x), size))

    def ok(cand):
        nonlocal cost
        if ink(cand, [WM_BOX]) < 0.8 * before:
            return False, "part of the product lost"
        v = check(model, data_url(cand), name, key, ASK_FREE)
        cost += float(v.get("_cost") or 0)
        return passes(v)

    # 1. cut out
    base = im.copy()
    ImageDraw.Draw(base).rectangle([0.88 * im.width, 0.9 * im.height, im.width, im.height], fill="white")
    with lock:
        cut, did = remove_background(flatten(base))
        cand = finish(cut) if did else None
    if cand is not None:
        good, w = ok(cand)
        if good:
            return cand, "cutout", cost
        why.append("cut-out: " + w)
    # 2. white fill / LaMa
    sb, w = safe_boxes(bx)
    if not sb:
        why.append(w)
        return None, "; ".join(why), cost
    filled = white_fill(im, bx)
    how = "white"
    if filled is None:
        with lock:
            t = time.time()
            filled = lama(im, bx)
            log.debug("lama %.1fs", time.time() - t)
        how = "lama"
    if filled is None:
        why.append("no LaMa model")
        return None, "; ".join(why), cost
    with lock:
        cand = finish(filled)
    good, w = ok(cand)
    if good:
        return cand, how, cost
    why.append(f"{how}: {w}")
    return None, "; ".join(why), cost
