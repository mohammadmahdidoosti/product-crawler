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


def _load(p: Path) -> np.ndarray | None:
    try:
        with Image.open(p) as im:
            return np.asarray(im.convert("RGB"), dtype=np.float32)
    except Exception:                                  # noqa: BLE001
        return None


def learn_from(arrays: list[np.ndarray]) -> dict | None:
    """The template of one shop and size, or None when its photos share no mark."""
    if len(arrays) < MIN_PHOTOS:
        return None
    st = np.stack(arrays)
    # A mark darkens what lies under it: J = a*W + (1-a)*I <= a*W + (1-a)*255, the mark over white.
    # So the lighter photos at a pixel show the mark over white even where most photos have a product
    # there (the middle of the photo): the 85th percentile, not the median, is the template.
    med = np.percentile(st, 85, axis=0)
    # a mark repeats exactly (whenever white lies under it); a product's colour does not
    repeats = (np.abs(st - med).max(axis=3) <= 12).mean(axis=0)
    mark = (med.min(axis=2) < 240) & (repeats >= max(0.15, 2.0 / len(arrays)))
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
    return {"med": med.astype(np.uint8), "mark": mark, "alpha": alpha, "frac": frac, "n": len(arrays)}


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
        np.savez_compressed(d / "template.npz", med=t["med"], mark=t["mark"], alpha=t["alpha"])
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
    return {"med": z["med"].astype(np.float32), "mark": z["mark"], "alpha": float(z["alpha"])}


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
    # else the product lies on the frame/card: its pixels stay as they are
    return Image.fromarray(out.astype(np.uint8)), "shop watermark removed"
