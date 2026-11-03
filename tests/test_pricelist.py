"""Supplier price lists -> site prices (crawler/pricelist.py, 3 Nov 2026)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from crawler import pricelist as PL


class Helpers(unittest.TestCase):
    def test_model_in_name(self):
        k = PL.model_key("VR2010-BLX")
        self.assertTrue(PL.contains_model("دریل شارژی ویوارکس مدل VR2010-BLX ۲۰ ولت", k))
        self.assertTrue(PL.contains_model("VR2010 BLX", k))
        k2 = PL.model_key("VR2010")
        self.assertFalse(PL.contains_model("مدل VR20105", k2))         # another model
        self.assertTrue(PL.contains_model("مدل VR2010-BLX", k2))      # a longer code of the family still holds it
        self.assertFalse(PL.contains_model("LE-710", PL.model_key("LE-71")))
        self.assertFalse(PL.contains_model("abc", "AB"))               # too short / no digit

    def test_prices(self):
        self.assertEqual(PL.to_toman("۱۴۳,۷۵۰,۰۰۰", "rial"), 14_375_000)
        self.assertEqual(PL.to_toman("3,980,000", "toman"), 3_980_000)
        self.assertIsNone(PL.to_toman("12", "toman"))
        self.assertEqual(PL.list_date_rank("لیست قیمت آبان ماه ۱۴۰۵"), 140508)
        self.assertEqual(PL.list_date_rank(""), 0)

    def test_newest_list_wins(self):
        rows = [{"brand": "VIVAREX", "model": "VR8016-AS", "name": "منگنه کوب", "price": "45,000,000", "unit": "rial",
                 "list_date": "مهر ۱۴۰۵", "file": "a"},
                {"brand": "VIVAREX", "model": "VR8016 AS", "name": "منگنه کوب", "price": "47,910,000", "unit": "rial",
                 "list_date": "آبان ۱۴۰۵", "file": "b"},
                {"brand": "VIVAREX", "model": "VR1608-BLX", "name": "دریل", "price": "75,290,000", "unit": "unknown",
                 "list_date": "", "file": "c"}]
        df = PL.merge_rows(rows)
        r = df.set_index("key")
        self.assertEqual(int(r.loc["VR8016AS", "toman"]), 4_791_000)
        self.assertEqual(r.loc["VR1608BLX", "toman"], "")                  # unit unknown: no price yet
        df = PL.merge_rows(rows, default_unit="rial")
        self.assertEqual(int(df.set_index("key").loc["VR1608BLX", "toman"]), 7_529_000)

    def test_same_date_disagreement_noted(self):
        rows = [{"brand": "", "model": "P3355", "name": "", "price": "259,000,000", "unit": "rial", "list_date": "مهر ۱۴۰۵", "file": "a"},
                {"brand": "", "model": "P3355", "name": "", "price": "295,000,000", "unit": "rial", "list_date": "مهر ۱۴۰۵", "file": "b"}]
        self.assertIn("بررسی", PL.merge_rows(rows).iloc[0]["note"])

    def test_list_price_longest_code(self):
        idx = {"VR2010": {"toman": 1}, "VR2010BLX": {"toman": 2}}
        self.assertEqual(PL.list_price({}, "دریل VR2010-BLX", idx=idx)["toman"], 2)
        self.assertIsNone(PL.list_price({}, "دریل VR3000", idx=idx))
        idx = {"3151": {"toman": 5, "brand_names": ["رونیکس", "ronix"]}}
        self.assertEqual(PL.list_price({}, "مینی فرز رونیکس مدل 3151", idx=idx)["toman"], 5)
        self.assertEqual(PL.list_price({}, "مینی فرز Ronix 3151", idx=idx)["toman"], 5)
        self.assertIsNone(PL.list_price({}, "پمپ پنتاکس 3151", idx=idx))


class FakeWC:
    def __init__(self, products, brands):
        self.products, self.brands, self.updates = {p["id"]: p for p in products}, brands, []

    def ok(self):
        return True

    def _call(self, fn, what):
        return fn()

    def _wc(self, method, path, params=None, json=None):
        if path.startswith("products/") and path.split("/")[1].isdigit():
            p = self.products[int(path.split("/")[1])]
            if (params or {}).get("context") == "view":
                return {**p, "regular_price": str(round(float(p["regular_price"]) * 273000))}
            return p
        if path == "products" and "brand" in (params or {}):
            return self.products_of_brand(params["brand"]) if params.get("page", 1) == 1 else []
        if path == "products" and "include" in (params or {}):
            return [self.products[int(i)] for i in params["include"].split(",")]
        if path == "products":
            t = PL.model_key(params["search"])
            return [p for p in self.products.values() if t and t in PL.model_key(p["name"] + p.get("sku", ""))]
        raise AssertionError(path)

    def list_brands(self):
        return self.brands

    def products_of_brand(self, bid):
        return [p for p in self.products.values() if bid in [b["id"] for b in p.get("brands") or []]]

    def batch_update(self, items):
        self.updates += items
        return {"update": [{"id": x["id"]} for x in items]}

    def create_brand(self, name):
        self.brands.append({"id": 99, "name": name})
        return {"id": 99}


def _p(pid, name, usd, brand=(5, "ویوارکس"), status="publish"):
    return {"id": pid, "name": name, "sku": f"CRV-{pid}", "regular_price": f"{usd:.2f}", "type": "simple", "status": status,
            "brands": [{"id": brand[0], "name": brand[1]}] if brand else []}


class MatchAndApply(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cfg = {"_data_dir": self.d}
        run = PL.run_path(self.cfg, "pl-test")
        PL.write_csv(pd.DataFrame([
            {"brand": "ویوارکس", "model": "VR8016-AS", "key": "VR8016AS", "name": "منگنه کوب بادی", "price": "", "unit": "rial",
             "toman": "4791000", "list_date": "آبان ۱۴۰۵", "file": "a", "note": ""},
            {"brand": "ویوارکس", "model": "VR2402-BCK", "key": "VR2402BCK", "name": "کیت شارژی براشلس", "price": "", "unit": "rial",
             "toman": "14375000", "list_date": "", "file": "a", "note": ""},
            {"brand": "ویوارکس", "model": "VR2010-BLX", "key": "VR2010BLX", "name": "دریل شارژی", "price": "", "unit": "rial",
             "toman": "8898000", "list_date": "", "file": "a", "note": ""}]), run / "items.csv", PL.ITEM_COLS)
        self.wc = FakeWC([
            _p(1, "منگنه کوب بادی ویوارکس مدل VR8016-AS", 876000 / 273000),
            _p(2, "منگنه کوب بادی ویوارکس VR8016-AS", 5522000 / 273000),
            _p(3, "باتری دریل VR2010-BLX ویوارکس", 1000000 / 273000),                 # a part: not matched
            _p(4, "دریل شارژی ویوارکس مدل VR2010-BLX", 8898000 / 273000, brand=(7, "ال‌ای (LE-)")),
            _p(5, "اره زنجیری ویوارکس VR2106P-CS", 1894000 / 273000),
            _p(6, "اره زنجیری ویوارکس مدل VR2106P-CS", 21233000 / 273000),
            _p(7, "مینی فرز ویوارکس VR9525-AG", 2667000 / 273000)],
            [{"id": 5, "name": "ویوارکس"}, {"id": 7, "name": "ال‌ای (LE-)"}])

    def test_plan(self):
        with mock.patch("crawler.catalog_publish._brand", side_effect=lambda cfg, raw: {"status": "ok", "display": raw, "fa": raw, "en": "Vivarex"}):
            s = PL.match(self.cfg, "pl-test", wc=self.wc)
        self.assertAlmostEqual(s["rate"], 273000, delta=50)
        pl = PL.plan(self.cfg, "pl-test")
        pl = pl[pl["product_id"].astype(str) != ""].assign(product_id=lambda x: x["product_id"].astype(float).astype(int)).set_index("product_id")
        self.assertEqual(pl.loc[1, "action"], "update")
        self.assertEqual(pl.loc[1, "big"], "yes")                          # 876k -> 4.79M
        self.assertEqual(pl.loc[2, "action"], "update")
        self.assertNotIn(3, pl.index[pl["action"] != "other"])           # the battery is not the drill
        self.assertEqual(pl.loc[4, "action"], "brand")                     # same price, wrong brand
        self.assertEqual(pl.loc[5, "action"], "spread")
        self.assertEqual(pl.loc[7, "action"], "other")
        new = PL.plan(self.cfg, "pl-test")
        self.assertEqual(list(new.loc[new["action"] == "new", "model"]), ["VR2402-BCK"])

        with mock.patch("crawler.catalog_publish._brand", side_effect=lambda cfg, raw: {"status": "ok", "display": raw, "fa": raw, "en": "Vivarex"}):
            res = PL.apply(self.cfg, "pl-test", wc=self.wc)
        ups = {x["id"]: x for x in self.wc.updates}
        self.assertNotIn(1, ups)                                           # big change held
        self.assertEqual(ups[2]["regular_price"], f"{4791000 / s['rate']:.2f}")
        self.assertTrue(any(m["key"] == "_caravan_price_lock" for m in ups[2]["meta_data"]))
        self.assertEqual(ups[4]["brands"], [{"id": 5}])
        self.assertEqual(ups[5]["status"], "pending")
        self.assertEqual(res["held_big"], 1)
        self.assertIn("VR8016AS", json.loads((self.d / "pricelists" / "index.json").read_text()))
        self.assertTrue((self.d / "pricelists" / "pl-test" / "backup.jsonl").exists())

        self.wc.updates = []
        with mock.patch("crawler.catalog_publish._brand", side_effect=lambda cfg, raw: {"status": "ok", "display": raw, "fa": raw, "en": "Vivarex"}):
            PL.apply(self.cfg, "pl-test", wc=self.wc, include_big=True, spread=False)
        self.assertIn(1, {x["id"] for x in self.wc.updates})


if __name__ == "__main__":
    unittest.main()
