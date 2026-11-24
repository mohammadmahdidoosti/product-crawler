"""The crawl pool: every product page any run has read, kept for the runs that come after.

A batch reads ~1,400 product pages but keeps well under half of them for its own 100
categories: the rest are real products (other models, other brands, other types) that a later
batch's categories may want. Each run's pages go to DATA_DIR/crawl_pool.csv (one row per page,
newest wins); when a later run crawls, pages from the pool that fit its categories are added to
its crawl as if found again — no search, no fetch — and then go through the same filter.
Prices age, so only pages read in the last `pool.max_age_days` are offered.

(24 Nov) The pool passed 700 MB / 190k pages. It was read whole into memory by every lane and
rewritten whole after every batch, so each lane went past 3 GB and six lanes ran the server out of
memory. Now a run's pages are appended to the end of the file (a lock file keeps two lanes from
writing at once) and the file is read a piece at a time; "newest wins" is decided while reading.
When the file grows past `pool.compact_mb` it is rewritten once, a piece at a time, without old
copies and pages older than max_age_days.
"""
from __future__ import annotations

import csv
import datetime as dt
import fcntl
import logging
import os
import re
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from .io import CRAWLED_COLS, ENCODING, iter_csv
from .normalize import norm_text

log = logging.getLogger(__name__)
PAGE_COLS = [c for c in CRAWLED_COLS if c not in ("taxonomy_id", "breadcrumb", "query", "route")]


def pool_path(cfg: dict) -> Path:
    return cfg["_data_dir"] / "crawl_pool.csv"


@contextmanager
def _lock(cfg: dict, shared: bool):
    """Readers share the lock, a writer has it alone: nobody reads half an appended row."""
    f = open(cfg["_data_dir"] / ".crawl_pool.lock", "a")
    try:
        fcntl.flock(f, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _header(p: Path) -> list[str]:
    with p.open(encoding=ENCODING, newline="") as f:
        return next(csv.reader(f), [])


def add_to_pool(cfg: dict, crawled: pd.DataFrame) -> int:
    """Pages with a name and a price are kept; appended, a newer read of a page wins when the pool is read."""
    if crawled is None or crawled.empty:
        return 0
    pages = crawled[(crawled["name"] != "") & (crawled["price_toman"] != "")]
    pages = pages.reindex(columns=PAGE_COLS, fill_value="").drop_duplicates("url", keep="last").fillna("")
    if pages.empty:
        return 0
    p = pool_path(cfg)
    with _lock(cfg, shared=False):
        new = not p.exists() or p.stat().st_size == 0
        cols = PAGE_COLS if new else (_header(p) or PAGE_COLS)
        with p.open("a", encoding=ENCODING if new else "utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
            if new:
                w.writeheader()
            for r in pages.to_dict("records"):
                w.writerow({c: r.get(c, "") for c in cols})
    limit = float(cfg.get("pool", {}).get("compact_mb", 1200))
    if limit and p.stat().st_size > limit * 1024 * 1024:
        compact_pool(cfg)
    return len(pages)


def _latest(cfg: dict, p: Path) -> dict[str, str]:
    """url -> the newest crawled_at in the file (two copies of a page: the newer one counts)."""
    out: dict[str, str] = {}
    for part in iter_csv(p, ["url", "crawled_at"]):
        for u, t in zip(part["url"], part["crawled_at"]):
            if t >= out.get(u, ""):
                out[u] = t
    return out


def compact_pool(cfg: dict) -> int:
    """Rewrite the pool without old copies of a page and without pages past max_age_days, a piece at a time."""
    p = pool_path(cfg)
    if not p.exists():
        return 0
    cut = (dt.datetime.now() - dt.timedelta(days=float(cfg.get("pool", {}).get("max_age_days", 30)))
           ).isoformat(timespec="seconds")
    tmp = p.with_name(f".{p.name}.{os.getpid()}.compact.tmp")
    kept = 0
    with _lock(cfg, shared=False):
        latest = _latest(cfg, p)
        cols = _header(p) or PAGE_COLS
        seen: set[str] = set()
        try:
            with tmp.open("w", encoding=ENCODING, newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
                w.writeheader()
                for part in iter_csv(p, cols):
                    for r in part.to_dict("records"):
                        u, t = r["url"], r["crawled_at"]
                        if t < cut or t != latest.get(u) or u in seen:
                            continue
                        seen.add(u)
                        w.writerow(r)
                        kept += 1
            os.replace(tmp, p)
        finally:
            if tmp.exists():
                tmp.unlink()
    log.info("crawl pool compacted: %d page(s) kept", kept)
    return kept


def from_pool(cfg: dict, queue: list[dict], have: set[str]) -> list[dict]:
    """Crawl rows for the run's categories from pages read by earlier runs.
    have: 'taxonomy_id|url' already in this run's crawl."""
    from .harvest import strict
    from .rules import STOP, judge
    p = pool_path(cfg)
    if not p.exists():
        return []
    pc = cfg.get("pool", {})
    cut = (dt.datetime.now() - dt.timedelta(days=float(pc.get("max_age_days", 30)))).isoformat(timespec="seconds")
    min_conf = float(pc.get("min_confidence", 0.85))
    wanted = []
    for q in queue:
        leaf = q["breadcrumb"].split(">")[-1].strip()
        words = [w for w in norm_text(leaf).split() if w not in STOP and len(w) > 1 and not re.search(r"\d", w)]
        if words:
            wanted.append((q, leaf, words))
    if not wanted:
        return []
    # one pass: the cheap word test on every page, only the pages that pass are kept (with every column)
    cands: list[tuple[int, dict]] = []                 # (index in wanted, page)
    with _lock(cfg, shared=True):
        latest = _latest(cfg, p)
        cols = _header(p) or PAGE_COLS
        for part in iter_csv(p, cols, chunk=5000):
            part = part[part["crawled_at"].astype(str) >= cut]
            if part.empty:
                continue
            hits: list[tuple[int, list[int]]] = []      # row position -> categories, before any row is copied
            for pos, (u, t, nm) in enumerate(zip(part["url"], part["crawled_at"], part["name"])):
                if t != latest.get(u):
                    continue                            # an older copy of a page read again later
                flat = norm_text(nm).replace(" ", "")
                ks = [i for i, (_q, _leaf, words) in enumerate(wanted)
                      if sum(1 for w in words if w in flat) / len(words) >= 0.5]
                if ks:
                    hits.append((pos, ks))
            if hits:
                recs = part.iloc[[pos for pos, _ in hits]].to_dict("records")
                for r, (_pos, ks) in zip(recs, hits):
                    cands += [(i, r) for i in ks]
            del part
    out = []
    seen: set[str] = set()
    cands.sort(key=lambda x: x[0])                      # category by category, as before
    for i, r in cands:
        q, leaf, _w = wanted[i]
        key = f"{q['taxonomy_id']}|{r['url']}"
        if key in have or key in seen:
            continue
        v = judge(q["breadcrumb"], leaf, r)
        # a page found for another category: held to the second-chance standard, not the
        # search's (a search result was at least found for this category's words)
        if not v["relevant"] or float(v["confidence"]) < min_conf or strict(
                {"leaf": leaf, "breadcrumb": q["breadcrumb"]}, r) is not None:
            continue
        seen.add(key)
        out.append({**r, "taxonomy_id": q["taxonomy_id"], "breadcrumb": q["breadcrumb"],
                    "query": q.get("query", leaf), "route": q.get("route", ""),
                    "price_flags": " | ".join(x for x in (str(r.get("price_flags") or ""), "from_pool") if x)})
    have |= seen
    return out
