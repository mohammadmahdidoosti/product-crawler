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
Image.MAX_IMAGE_PIXELS = 40_000_000                          # a 100-megapixel 'photo' is skipped, not loaded
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
        im.draft("RGB", (SIDE * 2, SIDE * 2))               # JPEG: decode small, not the full picture
        return np.asarray(flatten(im).resize((SIDE, SIDE)).convert("L"), dtype=np.uint8)


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
    stack = np.stack(thumbs[:40]).astype(np.float32)
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


def remembered_marks(data_dir: Path, marks: dict) -> dict:
    """A shop's mark is learned from at least 4 of its photos in one run; a site-fix or small run often
    has only 1-2 per shop. A mark learned once is kept (data/shop_marks/<domain>.npy) and used when the
    run cannot learn it; a newly learned one replaces the old. remove_mark only paints where a photo
    really shows the mark, so a kept mark the shop no longer uses changes nothing."""
    import re as _re
    folder = Path(data_dir) / "shop_marks"
    out = dict(marks)
    for dom, m in marks.items():
        if not dom:
            continue
        p = folder / (_re.sub(r"[^a-z0-9.-]", "_", str(dom).lower()) + ".npy")
        try:
            if m is not None:
                folder.mkdir(parents=True, exist_ok=True)
                np.save(p, m.astype(np.float32))
            elif p.exists():
                out[dom] = np.load(p)
                log.info("images: %s's mark from an earlier run", dom)
        except Exception as e:                               # noqa: BLE001 - memory is a help, not a must
            log.info("shop mark memory %s: %s", dom, e)
    return out


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
        banner = bw > 3 * bh                                    # a wide strip: a title banner or a web address
        if area > (0.35 if banner else max_rel) * stats[main, cv2.CC_STAT_AREA]:
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


# ------------------------------------------------------------------ open models (free, run on the server)

MODELS = {
    # U^2-Net salient object model (Apache-2.0), the one rembg uses: background removal
    "u2net": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx",
    # PP-OCRv3 text detector from the OpenCV model zoo (Apache-2.0): finds text, does not read it
    "textdet": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/text_detection_ppocr/"
               "text_detection_en_ppocrv3_2023may.onnx",
    # LaMa inpainting (Apache-2.0), ONNX export, 512x512: fills small shop marks at the photo's edges
    "lama": "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx",
}
_models: dict = {}


def model_path(name: str) -> Path | None:
    import os
    import requests
    folder = Path(os.environ.get("MODEL_DIR") or (Path(os.environ.get("DATA_DIR", ROOT / "data")) / "models"))
    p = folder / f"{name}.onnx"
    if p.exists() and p.stat().st_size > 1_000_000:
        return p
    folder.mkdir(parents=True, exist_ok=True)
    try:
        log.info("downloading model %s (once)", name)
        with requests.get(MODELS[name], stream=True, timeout=120) as r:
            r.raise_for_status()
            tmp = p.with_suffix(".part")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
            tmp.rename(p)
        return p
    except Exception as e:                                   # noqa: BLE001 - no model: the step still works
        log.warning("model %s not available: %s", name, e)
        return None


def _session(name: str):
    import os
    if os.environ.get("CARAVAN_NO_MODELS"):
        return None
    if name not in _models:
        p = model_path(name)
        if p is None:
            _models[name] = None
        elif name in ("u2net", "lama"):
            import onnxruntime as ort
            o = ort.SessionOptions()
            o.intra_op_num_threads = 1 if name == "u2net" else 2
            o.enable_cpu_mem_arena = False                   # the arena doubled memory (1.2 GB -> 0.56 GB)
            o.enable_mem_pattern = False
            _models[name] = ort.InferenceSession(str(p), o, providers=["CPUExecutionProvider"])
        else:
            import cv2
            m = cv2.dnn_TextDetectionModel_DB(str(p))
            m.setBinaryThreshold(0.3)
            m.setPolygonThreshold(0.5)
            m.setUnclipRatio(2.0)
            m.setMaxCandidates(200)
            m.setInputParams(1.0 / 255.0, (736, 736), (122.67891434, 116.66876762, 104.00698793))
            _models[name] = m
    return _models[name]


def free_models() -> None:
    _models.clear()


