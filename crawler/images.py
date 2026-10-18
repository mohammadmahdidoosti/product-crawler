"""Image step: the product photos become Caravan Sanat's own.

For every new product (05_new_products.csv), up to `images.per_product` photos:
  1. downloaded politely (robots.txt and the per-site delay of the crawl);
  2. the source shop's logo/watermark removed: a shop stamps the same mark at the same place on
     all its photos, so the pixels that stay the same across many of that shop's photos — while
     the products under them change — are its mark; they are filled in from their surroundings
     (OpenCV inpainting);
  3. trimmed of empty margins and centred on a white 1000x1000 square (transparent parts white);
  4. stamped with the Caravan logo, small, in the bottom-right corner;
  5. saved as WebP in data/images/<product key>/<n>.webp and served at /img/... so the WooCommerce
     importer can fetch them from the 'Images' column.
05_images.csv lists, per product page, the files made and what was done.
No paid service and no AI model: Pillow, NumPy and OpenCV only.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .config import run_dir
from .fetch import Fetcher, domain_of
from .io import FILES, read_csv, write_csv

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "assets" / "caravan-logo.png"
IMG_COLS = ["url", "key", "files", "source_images", "notes"]
SIDE = 256


def product_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:14]


# ------------------------------------------------------------------ download

def download(f: Fetcher, url: str, min_side: int = 250) -> bytes | None:
    if not url.startswith("http") or not f.allowed(url):
        return None
    f._wait(domain_of(url))
    try:
        r = f.s.get(url, timeout=f.timeout)
    except Exception as e:                                   # noqa: BLE001 - a bad image never stops the run
        log.info("image fetch failed %s (%s)", url, e)
        return None
    if r.status_code != 200 or not r.content:
        return None
    try:
        with Image.open(io.BytesIO(r.content)) as im:
            if min(im.size) < min_side:
                return None
    except Exception:                                        # noqa: BLE001
        return None
    return r.content


def thumb(data: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(data)) as im:
        return np.asarray(flatten(im).resize((SIDE, SIDE)).convert("L"), dtype=np.float32)


def flatten(im: Image.Image) -> Image.Image:
    """Transparent parts (PNG/WebP cut-outs) become white; everything ends up RGB."""
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


# ------------------------------------------------------------------ the shop's own mark


def shop_mark(thumbs: list[np.ndarray], min_images: int = 4) -> np.ndarray | None:
    """Where a shop stamps its logo: pixels that are (a) nearly identical across the shop's photos,
    (b) not plain white background, (c) a compact area, not the whole picture.
    Returns a SIDE x SIDE boolean mask in relative coordinates, or None."""
    if len(thumbs) < min_images:
        return None
    stack = np.stack(thumbs[:40])
    med = np.median(stack, axis=0)
    spread = np.median(np.abs(stack - med), axis=0)          # how much each pixel changes between photos
    mask = (spread < 6) & (med < 235)                        # constant and not background
    band = np.ones((SIDE, SIDE), bool)                       # shops put marks near the edges; a 'mark'
    c0, c1 = int(SIDE * 0.25), int(SIDE * 0.75)              # found in the middle is the product itself
    band[c0:c1, c0:c1] = False
    mask &= band
    frac = mask.mean()
    if frac < 0.0005 or frac > 0.12:                         # nothing, or the shop's photos are just alike
        return None
    import cv2
    mask = cv2.dilate(mask.astype(np.uint8), np.ones((5, 5), np.uint8), iterations=1).astype(bool)
    return np.where(mask, med, -1.0)               # the mark's usual grey level; -1 = not the mark


def remove_mark(im: Image.Image, mark: np.ndarray | None) -> tuple[Image.Image, bool]:
    """Only where this photo really shows the mark (its pixels look like the mark's usual ones);
    where the product itself covers that place, the product is left alone."""
    if mark is None:
        return im, False
    import cv2
    w, h = im.size
    g = cv2.resize(np.asarray(im.convert("L"), dtype=np.float32), (SIDE, SIDE), interpolation=cv2.INTER_AREA)
    here = (mark >= 0) & (np.abs(g - mark) < 30)
    if here.sum() < 4:
        return im, False
    m = cv2.resize(here.astype(np.uint8) * 255, (w, h), interpolation=cv2.INTER_NEAREST)
    m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=2)
    arr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    fixed = cv2.inpaint(arr, m, 5, cv2.INPAINT_TELEA)
    return Image.fromarray(cv2.cvtColor(fixed, cv2.COLOR_BGR2RGB)), True


# ------------------------------------------------------------------ framing and watermark

def drop_loose_marks(im: Image.Image, band: float = 0.3, max_rel: float = 0.15) -> tuple[Image.Image, bool]:
    """On a white photo, a shop logo, a title banner or a '2300W' caption is a separate island near an
    edge, apart from the product. Those islands are painted white; the product (the biggest island)
    and anything close to it stay."""
    import cv2
    a = np.asarray(im.convert("L"))
    h, w = a.shape
    fg = (a < 235).astype(np.uint8)
    k = max(3, int(min(w, h) * 0.02))
    joined = cv2.dilate(fg, np.ones((k, k), np.uint8))       # parts of one product join up
    n, lab, stats, _ = cv2.connectedComponentsWithStats(joined, 8)
    if n <= 2:
        return im, False
    areas = stats[1:, cv2.CC_STAT_AREA]
    main = 1 + int(np.argmax(areas))
    mx, my, mw, mh = stats[main, :4]
    out = np.asarray(im).copy()
    removed = False
    drop = np.zeros(n, bool)
    for i in range(1, n):
        if i == main:
            continue
        x, y, bw, bh, area = stats[i]
        if area > max_rel * stats[main, cv2.CC_STAT_AREA]:
            continue                                          # a second product (a kit), not a mark
        near_edge = (x + bw < w * band or x > w * (1 - band) or y + bh < h * band or y > h * (1 - band))
        inside_main = x >= mx and y >= my and x + bw <= mx + mw and y + bh <= my + mh
        if near_edge and not inside_main:
            drop[i] = True
            removed = True
    if removed:
        out[drop[lab]] = 255                                  # one pass over the picture, not one per island
    return (Image.fromarray(out), True) if removed else (im, False)


def trim(im: Image.Image, tol: int = 245) -> Image.Image:
    """Cut the empty white margins so every product fills the frame the same way."""
    a = np.asarray(im.convert("L"))
    ys, xs = np.where(a < tol)
    if len(xs) < 50:
        return im
    pad = 4
    box = (max(0, xs.min() - pad), max(0, ys.min() - pad), min(im.width, xs.max() + pad + 1),
           min(im.height, ys.max() + pad + 1))
    return im.crop(box)


def busy_background(im: Image.Image) -> bool:
    """The photo is not on white (a scene, a coloured studio backdrop): framed as it is, flagged.
    A banner or caption touching one edge does not make it busy: most of the border must be non-white."""
    a = np.asarray(im.convert("L"), dtype=np.float32)
    border = np.concatenate([a[:6].ravel(), a[-6:].ravel(), a[:, :6].ravel(), a[:, -6:].ravel()])
    return float((border < 225).mean()) > 0.35


def square(im: Image.Image, size: int = 1000, margin: float = 0.06) -> Image.Image:
    box = int(size * (1 - 2 * margin))
    im = im.copy()
    im.thumbnail((box, box), Image.LANCZOS)
    if max(im.size) < box:                                   # small photos are enlarged to the frame
        r = box / max(im.size)
        im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))), Image.LANCZOS)
    out = Image.new("RGB", (size, size), (255, 255, 255))
    out.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
    return out


_logo_cache: dict[int, Image.Image] = {}


def watermark(im: Image.Image, width_frac: float = 0.09, margin_frac: float = 0.025,
              opacity: float = 0.95) -> Image.Image:
    """Caravan logo, small, bottom-right (as on the site's own product photos)."""
    lw = max(40, int(im.width * width_frac))
    if lw not in _logo_cache:
        logo = Image.open(LOGO).convert("RGBA")
        logo = logo.resize((lw, max(1, int(logo.height * lw / logo.width))), Image.LANCZOS)
        if opacity < 1:
            a = logo.split()[-1].point(lambda v: int(v * opacity))
            logo.putalpha(a)
        _logo_cache[lw] = logo
    logo = _logo_cache[lw]
    m = int(im.width * margin_frac)
    out = im.copy()
    out.paste(logo, (im.width - logo.width - m, im.height - logo.height - m), logo)
    return out


def make(im: Image.Image, mark: np.ndarray | None, size: int = 1000) -> tuple[Image.Image, list[str]]:
    notes = []
    im = flatten(im)
    if max(im.size) > 1600:                                   # the result is 1000px: big originals only cost memory
        im.thumbnail((1600, 1600), Image.LANCZOS)
    im, removed = remove_mark(im, mark)
    if removed:
        notes.append("shop_mark_removed")
    if busy_background(im):
        notes.append("busy_background")
    else:
        im, loose = drop_loose_marks(im)
        if loose:
            notes.append("logo_text_removed")
        im = trim(im)
    return watermark(square(im, size)), notes


# ------------------------------------------------------------------ step

def step_images(cfg: dict, run: str) -> Path | None:
    from .pipeline import _fetchers, _progress
    ic = cfg.get("images", {})
    if not ic.get("enabled", True):
        log.info("images: off")
        return None
    d = run_dir(cfg, run)
    src = d / FILES["new"]
    if not src.exists():
        log.info("images: no new products yet")
        return None
    new = read_csv(src)
    per, size = int(ic.get("per_product", 3)), int(ic.get("size", 1000))
    out_root = cfg["_data_dir"] / "images"
    done_p = d / "05_images.csv"
    done = read_csv(done_p) if done_p.exists() else None
    have = set(done["url"]) if done is not None and len(done) else set()
    fx = _fetchers(cfg)
    rows = [r for r in new.to_dict("records") if r["url"] not in have]
    log.info("images: %d products to do (%d done before)", len(rows), len(have))

    # 1. download, grouped by shop (the shop's mark is learned from all its photos together)
    def fetch_one(r):
        try:
            urls = [u for u in json.loads(r.get("image_urls") or "[]") if isinstance(u, str)][:per]
        except ValueError:
            urls = []
        f = fx["foreign"] if r.get("source_type") == "foreign" else fx["iran"]
        tmp = tmp_root / product_key(r["url"])
        paths, thumbs = [], []
        for n, u in enumerate(urls):
            data = download(f, u)
            if data is None:
                continue
            tmp.mkdir(parents=True, exist_ok=True)
            p = tmp / f"{n}.src"
            p.write_bytes(data)
            paths.append(p)
            try:
                thumbs.append(thumb(data))
            except Exception:                                # noqa: BLE001
                pass
        return r, urls, paths, thumbs

    import os
    tmp_root = Path(os.environ.get("CACHE_DIR", "/tmp")) / "caravan-img"
    got: list[tuple] = []
    with ThreadPoolExecutor(int(cfg.get("http", {}).get("workers", 8))) as ex:
        for i, item in enumerate(ex.map(fetch_one, rows), 1):
            got.append(item)
            if _progress("images", i, len(rows) * 2):
                break
    by_shop: dict[str, list[np.ndarray]] = {}
    for r, _, _, th in got:
        by_shop.setdefault(r.get("source_domain", ""), []).extend(th)
    # a shop's own photos from the whole crawl help too, but downloading them all is slow: the
    # products of this run are usually enough (24 per category, several per shop)
    marks = {dom: shop_mark(ims) for dom, ims in by_shop.items()}
    log.info("images: shop marks found for %s", ", ".join(k for k, v in marks.items() if v is not None) or "none")

    # 2. make and save
    out = [] if done is None else done.to_dict("records")
    for i, (r, urls, paths, _) in enumerate(got, 1):
        key = product_key(r["url"])
        folder = out_root / key
        files, notes = [], set()
        for n, src_p in enumerate(paths):
            try:
                with Image.open(src_p) as im:
                    pic, nt = make(im, marks.get(r.get("source_domain", "")), size)
            except Exception as e:                       # noqa: BLE001 - one broken photo is skipped
                log.info("image failed for %s: %s", r["url"], e)
                continue
            finally:
                src_p.unlink(missing_ok=True)
            folder.mkdir(parents=True, exist_ok=True)
            p = folder / f"{n + 1}.webp"
            pic.save(p, "WEBP", quality=int(ic.get("quality", 85)), method=4)
            files.append(f"{key}/{p.name}")
            notes.update(nt)
        out.append({"url": r["url"], "key": key, "files": " | ".join(files), "source_images": " | ".join(urls),
                    "notes": " | ".join(sorted(notes)) or ("no_image" if not files else "")})
        _progress("images", len(rows) + i, len(rows) * 2)
    write_csv(__import__("pandas").DataFrame(out, columns=IMG_COLS), done_p)
    n_ok = sum(1 for x in out if x["files"])
    log.info("images: %d of %d products have photos (data/images)", n_ok, len(out))
    return done_p


def public_urls(cfg: dict, files: str) -> str:
    """'abc/1.webp | abc/2.webp' -> 'https://<server>/img/abc/1.webp, …' for the WooCommerce importer."""
    import os
    base = (cfg.get("images", {}).get("public_base") or os.environ.get("PUBLIC_URL")
            or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not base or not files:
        return ""
    return ", ".join(f"{base}/img/{f.strip()}" for f in files.split("|") if f.strip())
