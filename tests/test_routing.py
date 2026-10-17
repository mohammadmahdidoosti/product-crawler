"""Routing: free rules, the market probe, and the 100-batch table cycle (search stubbed, no credit used)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from crawler import routing_state as RS
from crawler.config import load_config
from crawler.io import FILES, read_csv
from crawler.probe import classify_results, decide, group_query
from crawler.routing import rule_route

CFG_R = {"routing": {"iran_min_priced": 3, "foreign_max_shops": 1}}


class Rules(unittest.TestCase):
    def test_brand_goes_iran(self):
        r = rule_route("شیرآلات > اکچویتور برقی و محرک شیر > اکچویتور برقی نوجیکس")
        self.assertEqual((r["decision"], r["kind"]), ("iran", "brand"))

    def test_level1_title_is_not_a_brand(self):
        r = rule_route("مصالح و شیمی ساختمان > چسب کاشی")
        self.assertEqual(r["kind"], "local_category")       # by the L1 list, not by a brand in the title

    def test_part_word_below_level1(self):
        r = rule_route("لوازم برقی > برد یخچال")
        self.assertEqual(r["decision"], "iran")

    def test_generic_is_open(self):
        r = rule_route("شیرآلات > اکچویتور برقی و محرک شیر > اکچویتور برقی خطی")
        self.assertEqual((r["decision"], r["kind"]), ("probe", "generic"))

    def test_not_a_brand_word(self):                         # پلیکا is a material, not a brand
        self.assertNotEqual(rule_route("لوله و اتصالات > لوله پلیکا")["kind"], "brand")


class Probe(unittest.TestCase):
    def test_query(self):
        self.assertEqual(group_query("شیرآلات > اکچویتور برقی و محرک شیر > محرک الکتریکی"),
                         "خرید محرک الکتریکی قیمت")
        self.assertEqual(group_query("تصفیه آب > ممبران"), "خرید ممبران تصفیه آب قیمت")

    def test_classify(self):
        organic = [
            {"link": "https://shop1.ir/product/a", "title": "اکچویتور برقی", "snippet": "۱۲,۵۰۰,۰۰۰ تومان"},
            {"link": "https://shop1.ir/product/b", "title": "اکچویتور", "snippet": ""},      # same shop
            {"link": "https://torob.com/p/x", "title": "اکچویتور", "snippet": "۱۰۰ تومان"},  # aggregator
            {"link": "https://shop2.com/shop/x", "title": "خرید محرک برقی", "snippet": ""},
            {"link": "https://blog.ir/what-is", "title": "اکچویتور چیست", "snippet": ""},      # article
            {"link": "https://example.com/item", "title": "Electric actuator", "snippet": ""},  # not Iranian
        ]
        c = classify_results(organic)
        self.assertEqual((c["shops"], c["priced"]), (2, 1))
        self.assertEqual(c["domains"][0]["domain"], "shop1.ir")

    def test_decide(self):
        self.assertEqual(decide({"shops": 5, "priced": 3}, CFG_R)[0], "iran")
        self.assertEqual(decide({"shops": 1, "priced": 0}, CFG_R)[0], "foreign")
        self.assertEqual(decide({"shops": "4", "priced": "1"}, CFG_R)[0], "both")


class TableCycle(unittest.TestCase):
    def cfg(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = load_config()
        cfg["_data_dir"] = tmp
        (tmp / "runs").mkdir()
        (tmp / "cache").mkdir()
        rows = []
        leaves = ["اکچویتور برقی خطی", "اکچویتور برقی کوچک", "اکچویتور برقی نوجیکس"]
        rows.append({"ID": "1", "Level 1 (دسته اصلی)": "شیرآلات", "Level 2 (زیردسته سطح ۱)": "اکچویتور برقی و محرک شیر",
                     "Level 3 (زیردسته سطح ۲)": "", "Level 4 (زیردسته سطح ۳)": "",
                     "Full Breadcrumb Path": "شیرآلات > اکچویتور برقی و محرک شیر", "Parent_ID": "",
                     "Is_Inferred_Parent": "False", "Site_Product_Count (تعداد محصول)": "5",
                     "Review_Priority (اولویت بررسی)": ""})
        for i, leaf in enumerate(leaves, 2):
            rows.append({"ID": str(i), "Level 1 (دسته اصلی)": "شیرآلات",
                         "Level 2 (زیردسته سطح ۱)": "اکچویتور برقی و محرک شیر", "Level 3 (زیردسته سطح ۲)": leaf,
                         "Level 4 (زیردسته سطح ۳)": "",
                         "Full Breadcrumb Path": f"شیرآلات > اکچویتور برقی و محرک شیر > {leaf}", "Parent_ID": "1",
                         "Is_Inferred_Parent": "False", "Site_Product_Count (تعداد محصول)": "0",
                         "Review_Priority (اولویت بررسی)": ""})
        tp = tmp / "tax.csv"
        pd.DataFrame(rows).to_csv(tp, index=False, encoding="utf-8-sig")
        cfg["taxonomy"] = {"file": str(tp), "exclude_level1": []}
        return cfg

    def test_build_probe_review_queue(self):
        cfg = self.cfg()
        s = RS.build(cfg, mode="gap", batch_size=100)
        self.assertEqual(s["rows"], 3)
        df = RS.load(cfg)
        self.assertEqual(df.set_index("taxonomy_id").at["4", "decision"], "iran")    # brand نوجیکس
        self.assertTrue((df["english"] != "").all())                                  # glossary term filled

        fake = {"group": "", "query": "q", "shops": 6, "priced": 1, "results": 10, "domains_json": "[]",
                "searched_at": "now"}
        with mock.patch.object(RS, "run_probe", side_effect=lambda c, g: {**fake, "group": g}) as rp, \
                mock.patch.object(RS, "record_usage", return_value=1):
            out = RS.probe_batch(cfg, 1)
            self.assertEqual(rp.call_count, 1)                   # one search for the whole group
            RS.probe_batch(cfg, 1)
            self.assertEqual(rp.call_count, 1)                   # cached in probes.csv: no second search
        self.assertEqual(out["searched"], 1)
        df = RS.load(cfg).set_index("taxonomy_id")
        self.assertEqual(df.at["2", "decision"], "both")
        self.assertEqual(df.at["4", "decision"], "iran")         # rule decision is not overwritten

        with self.assertLogs("crawler.routing_state", "INFO") as logs:
            r = RS.review_batch(cfg, 1, [{"taxonomy_id": "3", "review": "wrong", "final_route": "foreign",
                                          "note": "کالای عمومی"}])
        self.assertEqual((r["rows"], r["wrong"]), (3, 1))
        line = next(x for x in logs.output if "ROUTING_REVIEW" in x)
        self.assertEqual(json.loads(line.split("ROUTING_REVIEW ", 1)[1])["wrong"][0]["final_route"], "foreign")

        RS.build(cfg, mode="gap")                                # rebuild keeps the review
        self.assertEqual(RS.load(cfg).set_index("taxonomy_id").at["3", "final_route"], "foreign")

        q = RS.make_queue(cfg, 1, "batch-1")
        self.assertEqual(q["rows"], 3)
        qq = read_csv(cfg["_data_dir"] / "runs" / "batch-1" / FILES["queue"]).set_index("taxonomy_id")
        self.assertEqual(qq.at["3", "route"], "foreign")
        self.assertEqual(qq.at["2", "route"], "both")
        self.assertTrue(qq.at["2", "english"])


if __name__ == "__main__":
    unittest.main()


class ForeignSearch(unittest.TestCase):
    def test_leaf_terms_differ(self):
        from crawler.terms import leaf_english
        g, en = "اکچویتور برقی و محرک شیر", "electric valve actuator"
        self.assertEqual(leaf_english("اکچویتور برقی تدریجی", g, en), "electric valve actuator modulating")
        self.assertEqual(leaf_english("محرک الکتریکی شیر سه طرفه", g, en), "electric valve actuator 3 way")
        self.assertEqual(leaf_english("اکچویتور برقی ۱/۲ اینچ", g, en), "electric valve actuator 1/2 inch")
        self.assertEqual(leaf_english("اکچویتور برقی نوجیکس", g, en), en)   # brand words are dropped
        self.assertEqual(leaf_english("x", g, ""), "")

    def test_supplier_cap_and_shorter_retry(self):
        from crawler.search import search_mic
        links = "".join(f'<a href="https://big.en.made-in-china.com/product/{i}/x.html" title="Valve actuator {i}">a</a>'
                        f'<a href="https://big.en.made-in-china.com/product/{i}/x.html">US$100.00</a>' for i in range(6))
        links += '<a href="https://small.en.made-in-china.com/product/9/y.html" title="Small supplier actuator">b</a>'

        class F:
            urls = []

            def get(self, url):
                self.urls.append(url)
                return {"status": 200, "text": f"<html>{links}</html>"} if "Modulating" not in url \
                    else {"status": 404, "text": ""}
        f = F()
        row = {"taxonomy_id": "1", "query": "q", "route": "foreign"}
        hits = search_mic(f, row, "electric valve actuator modulating", 8)
        self.assertIn("Modulating", f.urls[0])
        self.assertTrue(f.urls[1].endswith("Electric_Valve_Actuator.html"))  # retried without the last word
        self.assertTrue(f.urls[2].endswith("Electric_Valve_Actuator-2.html"))  # then its page 2
        self.assertEqual([h["source_domain"] for h in hits].count("big.en.made-in-china.com"), 2)
        self.assertIn("small.en.made-in-china.com", [h["source_domain"] for h in hits])

    def test_english_relevance(self):
        from crawler.rules import judge
        c = "شیرآلات > اکچویتور برقی و محرک شیر > اکچویتور برقی شیر پروانه ای"
        leaf, en, g = "اکچویتور برقی شیر پروانه ای", "electric valve actuator butterfly", "electric valve actuator"
        ok = lambda n: judge(c, leaf, {"name": n}, en, g)["relevant"]
        self.assertTrue(ok("Electric Butterfly Valve Actuators DN100"))
        self.assertFalse(ok("047133062D Box Butterfly for Fabia VW"))
        self.assertFalse(ok("Micro Limit Switch for Electric Pallet"))
        self.assertTrue(ok("اکچویتور برقی پروانه ای"))          # Persian names still use the Persian rules


class Ranking(unittest.TestCase):
    def test_best_n_with_seller_cap(self):
        from crawler.ranking import select_top
        rows = []
        for i in range(6):   # 6 good products from one seller, 2 weaker from others
            rows.append({"taxonomy_id": "1", "sellers": "big", "price_toman": "100", "model": "A1",
                         "specs_json": json.dumps({str(k): k for k in range(8)}), "image_urls": '["a","b","c"]',
                         "relevance_confidence": "1", "n_sources": "1"})
        for s in ("x", "y"):
            rows.append({"taxonomy_id": "1", "sellers": s, "price_toman": "100", "model": "",
                         "specs_json": "{}", "image_urls": "[]", "relevance_confidence": "0.6", "n_sources": "1"})
        rows.append({"taxonomy_id": "1", "sellers": "z", "price_toman": "", "model": "",
                     "specs_json": "{}", "image_urls": "[]", "relevance_confidence": "0.5", "n_sources": "1"})
        kept, dropped = select_top(pd.DataFrame(rows), 5, max_per_seller=3)
        self.assertEqual(len(kept), 5)
        self.assertEqual(list(kept["sellers"]).count("big"), 3)        # cap keeps room for others
        self.assertIn("x", list(kept["sellers"]))
        self.assertNotIn("z", list(kept["sellers"]))                   # no price -> lowest score
        self.assertEqual(len(dropped), 4)