def _solidity(keep: np.ndarray) -> float:
    import cv2
    cs, _ = cv2.findContours(keep.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return 0.0
    hull = cv2.convexHull(np.vstack(cs))
    hm = np.zeros(keep.shape, np.uint8)
    cv2.fillPoly(hm, [hull], 1)
    return float(keep.sum()) / max(int(hm.sum()), 1)


def remove_background(im: Image.Image) -> tuple[Image.Image, bool]:
    """Product cut out and put on white. Kept only when the cut-out looks like one solid object
    (a model that loses half the product is worse than a busy background)."""
    from PIL import ImageFilter
    sess = _session("u2net")
    if sess is None:
        return im, False
    x = np.asarray(im.resize((320, 320), Image.LANCZOS), dtype=np.float32)
    x = x / max(float(x.max()), 1e-6)
    x = (x - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
    x = x.astype(np.float32).transpose(2, 0, 1)[None]
    p = sess.run(None, {sess.get_inputs()[0].name: x})[0][0, 0]
    p = (p - p.min()) / (p.max() - p.min() + 1e-6)
    pm = np.asarray(Image.fromarray((p * 255).astype(np.uint8)).resize(im.size, Image.BILINEAR)) / 255.0
    keep = pm > 0.08
    if not (0.05 < keep.mean() < 0.9) or _solidity(keep) < 0.6:
        return im, False
    import cv2
    keep = cv2.erode(keep.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=2)   # no coloured fringe
    m = Image.fromarray(keep * 255).filter(ImageFilter.GaussianBlur(1.5))
    out = Image.new("RGB", im.size, (255, 255, 255))
    out.paste(im, mask=m)
    return out, True


def overlay_text(pic: Image.Image) -> bool:
    """A shop's banner or web address written over the photo: wide text near the top or bottom edge,
    sitting on white (product labels sit on the product). Our own watermark corner is ignored."""
    det = _session("textdet")
    if det is None:
        return False
    import cv2
    a = cv2.cvtColor(np.asarray(pic), cv2.COLOR_RGB2BGR)
    h, w = a.shape[:2]
    g = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    boxes, _ = det.detect(a)
    wide_lines = 0
    for b in boxes:
        b = np.array(b)
        x0, y0 = b.min(0)
        x1, y1 = b.max(0)
        if x0 > 0.85 * w and y0 > 0.88 * h:
            continue                                         # the Caravan watermark
        if (x1 - x0) >= 0.25 * w:
            wide_lines += 1
            if wide_lines >= 2:                               # advert lines / phone numbers, anywhere
                return True
        if (x1 - x0) < 0.12 * w or not (y1 < 0.22 * h or y0 > 0.8 * h):
            continue
        pad = int(max(6, (y1 - y0) * 0.5))
        ring = np.concatenate([g[max(0, int(y0) - pad):max(0, int(y0)), int(x0):int(x1)].ravel(),
                               g[int(y1):min(h, int(y1) + pad), int(x0):int(x1)].ravel()])
        if ring.size and (ring > 235).mean() > 0.5:
            return True
    return False


def make(im: Image.Image, mark: np.ndarray | None, size: int = 1000, cutout: bool = True) -> tuple[Image.Image, list[str]]:
    notes = []
    if max(im.size) > 1200:                                   # the result is 1000px: big originals only cost memory
        im.draft("RGB", (1200, 1200))                         # (JPEG decodes at reduced size)
        im.thumbnail((1200, 1200), Image.LANCZOS)
    im = flatten(im)
    im, removed = remove_mark(im, mark)
    if removed:
        notes.append("shop_mark_removed")
    if busy_background(im) and cutout:
        im, cut = remove_background(im)
        notes.append("background_removed" if cut else "busy_background")
    elif busy_background(im):
        notes.append("busy_background")
    if "busy_background" not in notes:
        im, loose = drop_loose_marks(im)
        if loose:
            notes.append("logo_text_removed")
        im = trim(im)
    pic = watermark(square(im, size))
    if overlay_text(pic):
        notes.append("text_overlay")
    return pic, notes


# ------------------------------------------------------------------ products with no usable photo

def _iq(r: dict) -> str:
    from .aiphotos import image_query
    return image_query(r)


def search_images(cfg: dict, f: Fetcher, r: dict, n: int = 6) -> list[str]:
    """Google Images (Serper) by the product's name: photos from other pages, largest first."""
    from .config import secret
    from .probe import record_usage
    key = secret("SERPER_API_KEY")
    if not key:
        return []
    q = str(r.get("image_query") or r.get("name") or "")[:120]
    res = f.post_json("https://google.serper.dev/images", {"q": q, "gl": "ir", "num": 10},
                      {"X-API-KEY": key, "Content-Type": "application/json"})
    record_usage(cfg)
    items = ((res or {}).get("json") or {}).get("images") or []
    items = [it for it in items if it.get("imageUrl", "").startswith("http")
             and int(it.get("imageWidth") or 0) >= 400 and "torob.com" not in it.get("imageUrl", "")]
    items.sort(key=lambda it: -int(it.get("imageWidth") or 0) * int(it.get("imageHeight") or 0))
    return [it["imageUrl"] for it in items[:n]]


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
    # photos are static files: a lighter pace than pages (still one at a time per site)
    from copy import deepcopy
    icfg = deepcopy({k: v for k, v in cfg.items() if k != "_data_dir"})
    icfg["_data_dir"] = cfg["_data_dir"]
    icfg.setdefault("http", {})["delay_seconds"] = float(ic.get("delay_seconds", 1))
    fx = _fetchers(icfg)
    rows = [r for r in new.to_dict("records") if r["url"] not in have]
    log.info("images: %d products to do (%d done before)", len(rows), len(have))

    # 1. download, grouped by shop (the shop's mark is learned from all its photos together)
    hosts_seen: set[str] = set()

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
            try:                                     # the shop's photos, for learning its watermark
                from .shopwm import add_sample
                add_sample(cfg["_data_dir"], u, data)
                hosts_seen.add(u)
            except Exception:                        # noqa: BLE001 - a help, not a must
                pass
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
    try:                                             # see-through / framing watermarks (crawler/shopwm.py)
        from .shopwm import host_of, learn as learn_wm
        for h in sorted({host_of(u) for u in hosts_seen} - {""}):
            learn_wm(cfg["_data_dir"], h)
    except Exception as e:                           # noqa: BLE001
        log.info("shop watermarks: %s", e)
    marks = remembered_marks(cfg["_data_dir"], marks)
    by_shop.clear()
    got = [(r, urls, paths, None) for r, urls, paths, _ in got]   # thumbnails no longer needed
    log.info("images: shop marks found for %s", ", ".join(k for k, v in marks.items() if v is not None) or "none")

    # 2. make and save
    from .io import Appender
    out = [] if done is None else done.to_dict("records")
    sink = Appender(done_p, IMG_COLS)                      # row by row: a stopped run resumes where it was
    sink.__enter__()
    quality = int(ic.get("quality", 85))
    search_left = int(ic.get("search_max", 40))
    cutout = bool(ic.get("remove_background", True))

    def build(sources, mark):
        """sources: open-able paths or raw bytes -> [(picture, notes)]"""
        made = []
        for src in sources:
            try:
                with Image.open(io.BytesIO(src) if isinstance(src, bytes) else src) as im:
                    made.append(make(im, mark, size, cutout))
            except Exception as e:                       # noqa: BLE001 - one broken photo is skipped
                log.info("image failed: %s", e)
            finally:
                if isinstance(src, Path):
                    src.unlink(missing_ok=True)
        return made

    import gc
    for i, (r, urls, paths, _) in enumerate(got, 1):
        if i % 20 == 0:
            gc.collect()
        key = product_key(r["url"])
        folder = out_root / key
        made = build(paths, marks.get(r.get("source_domain", "")))
        notes: set[str] = set()
        if not made and search_left > 0:
            # 3. no usable photo on the source page: the same product's photo from other pages
            search_left -= 1
            f = fx["iran"]
            for u in search_images(cfg, f, {**r, "image_query": _iq(r)}):
                data = download(f, u, min_side=350)
                if data:
                    made = build([data], None)
                    if made:
                        urls = [u]
                        notes.add("found_by_search")
                        break
        # 4. a photo with a shop banner / web address over it gives way to a clean one of the same product
        clean = [m for m in made if "text_overlay" not in m[1]]
        if made and not clean and search_left > 0:          # every photo is an advert: look for a clean one
            search_left -= 1
            for u in search_images(cfg, fx["iran"], {**r, "image_query": _iq(r)}):
                data = download(fx["iran"], u, min_side=350)
                got_clean = [m for m in build([data], None) if "text_overlay" not in m[1]] if data else []
                if got_clean:
                    clean = got_clean
                    notes.add("found_by_search")
                    break
        if clean and len(clean) < len(made):
            notes.add("text_photo_replaced")
            made = clean
        if folder.exists():
            for old in folder.glob("*.webp"):
                old.unlink()
        files = []
        for n, (pic, nt) in enumerate(made):
            folder.mkdir(parents=True, exist_ok=True)
            p = folder / f"{n + 1}.webp"
            pic.save(p, "WEBP", quality=quality, method=4)
            files.append(f"{key}/{p.name}")
            notes.update(nt)
        row = {"url": r["url"], "key": key, "files": " | ".join(files), "source_images": " | ".join(urls),
               "notes": " | ".join(sorted(notes)) or ("no_image" if not files else "")}
        out.append(row)
        sink.write(row)
        _progress("images", len(rows) + i, len(rows) * 2)
    sink.__exit__(None, None, None)
    free_models()
    n_ok = sum(1 for x in out if x["files"])
    log.info("images: %d of %d products have photos (data/images)", n_ok, len(out))
    try:
        from .aiphotos import fix_photos
        # vision check, free fixes and search; the paid image-model edit waits for publishing (only the
        # products kept get it) unless images.defer_paid_fix is false
        fix_photos(cfg, run, paid=not cfg.get("images", {}).get("defer_paid_fix", True))
    except Exception as e:                       # noqa: BLE001 - the photos already made stay usable
        log.warning("photo check failed: %s", e)
    return done_p


def public_urls(cfg: dict, files: str) -> str:
    """'abc/1.webp | abc/2.webp' -> 'https://<server>/img/abc/1.webp, …' for the WooCommerce importer."""
    import os
    base = (cfg.get("images", {}).get("public_base") or os.environ.get("PUBLIC_URL")
            or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not base or not files:
        return ""
    return ", ".join(f"{base}/img/{f.strip()}" for f in files.split("|") if f.strip())
