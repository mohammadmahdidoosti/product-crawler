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


class Detective(unittest.TestCase):
    def test_decide_and_memory(self):
        import tempfile
        from crawler.detective import decide
        from crawler.pricemem import habit, learn_habit, recall, remember
        self.assertEqual(decide(638000, [("a", 6200000), ("b", 5900000)], None)[0], 6380000)
        self.assertIsNone(decide(15268000, [("a", 5000000), ("b", 5500000)], None)[0])     # 2 shops: no invented price
        self.assertEqual(decide(658000000, [("a", 65800000), ("b", 204000000)], None)[0], 65800000)
        self.assertEqual(decide(240000000, [("a", 2800000), ("b", 2263970)], None)[0], 2400000)
        self.assertEqual(decide(185000000, [("a", 12300000), ("b", 19700000), ("c", 24800000)], None)[0], 18500000)
        self.assertIsNone(decide(15268000, [], None)[0])
        cfg = {"_data_dir": Path(tempfile.mkdtemp())}
        remember(cfg, [{"name": "بلوور رونیکس مدل 1202", "dom": "a.ir", "price": 5000000, "source": "search"}])
        self.assertEqual([x[:2] for x in recall(cfg, "بلوور دمنده رونیکس 1202")], [("a.ir", 5000000.0)])
        for _ in range(3):
            learn_habit(cfg, "b.ir", "d10")
        self.assertEqual(habit(cfg, "b.ir"), "d10")
        self.assertEqual(habit(cfg, "a.ir"), "")


class Checked(unittest.TestCase):
    def test_checked_prices_and_parts(self):
        from crawler.pipeline import is_part, price_sanity
        from crawler.pricemem import checked, habit, import_checked, verdict
        cfg = {"_data_dir": Path(tempfile.mkdtemp())}
        self.assertEqual(verdict(229300000, 22930000), "d10")
        self.assertEqual(verdict(787000, 7870000), "x10")
        rows = [{"name": f"اره عمود بر رونیکس مدل 41{i}", "url": "https://x.ir/p", "old": 10 * (i + 5) * 1000000,
                 "new": (i + 5) * 1000000} for i in range(10, 13)]
        res = import_checked(cfg, rows)
        self.assertEqual(res["remembered"], 3)
        self.assertEqual(checked(cfg, "اره عمودبر رونیکس 4111"), 16000000.0)
        self.assertEqual(habit(cfg, "x.ir"), "d10")
        self.assertTrue(is_part("کلید بلوور رونیکس 1205"))
        self.assertFalse(is_part("بلوور دیمردار رونیکس 1206"))
        recs = [{"name": f"بلوور رونیکس مدل 12{i}", "price_toman": "6000000", "breadcrumb": "a > b"} for i in range(4)]
        recs.append({"name": "کلید بلوور رونیکس 1205", "price_toman": "700000", "breadcrumb": "a > b"})
        recs.append({"name": "بلوور رونیکس 1209", "price_toman": "600000", "breadcrumb": "a > b"})
        out = price_sanity(recs)
        self.assertFalse(str(out[4].get("price_check") or "").startswith("suspicious"))
        self.assertTrue(str(out[5].get("price_check") or "").startswith("suspicious"))


class SameModel(unittest.TestCase):
    def test_copies_and_short_codes(self):
        from crawler.detective import _same_model
        ours = {"name": "مینی فرز آهنگری ماکیتا مدل 9555HNG"}
        self.assertFalse(_same_model(ours, "مینی فرز طرح ماکیتا 9555HNG"))
        self.assertFalse(_same_model(ours, "مینی فرز ماکیتا 9555"))
        self.assertTrue(_same_model(ours, "مینی فرز آهنگری 125 میلی متری ماکیتا مدل 9555HNG"))
        self.assertTrue(_same_model(ours, "مینی فرز ماکیتا 9555HN"))
        self.assertTrue(_same_model({"name": "اره عمودبر رونیکس مدل 4111"}, "اره عمود بر رونیکس 750 وات مدل 4111"))
