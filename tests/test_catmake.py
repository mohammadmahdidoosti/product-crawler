"""Category maker: naming rules, taxonomy + map rows, site terms made once, placements."""
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from crawler import catmake as C
from crawler.io import read_csv, write_csv

TAX = pd.DataFrame([
    {"ID": "1", "Level 1 (دسته اصلی)": "ابزار برقی و شارژی", "Level 2 (زیردسته سطح ۱)": "", "Level 3 (زیردسته سطح ۲)": "",
     "Level 4 (زیردسته سطح ۳)": "", "Full Breadcrumb Path": "ابزار برقی و شارژی", "Parent_ID": "", "Category_Name": "ابزار برقی و شارژی"},
    {"ID": "2", "Level 1 (دسته اصلی)": "ابزار برقی و شارژی", "Level 2 (زیردسته سطح ۱)": "میخ کوب", "Level 3 (زیردسته سطح ۲)": "",
     "Level 4 (زیردسته سطح ۳)": "", "Full Breadcrumb Path": "ابزار برقی و شارژی > میخ کوب", "Parent_ID": "1", "Category_Name": "میخ کوب"},
    {"ID": "3", "Level 1 (دسته اصلی)": C.GARDEN, "Level 2 (زیردسته سطح ۱)": "", "Level 3 (زیردسته سطح ۲)": "",
     "Level 4 (زیردسته سطح ۳)": "", "Full Breadcrumb Path": C.GARDEN, "Parent_ID": "", "Category_Name": C.GARDEN},
])


class FakeWC:
    def __init__(self):
        self.terms = [{"id": 900, "name": "میخ نامریی", "parent": 20}]   # already on the site
        self.created = []

    def _call(self, fn, what, tries=3):
        return fn()

    def _wc(self, method, path, params=None, json=None):
        if method == "GET":
            return [t for t in self.terms if params["search"] in t["name"]]
        t = {"id": 1000 + len(self.created), "name": json["name"], "parent": json["parent"]}
        self.created.append(t)
        self.terms.append(t)
        return t


class Names(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(C.clean_name("⚡قیمت زنجیر"), "زنجیر")
        self.assertEqual(C.clean_name("دستکش باغبانیgardening"), "دستکش باغبانی")
        self.assertEqual(C.clean_name("قیمت و خرید میخکوب بادی نید صنعت"), "میخکوب بادی")
        self.assertEqual(C.clean_name("میخکوب dx"), "میخکوب dx")          # a model word stays

    def test_bad(self):
        self.assertIsNone(C.bad_name("میخ نامریی"))
        self.assertTrue(C.bad_name("تاریخ 1405-08-04"))
        self.assertTrue(C.bad_name("playstation باندل"))
        self.assertTrue(C.bad_name("آب مرکبات"))
        self.assertTrue(C.bad_name("ایران ابزار"))

    def test_garden_moves(self):
        self.assertEqual(C.garden_parent("ابزار برقی و شارژی > فارسی بر > اره باغبانی"),
                         f"{C.GARDEN} > ابزار باغبانی > اره باغبانی")
        self.assertEqual(C.garden_parent(f"{C.GARDEN} > ابزار باغبانی"), f"{C.GARDEN} > ابزار باغبانی")


class Make(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        write_csv(TAX, d / "taxonomy.csv")
        write_csv(pd.DataFrame([{"ID": "1", "wp_id": "10"}, {"ID": "2", "wp_id": "20"}, {"ID": "3", "wp_id": "30"}]),
                  d / "wp_categories.csv")
        self.cfg = {"_data_dir": d, "taxonomy": {"file": str(d / "taxonomy.csv")}}
        self.dec = pd.DataFrame([
            {"group": "میخ نامریی", "path": "ابزار برقی و شارژی > میخ کوب > میخ نامریی", "url": "u1"},
            {"group": "میخ نامریی", "path": "ابزار برقی و شارژی > میخ کوب > میخ نامریی", "url": "u2"},
            {"group": "اره باغبانی", "path": "ابزار برقی و شارژی > فارسی بر > اره باغبانی", "url": "u3"},
            {"group": "تاریخ", "path": "ابزار برقی و شارژی > میخ کوب > تاریخ 1405-08-04", "url": "u4"},
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_writes_nothing(self):
        before = (Path(self.tmp.name) / "taxonomy.csv").read_bytes()
        res = C.make(self.cfg, self.dec, dry_run=True)
        self.assertEqual(len(res["made"]), 3)              # میخ نامریی, ابزار باغبانی, اره باغبانی
        self.assertEqual(res["placed"], 3)
        self.assertEqual(len(res["rejected"]), 1)
        self.assertEqual((Path(self.tmp.name) / "taxonomy.csv").read_bytes(), before)
        self.assertFalse((Path(self.tmp.name) / C.PLACEMENTS).exists())

    def test_apply_then_again_makes_nothing_twice(self):
        wc = FakeWC()
        res = C.make(self.cfg, self.dec, dry_run=False, wc=wc)
        self.assertEqual(len(res["made"]), 3)
        self.assertEqual(len(wc.created), 2)               # میخ نامریی was on the site already
        d = Path(self.tmp.name)
        tax = read_csv(d / "taxonomy.csv")
        row = tax[tax["Full Breadcrumb Path"] == f"{C.GARDEN} > ابزار باغبانی > اره باغبانی"].iloc[0]
        parent = tax[tax["Full Breadcrumb Path"] == f"{C.GARDEN} > ابزار باغبانی"].iloc[0]
        self.assertEqual(row["Parent_ID"], parent["ID"])
        wp = dict(zip(*read_csv(d / "wp_categories.csv")[["ID", "wp_id"]].T.values))
        nail = tax[tax["Full Breadcrumb Path"].str.endswith("میخ نامریی")].iloc[0]["ID"]
        self.assertEqual(wp[nail], "900")
        self.assertEqual(wc.created[0]["parent"], 30)      # ابزار باغبانی under the garden section
        pl = read_csv(d / C.PLACEMENTS)
        self.assertEqual(set(pl["url"]), {"u1", "u2", "u3"})
        again = C.make(self.cfg, self.dec, dry_run=False, wc=wc)
        self.assertEqual(again["made"], [])
        self.assertEqual(len(wc.created), 2)
        self.assertEqual(len(read_csv(d / "taxonomy.csv")), len(tax))

    def test_placements_join_the_run(self):
        C.make(self.cfg, self.dec, dry_run=False, wc=FakeWC())
        crawled = pd.DataFrame([{"url": "u1", "name": "میخ نامریی ۱", "taxonomy_id": "2", "breadcrumb": "x", "price_toman": "1"},
                                {"url": "u9", "name": "چیز دیگر", "taxonomy_id": "2", "breadcrumb": "x", "price_toman": "1"}])
        df = pd.DataFrame(columns=["url", "name", "taxonomy_id", "breadcrumb"])
        out = C.apply_placements(self.cfg, df, crawled)
        self.assertEqual(list(out["url"]), ["u1"])
        self.assertTrue(out.iloc[0]["breadcrumb"].endswith("میخ نامریی"))


if __name__ == "__main__":
    unittest.main()
