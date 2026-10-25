"""Brand countries learned from the crawl: shops often write 'ساخت ایتالیا' or 'تولید ایران' on a
product page even when the name does not say it. For a brand the brand table leaves without a
country, the pages of all runs are counted: a brand seen on at least `min_pages` pages, where one
country is named on at least `min_share` of the country mentions (and on 3 pages or more),
gets that country (DATA_DIR/brand_countries.csv, with the counts, for a look). The brand table's
own 'country' column always wins over a learned one.
"""
from __future__ import annotations

import collections
import logging
import re

import pandas as pd

from .normalize import norm_text

log = logging.getLogger(__name__)
SAID = {"ایران": r"ساخت ایران|تولید ایران|ساخت داخل|تولید داخل|محصول ایران|برند ایرانی", "ایتالیا": r"ایتالیا",
        "چین": r"ساخت چین|چینی|china", "آلمان": r"آلمان|germany", "ترکیه": r"ترکیه|turkey|ترک\b",
        "ژاپن": r"ژاپن|japan", "کره": r"کره جنوبی|korea", "تایوان": r"تایوان|taiwan", "اسپانیا": r"اسپانیا|spain"}


def learn(cfg: dict, crawled: pd.DataFrame, min_pages: int = 3, min_share: float = 0.75) -> dict[str, str]:
    from .routing import brand_countries, find_brand
    p = cfg["_data_dir"] / "brand_countries.csv"
    counts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    pages: collections.Counter = collections.Counter()
    # every page read so far (the crawl pool and every run), each page once
    from .io import read_csv
    from .pool import pool_path
    parts = [crawled] if crawled is not None else []
    if pool_path(cfg).exists():
        parts.append(read_csv(pool_path(cfg)))
    for other in (cfg["_data_dir"] / "runs").glob("*/03_crawled.csv"):
        parts.append(read_csv(other))
    allp = pd.concat([x for x in parts if x is not None and len(x)], ignore_index=True).fillna("") if parts else None
    if allp is not None and len(allp):
        for r in allp.drop_duplicates("url").to_dict("records"):
            if r.get("source_type") == "foreign":
                continue
            fb = find_brand(str(r.get("name") or ""))
            if not fb:
                continue
            pages[fb[0]] += 1
            t = f"{r.get('name', '')} {r.get('short_description', '')} {str(r.get('description') or '')[:1500]}".lower()
            for c, pat in SAID.items():
                if re.search(pat, t):
                    counts[fb[0]][c] += 1
    table = brand_countries()
    rows, out = [], {}
    for b, n in pages.items():
        cc = counts.get(b, collections.Counter())
        top, k = (cc.most_common(1) or [("", 0)])[0]
        total = sum(cc.values())
        country = top if n >= min_pages and k >= 3 and total and k / total >= min_share else ""
        if country and not table.get(b):
            out[b] = country
        rows.append({"brand": b, "country": country, "pages": n,
                     "counts": "|".join(f"{c}:{v}" for c, v in cc.most_common())})
    pd.DataFrame(rows, columns=["brand", "country", "pages", "counts"]).to_csv(p, index=False, encoding="utf-8-sig")
    if out:
        log.info("brand countries learned from the crawl: %s", ", ".join(f"{b}={c}" for b, c in sorted(out.items())))
    return out
