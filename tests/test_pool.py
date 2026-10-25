import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd


def page(url, name, days=0):
    return {"url": url, "name": name, "price_toman": "1000000", "source_type": "iran", "image_urls": "[\"x\"]",
            "crawled_at": (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds"),
            "taxonomy_id": "T0", "breadcrumb": "x > y"}


class Pool(unittest.TestCase):
    def test_pages_offered_to_later_runs(self):
        from crawler.pool import add_to_pool, from_pool
        cfg = {"_data_dir": Path(tempfile.mkdtemp()), "pool": {"max_age_days": 30}}
        add_to_pool(cfg, pd.DataFrame([
            page("u1", "اره عمود بر رونیکس 750 وات مدل 4111"),
            page("u2", "اره گرد بر توسن مدل 5067SC"),
            page("u3", "اره عمود بر رونیکس مدل 4111 قدیمی", days=60),       # too old
        ]).fillna(""))
        q = [{"taxonomy_id": "A", "breadcrumb": "ابزار برقی > اره عمود بر > اره عمود بر رونیکس 4111", "query": "q"},
             {"taxonomy_id": "B", "breadcrumb": "آسانسور > کرکره و درب برقی > چشمی گرد", "query": "q"}]
        rows = from_pool(cfg, q, set())
        self.assertEqual([(r["taxonomy_id"], r["url"]) for r in rows], [("A", "u1")])
        self.assertIn("from_pool", rows[0]["price_flags"])
        self.assertEqual(from_pool(cfg, q, {"A|u1"}), [])                  # already in this run


if __name__ == "__main__":
    unittest.main()
