"""(25 Nov) Same photos, the free fixes' two questions (mark boxes, the final check) asked with and without the
DeepInfra endpoint first: do the answers agree? Prints the share of photos with the same verdict.

    python tools/freefix_ab.py batch-262 25
"""
from __future__ import annotations

import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

import app  # noqa: E402
from crawler import freefix as F  # noqa: E402
from crawler.config import run_dir  # noqa: E402
from crawler.io import read_csv  # noqa: E402
from crawler.vision import check  # noqa: E402


def boxes(model, im, key, provider):
    keep = F.PROVIDER
    F.PROVIDER = provider
    try:
        return F.find_boxes(model, im, key)[0]
    finally:
        F.PROVIDER = keep


def main() -> None:
    run, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 25
    cfg = app.build_cfg()
    key = os.environ["OPENROUTER_API_KEY"]
    model = cfg.get("images", {}).get("vision_model", "deepseek/deepseek-v4.1-flash")
    root = Path(cfg["_data_dir"]) / "images"
    im = read_csv(run_dir(cfg, run) / "05_images.csv")
    files = [f.strip() for v in im["files"].astype(str) for f in v.split("|") if f.strip()]
    files = [f for f in files if (root / f).exists()]
    random.seed(1)
    files = random.sample(files, min(n, len(files)))

    def one(f):
        img = Image.open(root / f).convert("RGB")
        img.thumbnail((1200, 1200))
        url = F.data_url(img)
        b0, b1 = boxes(model, img, key, None), boxes(model, img, key, F.PROVIDER)
        v0 = check(model, url, "", key, F.ASK_FREE)
        v1 = check(model, url, "", key, F.ASK_FREE, provider=F.PROVIDER)
        return f, bool(b0), bool(b1), F.passes(v0)[0], F.passes(v1)[0], v0.get("_error"), v1.get("_error")

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, files))
    same_b = sum(r[1] == r[2] for r in res)
    same_v = sum(r[3] == r[4] for r in res)
    errs = sum(bool(r[5]) or bool(r[6]) for r in res)
    for r in res:
        if r[1] != r[2] or r[3] != r[4]:
            print("DIFF", r[0], "marks", r[1], r[2], "pass", r[3], r[4])
    print(f"photos {len(res)}  marks found same {same_b}/{len(res)}  check verdict same {same_v}/{len(res)}  errors {errs}")


if __name__ == "__main__":
    main()
