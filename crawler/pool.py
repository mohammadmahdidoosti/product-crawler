"""The crawl pool: every product page any run has read, kept for the runs that come after.

A batch reads ~1,400 product pages but keeps well under half of them for its own 100
categories: the rest are real products (other models, other brands, other types) that a later
batch's categories may want. Each run's pages go to DATA_DIR/crawl_pool.csv (one row per page,
newest wins); when a later run crawls, pages from the pool that fit its categories are added to
its crawl as if found again — no search, no fetch — and then go through the same filter.
Prices age, so only pages read in the last `pool.max_age_days` are offered.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

import pandas as pd

from .io import CRAWLED_COLS, read_csv, write_csv
from .normalize import norm_text

log = logging.getLogger(__name__)
PAGE_COLS = [c for c in CRAWLED_COLS if c not in ("taxonomy_id", "breadcrumb", "query", "route")]


def pool_path(cfg: dict) -> Path:
    return cfg["_data_dir"] / "crawl_pool.csv"


def add_to_pool(cfg: dict, crawled: pd.DataFrame) -> int:
    """Pages with a name and a price are kept (one row per page; a newer read replaces an older one)."""
    if crawled is None or crawled.empty:
        return 0
    pages = crawled[(crawled["name"] != "") & (crawled["price_toman"] != "")]
    pages = pages.reindex(columns=PAGE_COLS, fill_value="").drop_duplicates("url", keep="last")
    p = pool_path(cfg)
    old = read_csv(p) if p.exists() else pd.DataFrame(columns=PAGE_COLS)
    old = old[~old["url"].isin(set(pages["url"]))]
    allp = pd.concat([old, pages], ignore_index=True).fillna("")
    write_csv(allp.reindex(columns=PAGE_COLS, fill_value=""), p)
    return len(pages)


def _fresh(pool: pd.DataFrame, days: float) -> pd.DataFrame:
    cut = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    return pool[pool["crawled_at"].astype(str) >= cut]


def from_pool(cfg: dict, queue: list[dict], have: set[str]) -> list[dict]:
    """Crawl rows for the run's categories from pages read by earlier runs.
    have: 'taxonomy_id|url' already in this run's crawl."""
    from .harvest import strict
    from .rules import STOP, judge
    p = pool_path(cfg)
    if not p.exists():
        return []
    pc = cfg.get("pool", {})
    pool = _fresh(read_csv(p), float(pc.get("max_age_days", 30)))
    min_conf = float(pc.get("min_confidence", 0.85))
    if pool.empty:
        return []
    names = [norm_text(n) for n in pool["name"]]
    recs = pool.to_dict("records")
    out = []
    for q in queue:
        leaf = q["breadcrumb"].split(">")[-1].strip()
        words = [w for w in norm_text(leaf).split() if w not in STOP and len(w) > 1 and not re.search(r"\d", w)]
        if not words:
            continue
        for nm, r in zip(names, recs):
            if f"{q['taxonomy_id']}|{r['url']}" in have:
                continue
            flat = nm.replace(" ", "")
            if sum(1 for w in words if w in flat) / len(words) < 0.5:        # cheap test first
                continue
            v = judge(q["breadcrumb"], leaf, r)
            # a page found for another category: held to the second-chance standard, not the
            # search's (a search result was at least found for this category's words)
            if not v["relevant"] or float(v["confidence"]) < min_conf or strict(
                    {"leaf": leaf, "breadcrumb": q["breadcrumb"]}, r) is not None:
                continue
            out.append({**r, "taxonomy_id": q["taxonomy_id"], "breadcrumb": q["breadcrumb"],
                        "query": q.get("query", leaf), "route": q.get("route", ""),
                        "price_flags": " | ".join(x for x in (str(r.get("price_flags") or ""), "from_pool") if x)})
            have.add(f"{q['taxonomy_id']}|{r['url']}")
    return out
