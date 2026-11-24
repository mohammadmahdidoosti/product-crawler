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
    # (24 Nov) read a piece at a time and only the 5 columns used: the pool and 220 runs are ~5 GB together,
    # read whole they took each lane past 3 GB of memory
    from .io import iter_csv
    from .pool import pool_path
    cols = ["url", "name", "short_description", "description", "source_type"]

    def parts():
        if crawled is not None and len(crawled):
            yield crawled.reindex(columns=cols, fill_value="").fillna("")
        srcs = [pool_path(cfg)] if pool_path(cfg).exists() else []
        srcs += sorted((cfg["_data_dir"] / "runs").glob("*/03_crawled.csv"))
        for s in srcs:
            yield from iter_csv(s, cols)

    seen: set[str] = set()
    for part in parts():
        for u, name, short, desc, st in zip(part["url"], part["name"], part["short_description"],
                                            part["description"], part["source_type"]):
            if u in seen:
                continue
            seen.add(u)
            if st == "foreign":
                continue
            fb = find_brand(str(name or ""))
            if not fb:
                continue
            pages[fb[0]] += 1
            t = f"{name} {short} {str(desc or '')[:1500]}".lower()
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
