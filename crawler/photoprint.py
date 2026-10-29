"""Photo fingerprints: a photo fixed once (free or paid) is not paid for again.

Shops copy each other's photos, and one photo often serves several products (sizes, colours, kits).
For every fixed photo the fingerprint of the photo BEFORE the fix is kept with the fixed file
(data/photo_fixes.csv). When the same photo comes up again, the fixed one is copied.

Same photo = a 256-bit difference hash at most 8 bits apart AND, in colour at 128x128 on what is not
background, under 2% of the pixels clearly different (two different tools can share a rough shape).
"""
from __future__ import annotations

import csv
import glob
import logging
import threading
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)
COLS = ["hash", "before", "fixed", "how"]


def _grey(p: Path, side: int) -> np.ndarray:
    with Image.open(p) as im:
        return np.asarray(im.convert("L").resize((side, side), Image.LANCZOS), dtype=np.int16)


def dhash(p: Path) -> str:
    a = _grey(p, 17)[:16, :]                        # 16 rows x 17 columns -> 16 x 16 comparisons
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    return "%064x" % int("".join("1" if b else "0" for b in bits), 2)


def distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def same_photo(p1: Path, p2: Path) -> bool:
    """Compared in colour, on what is not background in either photo: two drill bits, black and gold,
    on the same shop's patterned background were 'the same' in a plain grey average (29 Oct)."""
    try:
        with Image.open(p1) as x, Image.open(p2) as y:
            a = np.asarray(x.convert("RGB").resize((128, 128), Image.LANCZOS), dtype=np.int16)
            b = np.asarray(y.convert("RGB").resize((128, 128), Image.LANCZOS), dtype=np.int16)
    except Exception:                                    # noqa: BLE001
        return False
    diff = np.abs(a - b).max(axis=2)
    mask = (a.min(axis=2) < 235) | (b.min(axis=2) < 235)
    mask[-16:, -20:] = False                             # our camel mark
    if mask.sum() < 50:
        return False
    return float((diff[mask] > 40).mean()) < 0.02 and float(diff[mask].mean()) < 12


class Prints:
    """The kept fingerprints; thread-safe; new fixes are appended to the file at once."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "images"
        self.path = Path(data_dir) / "photo_fixes.csv"
        self.lock = threading.Lock()
        self.rows: list[dict] = []
        if not self.path.exists():
            self._build(Path(data_dir))
        else:
            with open(self.path, encoding="utf-8") as fh:
                self.rows = [r for r in csv.DictReader(fh) if r.get("hash") and r.get("fixed")]

    def _build(self, data_dir: Path) -> None:
        """First use: the fixes of all earlier runs (05_images.csv: originals -> files[0])."""
        seen = set()
        for f in sorted(glob.glob(str(data_dir / "runs" / "*" / "05_images.csv"))):
            try:
                with open(f, encoding="utf-8-sig") as fh:
                    for r in csv.DictReader(fh):
                        notes = r.get("notes") or ""
                        if not ("ai_fixed" in notes or "free_fixed" in notes) or "found_by_search" in notes:
                            continue
                        orig = (r.get("originals") or "").split("|")[0].strip()
                        fixed = (r.get("files") or "").split("|")[0].strip()
                        if not orig or not fixed or fixed in seen or orig.split("/")[-1].startswith("ai"):
                            continue
                        if not (self.root / orig).exists() or not (self.root / fixed).exists():
                            continue
                        seen.add(fixed)
                        how = next((x.strip() for x in notes.split("|") if "fixed:" in x), "")
                        self.rows.append({"hash": dhash(self.root / orig), "before": orig, "fixed": fixed,
                                          "how": how})
            except Exception as e:                       # noqa: BLE001
                log.info("photo prints %s: %s", f, e)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, COLS)
            w.writeheader()
            w.writerows(self.rows)
        log.info("photo prints: %d earlier fixes kept", len(self.rows))

    def find(self, before: Path) -> dict | None:
        """The earlier fix of this same photo, if there is one and its file still exists."""
        try:
            h = dhash(before)
        except Exception:                                # noqa: BLE001
            return None
        with self.lock:
            near = sorted((distance(h, r["hash"]), i) for i, r in enumerate(self.rows))
        for d, i in near[:5]:
            if d > 8:
                break
            r = self.rows[i]
            if not (self.root / r["fixed"]).exists():
                continue
            old = self.root / (r.get("before") or "")
            if (old.is_file() and same_photo(before, old)) or (not old.is_file() and d <= 3):
                return r
        return None

    def add(self, before: Path, fixed: str, how: str) -> None:
        try:
            rel = str(before.relative_to(self.root)) if before.is_relative_to(self.root) else ""
            row = {"hash": dhash(before), "before": rel, "fixed": fixed, "how": how}
        except Exception:                                # noqa: BLE001
            return
        with self.lock:
            self.rows.append(row)
            new = not self.path.exists()
            with open(self.path, "a", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, COLS)
                if new:
                    w.writeheader()
                w.writerow(row)
