"""Command line: python -m crawler <step> [options]

Steps, in order (each reads the previous step's file in data/runs/<run>/):
  queue    taxonomy -> 01_queue.csv
  search   queue -> 02_search_hits.csv
  crawl    hits -> 03_crawled.csv            (needs the Iranian internet)
  filter   AI relevance check -> 04_filtered.csv   (needs Claude API access)
  dedupe   merge + compare with site -> 05_new / 05_updates / 05_review
  export   WooCommerce import files -> 06_*.csv
  stats    row counts and quality numbers for a run
  catalog  a supplier's price list (PDF / Excel) -> variable products as drafts (crawler/catalog.py):
           python -m crawler catalog --run catalog-garoosi --file list.pdf --brand گروسی --category "تجهیزات استخر"
           python -m crawler catalog --run catalog-garoosi --publish 5 [--dry-run]
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
    ap.add_argument("step", choices=["queue", "search", "crawl", "filter", "dedupe", "export", "stats", "catalog"])
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
    g3 = ap.add_argument_group("catalog options")
    g3.add_argument("--file", help="catalog: the price list (PDF, xlsx or csv)")
    for k in ("brand", "supplier", "category", "maker-site", "price-factor", "attribute"):
        g3.add_argument(f"--{k}", help=f"catalog: {k.replace('-', '_')} setting of the run")
    g3.add_argument("--unit", choices=["auto", "rial", "toman", "usd"], help="catalog: unit of the printed prices")
    g3.add_argument("--steps", nargs="*", help="catalog: only these steps (read structure prices category photos describe)")
    g3.add_argument("--publish", type=int, metavar="N", help="catalog: send N products to the site (0 = all)")
    g3.add_argument("--dry-run", action="store_true", help="catalog: check the publish payloads, send nothing")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(a.config)
    if a.step == "catalog":
        import json
        from pathlib import Path
        from . import catalog as C
        if a.file:
            C.add_source(cfg, a.run, Path(a.file).read_bytes(), Path(a.file).name)
        opts = {k: getattr(a, k.replace("-", "_")) for k in ("brand", "supplier", "category", "maker-site",
                                                              "price-factor", "attribute", "unit")}
        C.save_settings(cfg, a.run, {k.replace("-", "_"): v for k, v in opts.items() if v is not None})
        if a.publish is None or a.steps:
            res = C.run_steps(cfg, a.run, a.steps or None)
            print(json.dumps(res, ensure_ascii=False, indent=1))
        if a.publish is not None:
            from .catalog_publish import publish
            res = publish(cfg, a.run, limit=a.publish, dry_run=a.dry_run)
            print(json.dumps(res["counts"], ensure_ascii=False))
        return 0
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
