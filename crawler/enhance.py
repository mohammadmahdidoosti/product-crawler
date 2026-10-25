"""AI clean-up of problem photos (adverts / shop text over the photo, busy background), on request only.

An image model on OpenRouter gets the source photo with an instruction to remove every text, logo,
watermark and banner and put the product on a plain white background without changing the product.
Its answer goes through the normal image step (frame, size, Caravan watermark) and is saved as a
candidate next to the product's photos (data/images/<key>/ai<k>_<n>.webp); nothing replaces a photo
until the reviewer accepts it on the review page.
"""
from __future__ import annotations

import base64
import io
import logging
import re
import time
from pathlib import Path

import pandas as pd
import requests
from PIL import Image

from .config import run_dir, secret
from .images import make, product_key
from .io import Appender, read_csv

log = logging.getLogger(__name__)
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_IMAGES = "https://openrouter.ai/api/v1/images"
PROMPT = ("Edit this product photo for an online store. Remove ALL text, captions, prices, phone numbers, web "
          "addresses, shop logos, watermarks, stickers and banners that are overlaid on the photo or placed around "
          "the product. Put the product alone on a plain pure white background, centered, with soft natural "
          "lighting. Do NOT change the product itself in any way: keep its exact shape, colours, parts, proportions "
          "and the brand name or labels printed on the product body. Do not add anything.")
ENH_COLS = ["url", "key", "n", "model", "file", "seconds", "status"]


def _data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def edit(model: str, img: Image.Image, key: str) -> tuple[Image.Image | None, str]:
    """Image models use OpenRouter's /images endpoint; chat models that can return images use chat."""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://caravansanat.com", "X-Title": "Caravan crawler"}
    body = {"model": model, "prompt": PROMPT, "n": 1,
            "input_references": [{"type": "image_url", "image_url": {"url": _data_url(img)}}]}
    try:
        r = requests.post(OPENROUTER_IMAGES, json=body, timeout=300, headers=headers)
    except requests.RequestException as e:
        return None, f"error {e}"
    if r.status_code == 404 and "chat" in r.text:              # a chat model with image output
        return _edit_chat(model, img, headers)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:300]}"
    j = r.json()
    cost = (j.get("usage") or {}).get("cost")
    for it in j.get("data") or []:
        try:
            if it.get("b64_json"):
                return Image.open(io.BytesIO(base64.b64decode(it["b64_json"]))), f"ok cost={cost}"
            if it.get("url"):
                return Image.open(io.BytesIO(requests.get(it["url"], timeout=60).content)), f"ok cost={cost}"
        except Exception as e:                                # noqa: BLE001
            return None, f"bad image: {e}"
    return None, "no image in answer: " + r.text[:200]


def _edit_chat(model: str, img: Image.Image, headers: dict) -> tuple[Image.Image | None, str]:
    body = {"model": model, "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT},
                                                      {"type": "image_url", "image_url": {"url": _data_url(img)}}]}]}
    try:
        r = requests.post(OPENROUTER, json=body, timeout=300, headers=headers)
    except requests.RequestException as e:
        return None, f"error {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:300]}"
    msg = ((r.json().get("choices") or [{}])[0].get("message") or {})
    for im in msg.get("images") or []:
        u = (im.get("image_url") or {}).get("url") or ""
        try:
            if u.startswith("data:"):
                return Image.open(io.BytesIO(base64.b64decode(u.split(",", 1)[1]))), "ok"
        except Exception as e:                                # noqa: BLE001
            return None, f"bad image: {e}"
    return None, "no image in answer"


def source_photo(row: dict, n: int, data_dir: Path) -> Image.Image | None:
    """The original photo n (1-based) of the product, else our processed one."""
    urls = [u for u in str(row.get("source_images") or "").split(" | ") if u]
    if 0 < n <= len(urls):
        try:
            r = requests.get(urls[n - 1], timeout=30, headers={"User-Agent": "Mozilla/5.0 CaravanSanatBot"})
            if r.status_code == 200:
                return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception:                                     # noqa: BLE001
            pass
    p = data_dir / "images" / product_key(row["url"]) / f"{n}.webp"
    return Image.open(p).convert("RGB") if p.exists() else None


def enhance(cfg: dict, run: str, urls: list[str], models: list[str], n: int = 1, first_ok: bool = False,
            compare: bool = False) -> Path:
    """first_ok: stop at the first model that returns a picture (the backups are for refusals/errors)."""
    key = secret("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    d = run_dir(cfg, run)
    imgs = {r["url"]: r for r in read_csv(d / "05_images.csv").to_dict("records")}
    out_p = d / "05_enhance.csv"
    with Appender(out_p, ENH_COLS) as a:
        for url in urls:
            row = imgs.get(url)
            if not row:
                continue
            k = product_key(url)
            src = source_photo(row, n, cfg["_data_dir"])
            if src is None:
                a.write({"url": url, "key": k, "n": n, "model": "", "file": "", "seconds": 0, "status": "no photo"})
                continue
            if max(src.size) > 1400:
                src.thumbnail((1400, 1400))
            for mi, m in enumerate(models, 1):
                t0 = time.time()
                out, status = edit(m, src, key)
                file = ""
                if out is not None:
                    pic, _ = make(out, None, int(cfg.get("images", {}).get("size", 1000)), cutout=False)
                    if compare:                      # a model test: never over a product's own photos
                        file = f"_compare/{k}/{re.sub(r'[^a-z0-9]+', '-', m.lower())}_{n}.webp"
                    else:
                        file = f"{k}/ai{mi}_{n}.webp"
                    (cfg["_data_dir"] / "images" / file).parent.mkdir(parents=True, exist_ok=True)
                    pic.save(cfg["_data_dir"] / "images" / file, "WEBP", quality=88, method=4)
                log.info("enhance %s with %s: %s (%.0fs)", url[-50:], m, status[:80], time.time() - t0)
                a.write({"url": url, "key": k, "n": n, "model": m, "file": file,
                         "seconds": round(time.time() - t0), "status": status[:200]})
                if first_ok and file:
                    break
    return out_p


def candidates(d: Path) -> dict[str, list[dict]]:
    p = d / "05_enhance.csv"
    if not p.exists():
        return {}
    out: dict[str, list[dict]] = {}
    for r in read_csv(p).to_dict("records"):
        if r.get("file"):
            out.setdefault(r["url"], []).append(r)
    return out
