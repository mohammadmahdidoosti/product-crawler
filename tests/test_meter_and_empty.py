"""31 Oct: hose prices per metre were held as too cheap; a crawl that found nothing stopped with KeyError."""
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from crawler import pipeline as P
from crawler.extract import PER_METER
from crawler.io import FILES, HITS_COLS, read_csv, write_csv
from crawler.normalize import norm_text


class PerMetre(unittest.TestCase):
    def test_page_says_per_metre(self):
        for t in ("قیمت هر متر: 50,700 تومان", "فروش به صورت متری", "قیمت یک متر شیلنگ", "50700 تومان / متر"):
            self.assertTrue(PER_METER.search(norm_text(t)), t)

    def test_not_per_metre(self):
        for t in ("متر لیزری بوش 40 متری", "طول شیلنگ 10 متر", "قیمت 2,500,000 تومان"):
            self.assertFalse(PER_METER.search(norm_text(t)), t)

    def rec(self, v, unit=""):
        return {"url": "https://a.ir/p", "name": "شیلنگ 1/2 اینچ دولا سیم", "brand": "",
                "breadcrumb": "هیدرولیک > شیلنگ هیدرولیک", "price_toman": str(v), "sale_unit": unit}

    def test_per_metre_price_not_held(self):
        out = P.price_sanity([self.rec(50_700, "متر")])
        self.assertFalse(str(out[0].get("price_check", "")).startswith("suspicious"))

    def test_same_price_without_unit_still_held(self):
        out = P.price_sanity([self.rec(50_700)])
        self.assertTrue(str(out[0].get("price_check", "")).startswith("suspicious"))

    def test_too_cheap_even_per_metre_is_held(self):
        out = P.price_sanity([self.rec(3_000, "متر")])
        self.assertTrue(str(out[0].get("price_check", "")).startswith("suspicious"))


class EmptyCrawl(unittest.TestCase):
    def test_dedupe_after_nothing_found(self):
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from test_e2e import EndToEnd
        EndToEnd.setUpClass()
        try:
            t = EndToEnd()
            cfg, run = t.cfg(), "empty"
            P.step_queue(cfg, run)
            d = t.tmp / "runs" / run
            write_csv(pd.DataFrame(columns=HITS_COLS), d / FILES["hits"], HITS_COLS)
            P.step_crawl(cfg, run)
            P.step_filter(cfg, run)
            P.step_dedupe(cfg, run, None)
            P.step_export(cfg, run)
            self.assertEqual(len(read_csv(d / FILES["new"])), 0)
        finally:
            EndToEnd.tearDownClass()


if __name__ == "__main__":
    unittest.main()
