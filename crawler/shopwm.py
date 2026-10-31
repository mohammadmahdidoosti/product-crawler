"""A shop's own watermark, learned from many of its photos and taken off for free.

A shop stamps the same logo, frame or see-through watermark on every photo it publishes (batches 5-9:
70% of the paid photo edits were photos of 13 shops; rockcontrol.ir framed all its photos in the same
grey card with its phone and logo, control24.ir laid the same faint logo across every product). In
photos of one shop that have the same size, what stays the same pixel for pixel is the watermark: the
products differ from photo to photo.

  learn  -> the shop's photos of one size (at least MIN_PHOTOS): the per-pixel median (the template) and
            where it does not change between photos and is not white (the mark); for a see-through mark
            also how strong it is (alpha), chosen so the mark's edges vanish from the photos it covers.
  remove -> where the photo still shows the template unchanged: white (a frame, a logo on the margin);
            where the product shows through a see-through mark: the blend reversed,
            I = (J - M) / (1 - alpha) + 255 (the mark laid over white gives M).
The result still has to pass the free fix's checks before it is used (crawler/freefix.py).
Kept in DATA_DIR/shop_wm/<host>/ (sample photos and template.npz per size).
"""
from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)
MIN_PHOTOS = 5
MAX_SAMPLES = 16
PCT = 97            # which brightness percentile of a shop's photos is taken as the mark over white
REP_MIN = 0.10      # least share of photos that must show the mark over white at a pixel


def host_of(url: str) -> str:
    m = re.match(r"https?://([^/:]+)", str(url or ""))
    return (m.group(1).lower().removeprefix("www.") if m else "")


def _folder(data_dir: Path, host: str) -> Path:
    return Path(data_dir) / "shop_wm" / re.sub(r"[^a-z0-9.-]", "_", host)


def add_sample(data_dir: Path, url: str, data: bytes) -> None:
    """Keeps up to MAX_SAMPLES original photos per shop and size (JPEG, as downloaded)."""
    host = host_of(url)
    if not host or "made-in-china" in host:            # one host, thousands of suppliers: no common mark
        return
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:                                  # noqa: BLE001
        return
    if min(w, h) < 300:
        return
    d = _folder(data_dir, host) / f"{w}x{h}"
    if d.exists() and len(list(d.glob("*.img"))) >= MAX_SAMPLES:
        return
    d.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9]", "", url)[-40:] or "x"
    p = d / f"{name}.img"
    if not p.exists():
        p.write_bytes(data)


def sample_count(data_dir: Path, host: str) -> int:
    """The most samples this shop has in one photo size."""
    root = _folder(data_dir, host)
    if not root.exists():
        return 0
    return max((len(list(d.glob("*.img"))) for d in root.glob("*x*")), default=0)


