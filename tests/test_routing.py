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
