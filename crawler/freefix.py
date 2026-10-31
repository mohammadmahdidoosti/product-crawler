"""Free photo fix, tried before a paid image model (29 Oct test: 8 of 20 photos the paid model had
fixed came out as good for free, and the paid model had drawn another product on 2 of them):

  1. the product cut out of the photo (U2-Net) and put on white: marks around it go with the background;
  2. small marks at the edges of the photo: painted white when they sit on white, else filled by LaMa.

A vision model first finds the marks' boxes (one cheap call). A cut-out is not used when a mark's box is
still full after it (the mark sits on the product). Every free result must pass three locks before it is
used, otherwise the photo goes on to the paid model as before:
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
    for (x0, y0, x1, y1), a in zip(bx, areas):
        # a small mark on the product (a shop's sticker on a handle, 30 Oct #13) is filled locally;
        # a bigger one in the middle is the product itself or too much to invent
        if 200 < (x0 + x1) / 2 < 800 and 200 < (y0 + y1) / 2 < 800 and a > 0.015:
            return False, "mark in the middle"
    return True, "small marks"


def text_strips(bx: list) -> bool:
    """Every mark is a thin line (a shop's name or web address written across the product: 30 Oct #17,
    #36): filled in its own crop, even over the product; a big block (a see-through logo) is not."""
    if not bx:
        return False
    for x0, y0, x1, y1 in bx:
        h, w = (y1 - y0) / 1000, (x1 - x0) / 1000
        if h > 0.08 or h * w > 0.15 or w < 1.5 * h:
            return False
    return sum((x1 - x0) * (y1 - y0) / 1e6 for x0, y0, x1, y1 in bx) <= 0.2


def ink(im: Image.Image, bx=()) -> float:
    """Share of the photo that is 'something' (not near-white), boxes left out."""
    a = np.asarray(im.convert("L").resize((250, 250)), dtype=np.uint8) < 235
    for x0, y0, x1, y1 in bx:
        a[int(y0 / 4):int(y1 / 4) + 1, int(x0 / 4):int(x1 / 4) + 1] = False
    return float(a.mean())


def left_in_boxes(im: Image.Image, bx: list) -> float:
    """After the cut-out: the largest share of 'something' still inside a mark's box. A mark sitting on
    the product survives the cut-out (29 Oct, #17: a shop's orange 'K' on a heat gun's handle passed the
    vision check), so a box that is still full means the cut-out did not remove that mark."""
    if not bx:
        return 0.0
    W, H = im.size
    g = np.asarray(im.convert("L"), dtype=np.uint8) < 235
    worst = 0.0
    for x0, y0, x1, y1 in bx:
        a = g[int(y0 / 1000 * H):int(y1 / 1000 * H) + 1, int(x0 / 1000 * W):int(x1 / 1000 * W) + 1]
        if a.size:
            worst = max(worst, float(a.mean()))
    return worst


def _ring_dirty(a: np.ndarray, b: list, W: int, H: int, pad: int = 12) -> float:
    """Share of non-white pixels in a thin ring just outside box b (0-1000 units)."""
    X0, Y0 = max(0, int(b[0] / 1000 * W)), max(0, int(b[1] / 1000 * H))
    X1, Y1 = min(W, int(b[2] / 1000 * W)), min(H, int(b[3] / 1000 * H))
    ring = np.concatenate([a[max(0, Y0 - pad):Y0, X0:X1].reshape(-1, 3), a[Y1:Y1 + pad, X0:X1].reshape(-1, 3),
                           a[Y0:Y1, max(0, X0 - pad):X0].reshape(-1, 3), a[Y0:Y1, X1:X1 + pad].reshape(-1, 3)])
    if not len(ring):
        return 0.0                                           # the box reaches the photo's edges
    return float((ring.min(axis=1) < 235).mean())


def grow(a: np.ndarray, b: list, W: int, H: int, steps: int = 6, by: float = 15) -> list | None:
    """The finder's box runs small at times (29 Oct, #14: half a logo was left). The box is grown until
    the ring around it is white; None when it never is (the mark touches the product or a busy part)."""
    for k in range(steps + 1):
        g = [max(0, b[0] - k * by), max(0, b[1] - k * by), min(1000, b[2] + k * by), min(1000, b[3] + k * by)]
        if _ring_dirty(a, g, W, H) <= 0.03:
            return g
    return None


def white_fill(im: Image.Image, bx: list) -> Image.Image | None:
    """Marks on a white margin painted white (each box grown until white surrounds it); None when any
    mark's surroundings are not white."""
    W, H = im.size
    out = im.convert("RGB").copy()
    a = np.asarray(out)
    grown = [grow(a, b, W, H) for b in bx]
    if any(g is None for g in grown):
        return None
    dr = ImageDraw.Draw(out)
    p = 0.01
    for x0, y0, x1, y1 in grown:
        dr.rectangle([(x0 / 1000 - p) * W, (y0 / 1000 - p) * H, (x1 / 1000 + p) * W, (y1 / 1000 + p) * H], fill="white")
    return out


def lama(im: Image.Image, bx: list) -> Image.Image | None:
    """LaMa fills the marks; None when there is no model, or when a filled part comes out much 'fuller'
    than what is around it (a patch of the mark copied back in)."""
    from .images import _session
    sess = _session("lama")
    if sess is None:
        return None
    W, H = im.size
    mask = Image.new("L", (W, H), 0)
    dr = ImageDraw.Draw(mask)
    p = 0.035                                                # the finder's boxes run a little small
    big = [[b[0] - p * 1000, b[1] - p * 1000, b[2] + p * 1000, b[3] + p * 1000] for b in bx]
    a0 = np.asarray(im.convert("RGB"))
    # the mask's edge must run round the mark, not through it (29 Oct, #14: a ring 45% 'full' meant
    # half the logo lay outside the box, and LaMa drew the rest of it back in); 0, 3: under 10%
    if any(_ring_dirty(a0, g, W, H) > 0.25 for g in big):
        return None
    for x0, y0, x1, y1 in big:
        dr.rectangle([x0 / 1000 * W, y0 / 1000 * H, x1 / 1000 * W, y1 / 1000 * H], fill=255)
    a = np.asarray(im.convert("RGB").resize((512, 512)), dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
    m = (np.asarray(mask.resize((512, 512)), dtype=np.float32) > 127).astype(np.float32)[None, None]
    o = sess.run(None, {sess.get_inputs()[0].name: a, sess.get_inputs()[1].name: m})[0][0]
    if o.max() <= 1.5:
        o = o * 255
    filled = Image.fromarray(np.clip(o.transpose(1, 2, 0), 0, 255).astype(np.uint8)).resize((W, H), Image.LANCZOS)
    res = im.convert("RGB").copy()
    res.paste(filled, (0, 0), mask)                          # only the marked parts change
    r = np.asarray(res)
    for b in big:
        X0, Y0 = max(0, int(b[0] / 1000 * W)), max(0, int(b[1] / 1000 * H))
        X1, Y1 = min(W, int(b[2] / 1000 * W)), min(H, int(b[3] / 1000 * H))
        inside = r[Y0:Y1, X0:X1].reshape(-1, 3)
        if len(inside) and float((inside.min(axis=1) < 235).mean()) > _ring_dirty(r, b, W, H) + 0.15:
            return None
    return res


def lama_local(im: Image.Image, bx: list) -> Image.Image | None:
    """Each mark filled by LaMa in a crop around it (about 3x the mark), at the photo's own detail; only
    the mark's pixels change. Used when the whole-photo fill is refused or for marks on the product."""
    from .images import _session
    sess = _session("lama")
    if sess is None:
        return None
    res = im.convert("RGB").copy()
    W, H = res.size
    for b in bx:
        p = 0.02 * 1000                                       # the finder's boxes run a little small
        x0, y0, x1, y1 = (max(0, b[0] - p) / 1000 * W, max(0, b[1] - p) / 1000 * H,
                          min(1000, b[2] + p) / 1000 * W, min(1000, b[3] + p) / 1000 * H)
        bw, bh = x1 - x0, y1 - y0
        side = int(max(bw, bh) * 3) + 32
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        C0, D0 = int(max(0, min(W - side, cx - side / 2))), int(max(0, min(H - side, cy - side / 2)))
        C1, D1 = min(W, C0 + side), min(H, D0 + side)
        crop = res.crop((C0, D0, C1, D1))
        cw, ch = crop.size
        mask = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(mask).rectangle([x0 - C0, y0 - D0, x1 - C0, y1 - D0], fill=255)
        a = np.asarray(crop.resize((512, 512), Image.LANCZOS), dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        m = (np.asarray(mask.resize((512, 512)), dtype=np.float32) > 127).astype(np.float32)[None, None]
        o = sess.run(None, {sess.get_inputs()[0].name: a, sess.get_inputs()[1].name: m})[0][0]
        if o.max() <= 1.5:
            o = o * 255
        filled = Image.fromarray(np.clip(o.transpose(1, 2, 0), 0, 255).astype(np.uint8)).resize((cw, ch), Image.LANCZOS)
        crop.paste(filled, (0, 0), mask)
        res.paste(crop, (C0, D0))
    return res


SAME = ("Two photos of the same product for an online store. In the second, a shop's ADDED overlay (a web "
        "address, a company name or a logo laid over the photo) was REMOVED ON PURPOSE: that removal is expected "
        "and is NOT damage. Compare everything else. Answer JSON only: {\"damaged\": true/false, \"note\": "
        "\"<few words>\"} — damaged is true only when the product itself changed: a part of it missing, smeared, "
        "blurred or redrawn, or text/brand PRINTED ON the product or its packaging cut or erased.")


def unchanged(model: str, before: Image.Image, after: Image.Image, key: str) -> tuple[bool, float, str]:
    """A local fill over the product is compared with the original (30 Oct #33: a web address removed from a
    blister pack took half of the pack's own brand with it, and the plain check passed it)."""
    body = {"model": model, "max_tokens": 120, "temperature": 0, "reasoning": {"enabled": False},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": SAME},
                {"type": "image_url", "image_url": {"url": data_url(before)}},
                {"type": "image_url", "image_url": {"url": data_url(after)}}]}]}
    try:
        j = requests.post(OPENROUTER, json=body, timeout=90, headers={"Authorization": f"Bearer {key}"}).json()
        t = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        m = re.search(r"\{.*\}", t, re.S)
        v = json.loads(m.group(0)) if m else {}
    except Exception as e:                                   # noqa: BLE001 - unsure is a no
        return False, 0.0, f"compare failed ({e})"
    return v.get("damaged") is False, float((j.get("usage") or {}).get("cost") or 0), str(v.get("note") or "")[:60]


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
             ours: bool = False, first_as_is: bool = False) -> tuple[Image.Image | None, str, float]:
    """(photo, how, cost): the fixed photo (1000 px, our watermark on) or None when no free way passes.
    `lock` guards the local models (not thread-safe); `check(model, img_url, name, key, ask)` is the vision
    call (crawler.vision.check). `ours`: the photo is our processed one, which already carries our camel
    mark: that corner is painted white first, or the result would carry two marks."""
    from contextlib import nullcontext
    from .images import flatten, remove_background, square, trim, watermark
    if check is None:
        from .vision import check
    lock = lock or nullcontext()
    im = im.convert("RGB")
    if max(im.size) > 1200:
        im.thumbnail((1200, 1200), Image.LANCZOS)
    if ours:
        im = im.copy()
        ImageDraw.Draw(im).rectangle([0.88 * im.width, 0.9 * im.height, im.width, im.height], fill="white")
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
        good, w = passes(v)
        if not good:
            return good, w
        # the mark finder asked again: the check passed a half-filled logo strip on 29 Oct (#14)
        again, c2 = find_boxes(model, cand, key)
        cost += c2
        if [b for b in again if not ((b[0] + b[2]) / 2 > 850 and (b[1] + b[3]) / 2 > 870)]:
            return False, "marks still found"
        return True, "clean"

    def local():
        nonlocal cost
        with lock:
            filled = lama_local(im, bx)
            cand = finish(filled) if filled is not None else None
        if cand is None:
            return None, "; ".join(why), cost
        good, w = ok(cand)
        if good:
            same, c3, note = unchanged(model, finish(im), cand, key)
            cost += c3
            if same:
                return cand, "lama_local", cost
            good, w = False, f"changed more than the mark ({note})"
        why.append(f"lama_local: {w}")
        return None, "; ".join(why), cost

    # 0. a photo whose shop watermark was already taken off (crawler/shopwm.py) is tried as it is first:
    #    the cut-out keeps the largest object only and dropped the loose drill bit beside its pack (31 Oct)
    if first_as_is:
        with lock:
            cand = finish(im)
        good, w = ok(cand)
        if good:
            return cand, "as_is", cost
        why.append("as is: " + w)
    # 1. cut out
    with lock:
        cut, did = remove_background(flatten(im))
        cand = finish(cut) if did else None
    if cand is not None and left_in_boxes(cut, bx) > 0.3:
        why.append("cut-out: a mark sits on the product")
        cand = None
    if cand is not None:
        good, w = ok(cand)
        if good:
            return cand, "cutout", cost
        why.append("cut-out: " + w)
    # 2. no marks found and nothing to cut: the first check may have been wrong (30 Oct: clean photos
    #    of a pipe cutter were sent to the paid model); the strict checks on the photo as it is
    if not bx:
        with lock:
            cand = finish(im)
        good, w = ok(cand)
        if good:
            return cand, "as_is", cost
        why.append("as is: " + w)
        return None, "; ".join(why), cost
    # 3. white fill / LaMa
    sb, w = safe_boxes(bx)
    if not sb and not text_strips(bx):
        why.append(w)
        return None, "; ".join(why), cost
    if not sb:                                               # thin text lines: only the local fill
        why.append(w)
        return local()
    filled = white_fill(im, bx)
    how = "white"
    middle = any(200 < (b[0] + b[2]) / 2 < 800 and 200 < (b[1] + b[3]) / 2 < 800 for b in bx)
    if filled is None and not middle:
        with lock:
            t = time.time()
            filled = lama(im, bx)
            log.debug("lama %.1fs", time.time() - t)
        how = "lama"
    if filled is not None:
        with lock:
            cand = finish(filled)
        good, w = ok(cand)
        if good:
            return cand, how, cost
        why.append(f"{how}: {w}")
    else:
        why.append("LaMa left a patch (or no model)" if not middle else "mark on the product")
    # 4. each mark filled in its own crop, at full detail
    return local()