def earlier_photos(data_dir: Path, hosts: set[str], per_host: int = 12) -> dict[str, list[str]]:
    """Photo URLs of these shops from earlier runs (05_images.csv), to learn a shop's watermark before
    its first photo needs fixing (batch 10: no shop had 5 same-size photos yet, so nothing was learned)."""
    import csv
    out: dict[str, list[str]] = {h: [] for h in hosts}
    for f in sorted((Path(data_dir) / "runs").glob("*/05_images.csv"), reverse=True):
        try:
            with open(f, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    for u in str(r.get("source_images") or "").split(" | "):
                        h = host_of(u)
                        if h in out and len(out[h]) < per_host and u not in out[h]:
                            out[h].append(u)
        except OSError:
            continue
        if all(len(v) >= per_host for v in out.values()):
            break
    return {h: v for h, v in out.items() if v}


def _load(p: Path) -> np.ndarray | None:
    try:
        with Image.open(p) as im:
            return np.asarray(im.convert("RGB"), dtype=np.float32)
    except Exception:                                  # noqa: BLE001
        return None


def _solid(mark: np.ndarray) -> np.ndarray:
    """Only solid parts: a logo or a frame is a solid shape, while scattered dots are bits of products
    that came out alike in a few photos (two photos of one product: 30 Oct, choobakabzar)."""
    import cv2
    m = cv2.morphologyEx(mark.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    keep = np.zeros_like(mark)
    min_area = max(30, int(0.0002 * mark.size))
    for i in range(1, n):
        x, y, w, h, a = stats[i]
        if a >= min_area and a >= 0.15 * w * h:           # dense enough to be one shape
            keep |= lab == i
    if keep.any():                                         # the shape's own holes and thin strokes back
        keep = cv2.dilate(keep.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & mark
        # and the small parts of the same logo (the dots of Persian letters: 31 Oct, choobakabzar): a small
        # piece of the raw mark that lies within the box of a kept shape belongs to it
        n2, lab2, st2, _ = cv2.connectedComponentsWithStats(keep.astype(np.uint8), connectivity=8)
        H, W = mark.shape
        pad = max(4, int(0.01 * max(H, W)))
        zone = np.zeros_like(mark)
        for i in range(1, n2):
            x, y, w, h, _a = st2[i]
            if w * h > 0.25 * H * W:                        # a frame round the photo: no logo box
                continue
            zone[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad] = True
        n3, lab3, st3, _ = cv2.connectedComponentsWithStats(mark.astype(np.uint8), connectivity=8)
        for i in range(1, n3):
            x, y, w, h, _a = st3[i]
            if zone[y:y + h, x:x + w].all():
                keep |= lab3 == i
    return keep


def learn_from(arrays: list[np.ndarray]) -> dict | None:
    """The template of one shop and size, or None when its photos share no mark."""
    if len(arrays) < MIN_PHOTOS:
        return None
    st = np.stack(arrays)
    # A mark darkens what lies under it: J = a*W + (1-a)*I <= a*W + (1-a)*255, the mark over white.
    # So the lighter photos at a pixel show the mark over white even where most photos have a product
    # there (the middle of the photo): the 85th percentile, not the median, is the template.
    med = np.percentile(st, PCT, axis=0)
    # a mark repeats exactly (whenever white lies under it); a product's colour does not
    repeats = (np.abs(st - med).max(axis=3) <= 12).mean(axis=0)
    mark = (med.min(axis=2) < 240) & (repeats >= max(REP_MIN, 2.0 / len(arrays)))
    mark = _solid(mark)
    frac = float(mark.mean())
    if frac < 0.0008:
        return None
    # Inside the mark a photo that differs from the template has its product there: either the product
    # lies ON the mark (a frame or card behind it: left as it is) or it shows THROUGH a see-through mark
    # (the blend is reversed). Told apart at the mark's border, where the product runs on both sides:
    # a see-through mark makes a step there, and J - M = (1 - alpha) * (I - 255) gives alpha.
    ratios, steps = [], []
    for J in arrays:
        diffm = np.abs(J - med).max(axis=2) > 18
        for dx in (2, -2):
            inside = mark & diffm
            q = np.roll(inside, dx, axis=1)              # q: the pixel dx away from an inside pixel
            out_q = q & ~mark
            ys, xs = np.nonzero(out_q)
            if not len(ys):
                continue
            xp = xs - dx
            Jq, Jp, Mp = J[ys, xs], J[ys, xp], med[ys, xp]
            prod = Jq.min(axis=1) < 225                   # the product (not white) just outside
            if prod.sum() < 20:
                continue
            Jq, Jp, Mp = Jq[prod], Jp[prod], Mp[prod]
            steps.extend(np.abs(Jp - Jq).max(axis=1).tolist())
            den = Jq - 255.0
            ok = np.abs(den) > 40
            ratios.extend(((Jp - Mp)[ok] / den[ok]).tolist())
    alpha = 0.0
    if len(steps) >= 30 and float(np.median(steps)) > 10 and len(ratios) >= 30:
        a = 1.0 - float(np.median(ratios))
        if 0.08 <= a <= 0.85:
            alpha = round(a, 3)
    bg = np.median(st, axis=0).min(axis=2) > 235          # white in most of the shop's photos: the margin
    return {"med": med.astype(np.uint8), "mark": mark, "alpha": alpha, "frac": frac, "n": len(arrays), "bg": bg}


def learn(data_dir: Path, host: str) -> int:
    """Learns (or re-learns) every size of a shop that has enough samples; returns how many templates."""
    made = 0
    root = _folder(data_dir, host)
    for d in sorted(root.glob("*x*")) if root.exists() else []:
        arrays = [a for a in (_load(p) for p in sorted(d.glob("*.img"))) if a is not None]
        t = learn_from(arrays)
        if t is None:
            (d / "template.npz").unlink(missing_ok=True)
            continue
        np.savez_compressed(d / "template.npz", med=t["med"], mark=t["mark"], alpha=t["alpha"], bg=t["bg"])
        (d / "info.json").write_text(json.dumps({"alpha": t["alpha"], "frac": t["frac"], "photos": t["n"]}))
        made += 1
        log.info("shop watermark %s %s: %.1f%% of the photo, alpha %.2f, from %d photos",
                 host, d.name, 100 * t["frac"], t["alpha"], t["n"])
    return made


def template(data_dir: Path, url: str, size: tuple[int, int]) -> dict | None:
    p = _folder(data_dir, host_of(url)) / f"{size[0]}x{size[1]}" / "template.npz"
    if not p.exists():
        return None
    z = np.load(p)
    return {"med": z["med"].astype(np.float32), "mark": z["mark"], "alpha": float(z["alpha"]),
            "bg": z["bg"] if "bg" in z.files else None}


def remove(im: Image.Image, t: dict) -> tuple[Image.Image | None, str]:
    """The photo without the shop's watermark, or None when it does not carry this template."""
    J = np.asarray(im.convert("RGB"), dtype=np.float32)
    if J.shape != t["med"].shape:
        return None, "other size"
    M, mark, a = t["med"], t["mark"], t["alpha"]
    diff = np.abs(J - M).max(axis=2)
    same = mark & (diff <= 18)
    # a photo with the mark is nowhere lighter than the mark over white; one without it is white there
    lighter = mark & ((J - M).max(axis=2) > 18)
    if lighter.sum() > 0.1 * mark.sum() or same.sum() < 0.1 * mark.sum():
        return None, "mark not in this photo"
    out = J.copy()
    out[same] = 255.0
    under = mark & ~same
    if under.any() and a > 0.05:                      # the product shows through a see-through mark
        out[under] = np.clip((J[under] - M[under]) / (1 - a) + 255.0, 0, 255)
        # the reversal multiplies small differences by 1/(1-alpha): where an opaque logo's soft edge lay over
        # white it made faint pink dots (31 Oct, choobakabzar). What is light both before and after is white.
        light = under & (J.min(axis=2) > 200) & (out.min(axis=2) > 200)
        out[light] = 255.0
    # else the product lies on the frame/card: its pixels stay as they are
    # the mark's soft edges and light boxes (too light to count as mark) stay as faint dots and a grey patch
    # (31 Oct, choobakabzar): around each part of the mark, what still equals the template goes white too
    import cv2
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mark.astype(np.uint8), connectivity=8)
    zone = np.zeros_like(mark)
    H, W = mark.shape
    pad = max(6, int(0.012 * max(H, W)))
    for i in range(1, n):
        x, y, w, h, _a = stats[i]
        zone[max(0, y - pad):min(H, y + h + pad), max(0, x - pad):min(W, x + w + pad)] = True
    edge = zone & ~mark & (diff <= 30) & (M.min(axis=2) > 150)
    out[edge] = 255.0
    # a logo on the margin (white in most of the shop's photos) whose box holds nothing else in this photo:
    # the whole box goes white, so no soft edge or dot of it is left (31 Oct, the dots of choobakabzar's logo)
    bg = t.get("bg")
    if bg is not None:
        near = cv2.dilate(mark.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
        for i in range(1, n):
            x, y, w, h, _a = stats[i]
            ys, ye = max(0, y - pad), min(H, y + h + pad)
            xs, xe = max(0, x - pad), min(W, x + w + pad)
            if (ye - ys) * (xe - xs) > 0.1 * H * W:
                continue
            rest = ~near[ys:ye, xs:xe]
            if not rest.any() or bg[ys:ye, xs:xe][rest].mean() < 0.95:
                continue                                  # the box lies where products usually are
            if (J[ys:ye, xs:xe][rest].min(axis=1) < 200).mean() > 0.01:
                continue                                  # something of the product is in the box
            out[ys:ye, xs:xe] = 255.0
    return Image.fromarray(out.astype(np.uint8)), "shop watermark removed"
