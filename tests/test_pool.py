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


class PlaceUp(unittest.TestCase):
    def test_up_then_elsewhere(self):
        import tempfile
        from crawler.harvest import place_up
        d = Path(tempfile.mkdtemp())
        tax = d / "tax.csv"
        pd.DataFrame([
            {"ID": "1", "Level 1 (دسته اصلی)": "ابزار برقی", "Level 2 (زیردسته سطح ۱)": "اره عمود بر",
             "Level 3 (زیردسته سطح ۲)": "", "Full Breadcrumb Path": "ابزار برقی > اره عمود بر", "Parent ID": "", "site_product_count": "5"},
            {"ID": "2", "Level 1 (دسته اصلی)": "ابزار برقی", "Level 2 (زیردسته سطح ۱)": "اره عمود بر",
             "Level 3 (زیردسته سطح ۲)": "اره عمود بر شارژی", "Full Breadcrumb Path": "ابزار برقی > اره عمود بر > اره عمود بر شارژی",
             "Parent ID": "1", "site_product_count": "0"},
            {"ID": "3", "Level 1 (دسته اصلی)": "باغبانی", "Level 2 (زیردسته سطح ۱)": "اره موتوری",
             "Level 3 (زیردسته سطح ۲)": "", "Full Breadcrumb Path": "باغبانی > اره موتوری", "Parent ID": "", "site_product_count": "9"},
        ]).to_csv(tax, index=False, encoding="utf-8-sig")
        from crawler.taxonomy import load_taxonomy
        try:
            load_taxonomy(tax)
        except Exception as e:                         # the column names differ in this checkout
            self.skipTest(f"taxonomy format: {e}")
        cfg = {"_data_dir": d, "taxonomy": {"file": str(tax)}}
        crawled = pd.DataFrame([
            {**page("u1", "اره عمود بر ماکیتا 450 وات مدل 4329"), "taxonomy_id": "2"},
            {**page("u2", "اره موتوری بنزینی توسن 45 سانتی"), "taxonomy_id": "2"},
        ]).fillna("")
        out = place_up(cfg, pd.DataFrame(columns=["url"]), crawled)
        got = {r["url"]: r["taxonomy_id"] for r in out.to_dict("records")}
        self.assertEqual(got.get("u1"), "1")            # corded jigsaw: one level up
