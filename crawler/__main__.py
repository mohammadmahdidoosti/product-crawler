"""Command line: python -m crawler <step> [options]

Steps, in order (each reads the previous step's file in data/runs/<run>/):
  queue    taxonomy -> 01_queue.csv
  search   queue -> 02_search_hits.csv
  crawl    hits -> 03_crawled.csv            (needs the Iranian internet)
  filter   AI relevance check -> 04_filtered.csv   (needs Claude API access)
  dedupe   merge + compare with site -> 05_new / 05_updates / 05_review
  export   WooCommerce import files -> 06_*.csv
  stats    row counts and quality numbers for a run
Every step can be stopped and re-run; finished rows are skipped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from .config import load_config
from . import pipeline as P


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m crawler", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["queue", "search", "crawl", "filter", "dedupe", "export", "stats"])
    ap.add_argument("--run", default=dt.date.today().isoformat(), help="run folder name (default: today)")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    g = ap.add_argument_group("queue options")
    g.add_argument("--mode", choices=["gap", "enrich", "all"], default="gap",
                   help="gap: leaves with no product on the site (default); enrich: leaves that have products")
    g.add_argument("--level1", help="only this top-level category (part of its name is enough)")
    g.add_argument("--path", dest="path_contains", help="only breadcrumbs containing this text")
    g.add_argument("--ids", nargs="*", help="only these taxonomy IDs")
    g.add_argument("--include-flagged", action="store_true", help="also include high-priority review rows")
    g.add_argument("--limit", type=int, help="first N queue rows (for a test run)")
    g2 = ap.add_argument_group("other step options")
    g2.add_argument("--llm-fallback", action="store_true", help="crawl: use Claude for pages with no product markup")
    g2.add_argument("--site-export", help="dedupe: WooCommerce product export CSV of caravansanat.com")
    g2.add_argument("--skip-filter", action="store_true", help="dedupe: use 03_crawled.csv (no AI filter)")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(a.config)
    if a.step == "queue":
        P.step_queue(cfg, a.run, mode=a.mode, level1=a.level1, path_contains=a.path_contains,
                     ids=a.ids, include_flagged=a.include_flagged, limit=a.limit)
    elif a.step == "search":
        P.step_search(cfg, a.run)
    elif a.step == "crawl":
        P.step_crawl(cfg, a.run, use_llm_fallback=a.llm_fallback)
    elif a.step == "filter":
        P.step_filter(cfg, a.run)
    elif a.step == "dedupe":
        P.step_dedupe(cfg, a.run, a.site_export, a.skip_filter)
    elif a.step == "export":
        P.step_export(cfg, a.run)
    print(P.stats(cfg, a.run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
