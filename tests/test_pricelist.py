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

    def test_digit_codes_need_the_kind(self):
        self.assertTrue(PL.weak_key(PL.model_key("90-315")))
        self.assertFalse(PL.kind_in("زانو 90 315-16 بار پلی اتیلن", "دستگاه جوش پلی اتیلن دو فک"))
        self.assertTrue(PL.kind_in("دستگاه جوش پلی اتیلن 90-315 دو فک", "دستگاه جوش پلی اتیلن دو فک"))
        idx = {"90315": {"toman": 1, "name": "دستگاه جوش پلی اتیلن", "brand_names": []}}
        self.assertIsNone(PL.list_price({}, "زانو 90 315-16 بار", idx=idx))

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
        """3 Nov rules: one copy of a listed model stays (the oldest published), the other goes (301); the
        brand's products not in the list go out of stock; the brand is locked for the crawler."""
        brand = lambda cfg, raw: {"status": "ok", "display": raw, "fa": raw, "en": "Vivarex"}  # noqa: E731
        with mock.patch("crawler.catalog_publish._brand", side_effect=brand):
            s = PL.match(self.cfg, "pl-test", wc=self.wc)
        self.assertAlmostEqual(s["rate"], 273000, delta=50)
        pl = PL.plan(self.cfg, "pl-test")
        pl = pl[pl["product_id"].astype(str) != ""].assign(product_id=lambda x: x["product_id"].astype(float).astype(int)).set_index("product_id")
        self.assertEqual(pl.loc[1, "action"], "update")
        self.assertEqual(pl.loc[1, "big"], "yes")                          # 876k -> 4.79M
        self.assertEqual(pl.loc[2, "action"], "dup")                       # a copy of #1
        self.assertEqual(int(float(pl.loc[2, "keep_id"])), 1)
        self.assertNotIn(3, pl.index[~pl["action"].isin(["other", "spread"])])  # the battery is not the drill
        self.assertEqual(pl.loc[4, "action"], "brand")                     # same price, wrong brand
        self.assertEqual(pl.loc[5, "action"], "spread")
        self.assertEqual(pl.loc[7, "action"], "other")
        self.assertEqual(s["out_of_stock"], 4)                              # 3, 5, 6, 7
        new = PL.plan(self.cfg, "pl-test")
        self.assertEqual(list(new.loc[new["action"] == "new", "model"]), ["VR2402-BCK"])

        moved = []
        with mock.patch("crawler.catalog_publish._brand", side_effect=brand), \
                mock.patch("crawler.merge_site.redirect_copy", side_effect=lambda cfg, wc, k, dd, run="": moved.append((k, dd)) or "redirect"):
            res = PL.apply(self.cfg, "pl-test", wc=self.wc)
        ups = {x["id"]: x for x in self.wc.updates}
        self.assertNotIn(1, ups)                                           # big change held
        self.assertEqual(moved, [(1, 2)])
        self.assertTrue(any(m["key"] == "_caravan_price_lock" for m in ups[4]["meta_data"]))
        self.assertEqual(ups[4]["brands"], [{"id": 5}])
        for pid in (3, 5, 6, 7):
            self.assertEqual(ups[pid]["stock_status"], "outofstock")
        self.assertEqual((res["out_of_stock"], res["redirected"], res["held_big"]), (4, 1, 1))
        self.assertEqual(res["brand_locked"], ["ویوارکس"])
        locks = PL.brand_locks(self.cfg)
        self.assertEqual(PL.locked_brand_of(locks, "دریل شارژی ویوارکس VR9999"), "ویوارکس")
        self.assertEqual(PL.locked_brand_of(locks, "دریل VR9999", "Vivarex"), "ویوارکس")
        self.assertEqual(PL.locked_brand_of(locks, "دریل رونیکس 2700"), "")
        self.assertIn("VR8016AS", json.loads((self.d / "pricelists" / "index.json").read_text()))
        self.assertTrue((self.d / "pricelists" / "pl-test" / "backup.jsonl").exists())
        self.assertTrue(PL.unlock_brand(self.cfg, "ویوارکس"))
        self.assertEqual(PL.brand_locks(self.cfg), {})

        self.wc.updates = []                                               # not the brand's whole list
        with mock.patch("crawler.catalog_publish._brand", side_effect=brand), \
                mock.patch("crawler.merge_site.redirect_copy", return_value="redirect"):
            res = PL.apply(self.cfg, "pl-test", wc=self.wc, include_big=True, complete=False)
        self.assertIn(1, {x["id"] for x in self.wc.updates})
        self.assertFalse(any(x.get("stock_status") == "outofstock" for x in self.wc.updates))
        self.assertEqual(res["brand_locked"], [])


if __name__ == "__main__":
    unittest.main()


class RowsWithoutCode(unittest.TestCase):
    """3 Nov, the P.S.C list: 53 rows, none with a model code; sizes are not codes."""

    def test_rows_kept_by_name(self):
        rows = [{"brand": "P.S.C", "model": "", "name": "دستگاه جوش پلی اتیلن 90-315 دو فک پرتابل اهرم دستی",
                 "price": "1,270,000,000", "unit": "rial", "list_date": "1405/08/01", "file": "a"},
                {"brand": "P.S.C", "model": "", "name": "دستگاه جوش پلی اتیلن نیمه هیدرولیک فشارقوی 315 - 90",
                 "price": "2,430,000,000", "unit": "rial", "list_date": "1405/08/01", "file": "a"},
                {"brand": "P.S.C", "model": "", "name": "کالیبر (لقمه جوش) سایز 20 1/2 اینچ",
                 "price": "4,600,000", "unit": "rial", "list_date": "1405/08/01", "file": "b"}]
        df = PL.merge_rows(rows)
        self.assertEqual(len(df), 3)
        self.assertTrue(all(PL.is_name_key(k) for k in df["key"]))
        self.assertEqual(sorted(df["toman"].astype(int)), [460000, 127000000, 243000000])

    def test_same_by_name(self):
        row = "دستگاه جوش پلی اتیلن 90-315 دو فک پرتابل اهرم دستی"
        self.assertTrue(PL.same_by_name("دستگاه جوش پلی اتیلن 90-315 دو فک پرتابل اهرم دستی برند پی اس سی", row))
        self.assertFalse(PL.same_by_name("دستگاه جوش پلی اتیلن تمام هیدرولیک 90-315", row))   # another variant
        self.assertFalse(PL.same_by_name("زانو 90 315-16 بار پلی اتیلن", row))
        self.assertFalse(PL.same_by_name("دستگاه جوش پلی اتیلن 63-200 دو فک پرتابل اهرم دستی", row))  # other size


class CreateFromList(unittest.TestCase):
    def test_new_rows_made_pending_and_locked(self):
        d = Path(tempfile.mkdtemp())
        cfg = {"_data_dir": d}
        run = PL.run_path(cfg, "pl-psc")
        PL.write_csv(pd.DataFrame([{"action": "new", "key": "NAAAAAAAAAA", "model": "", "brand": "P.S.C",
                                    "list_name": "دستگاه جوش پلی اتیلن 90-315 دو فک", "list_toman": 127000000},
                                   {"action": "new", "key": "NBBBBBBBBBB", "model": "", "brand": "P.S.C",
                                    "list_name": "چیزی ناشناخته", "list_toman": 1000000}]), run / "plan.csv", PL.PLAN_COLS)
        (run / "match.json").write_text(json.dumps({"rate": 273000}))

        class Idx:
            def __init__(self, *a):
                pass

            def match(self, name, n=3):
                return [{"taxonomy_id": "77", "leaf": "دستگاه جوش پلی اتیلن", "breadcrumb": "جوش > دستگاه جوش پلی اتیلن"}] \
                    if "جوش" in name else []
        made = []
        wc = mock.Mock()
        wc.ok.return_value = True
        wc.find_by_sku.return_value = None
        wc.list_brands.return_value = []
        wc.create_brand.return_value = {"id": 5}
        wc.create_product.side_effect = lambda data: made.append(data) or {"id": 900}
        with mock.patch("crawler.searches.TaxIndex", Idx), mock.patch("crawler.searches.load_tax", return_value=None), \
                mock.patch("crawler.wc.category_map", return_value={"77": "1234"}), \
                mock.patch("crawler.describe.Writer.ok", return_value=False), \
                mock.patch("crawler.catalog_publish._brand", return_value={"status": "ok", "display": "پی اس سی (P.S.C)", "fa": "پی اس سی", "en": "P.S.C"}):
            res = PL.create_new(cfg, "pl-psc", wc=wc)
        self.assertEqual((res["made"], res["no_category"]), (1, 1))
        p = made[0]
        self.assertEqual(p["status"], "pending")
        self.assertEqual(p["categories"], [{"id": 1234}])
        self.assertEqual(p["regular_price"], f"{127000000 / 273000:.4f}")
        self.assertTrue(any(m["key"] == "_caravan_price_lock" for m in p["meta_data"]))
        self.assertIn("پی اس سی", p["name"])


class SupplierSite(unittest.TestCase):
    """3 Nov: the supplier's own site (pooya-sanat.com) gives the new rows their photos and text."""

    SITE = [{"name": "صفحه گرمکن پلی اتیلن گازی 315", "url": "https://s/g315", "images": "https://s/a.jpg", "image_count": 1},
            {"name": "صفحه گرمکن پلی اتیلن طرح رتنبرگ سایز 315", "url": "https://s/r315", "images": "https://s/b.jpg", "image_count": 1},
            {"name": "دستگاه جوش پلی اتیلن دستی ۳۱۵-۹۰ میلی‌متر دو فک", "url": "https://s/m90", "images": "", "image_count": 0},
            {"name": "دستگاه لوله بازکن", "url": "https://s/x", "images": "", "image_count": 0}]

    def setUp(self):
        self.cfg = {"_data_dir": Path(tempfile.mkdtemp())}
        run = PL.run_path(self.cfg, "pl-psc")
        PL.write_csv(pd.DataFrame([
            {"action": "new", "key": "N1", "list_name": "هیتر گازی پلی اتیلن 315", "list_toman": 27000000},
            {"action": "new", "key": "N2", "list_name": "دستگاه جوش پلی اتیلن 90-315 دو فک پرتابل اهرم دستی", "list_toman": 127000000},
            {"action": "new", "key": "N3", "list_name": "غلطک زیر سری", "list_toman": 8000000}]), run / "plan.csv", PL.PLAN_COLS)

    def test_site_address(self):
        self.assertEqual(PL.norm_site("pooya-sanat.com"), "https://pooya-sanat.com/")
        self.assertEqual(PL.norm_site("http://Pooya-Sanat.com/shop/"), "https://pooya-sanat.com/")
        self.assertEqual(PL.norm_site(""), "")
        with self.assertRaises(ValueError):
            PL.norm_site("not a site")
        self.assertEqual(PL.set_site(self.cfg, "pl-psc", "pooya-sanat.com"), "https://pooya-sanat.com/")
        self.assertEqual(PL.get_site(self.cfg, "pl-psc"), "https://pooya-sanat.com/")

    def test_candidates_share_the_size(self):
        c = PL._cands("هیتر گازی پلی اتیلن 315", self.SITE)
        self.assertEqual(set(c[:3]), {0, 1, 2})
        self.assertNotIn(3, c)

    def test_match_by_the_model(self):
        PL.set_site(self.cfg, "pl-psc", "pooya-sanat.com")
        asked = []

        def answer(cfg, messages, **kw):
            asked.append(messages[1]["content"])
            return {"picks": [{"i": 0, "j": 0}, {"i": 1, "j": 2}, {"i": 2, "j": 3}]}, 0.0001
        with mock.patch.object(PL, "site_products", return_value=self.SITE), mock.patch.object(PL, "_openrouter", side_effect=answer):
            res = PL.site_match(self.cfg, "pl-psc")
        self.assertEqual((res["rows"], res["found"], res["with_photos"]), (3, 2, 1))
        m = {r["key"]: r for r in PL.read_csv(PL.run_path(self.cfg, "pl-psc") / "site_match.csv").fillna("").to_dict("records")}
        self.assertEqual(m["N1"]["site_url"], "https://s/g315")
        self.assertEqual(m["N2"]["site_url"], "https://s/m90")
        self.assertEqual(m["N3"]["site_url"], "")           # 'لوله بازکن' was never a candidate: the model can't pick it
        PL.set_site(self.cfg, "pl-psc", "another.com")      # another site: the old matches go
        self.assertFalse((PL.run_path(self.cfg, "pl-psc") / "site_match.csv").exists())


class OurOldCopiesByName(unittest.TestCase):
    """3 Nov: the shop's own P.S.C products are named in other words ('صفحه گرمکن' for 'هیتر', the range reversed)."""

    def test_found_and_chosen(self):
        from crawler.normalize import norm_text
        prods = [_p(10, "صفحه گرمکن پلی اتیلن گازی 315", 25000000 / 273000, brand=None),
                 _p(11, "صفحه گرمکن پلی اتیلن گازی 250", 23000000 / 273000, brand=None),
                 _p(12, "دستگاه جوش پلی اتیلن دستی ۳۱۵-۹۰ میلی‌متر دو فک", 120000000 / 273000, brand=None),
                 _p(13, "دستگاه جوش پلی اتیلن تمام هیدرولیک مدل TSD315 برند تی‌اس‌دی", 790000000 / 273000, brand=None),
                 _p(14, "صفحه گرمکن پلی اتیلن گازی 315 برند دیگر", 20000000 / 273000, brand=(9, "دیگر"))]

        class W(FakeWC):
            def _wc(self, method, path, params=None, json=None):
                if path == "products" and "search" in (params or {}):
                    ws = norm_text(params["search"]).split()
                    return [p for p in self.products.values()
                            if all(w in norm_text(p["name"].translate(PL.FA_DIGITS)).split() for w in ws)]
                return super()._wc(method, path, params, json)
        wc = W(prods, [])
        c = PL._name_cands(wc, "هیتر گازی پلی اتیلن 315")
        self.assertEqual([p["id"] for p in c], [10, 14])              # 250 is another size (14: another maker, left out in match)
        c = PL._name_cands(wc, "دستگاه جوش پلی اتیلن 90-315 دو فک پرتابل اهرم دستی")
        self.assertEqual({p["id"] for p in c}, {12, 13} & {p["id"] for p in c})
        self.assertIn(12, [p["id"] for p in c])

        cfg = {"_data_dir": Path(tempfile.mkdtemp())}
        run = PL.run_path(cfg, "pl-psc")
        PL.write_csv(pd.DataFrame([
            {"brand": "P.S.C", "model": "", "key": PL.name_key("هیتر گازی پلی اتیلن 315"), "name": "هیتر گازی پلی اتیلن 315",
             "price": "", "unit": "rial", "toman": "27000000", "list_date": "", "file": "a", "note": ""},
            {"brand": "P.S.C", "model": "", "key": PL.name_key("دستگاه جوش پلی اتیلن 90-315 دو فک پرتابل اهرم دستی"),
             "name": "دستگاه جوش پلی اتیلن 90-315 دو فک پرتابل اهرم دستی", "price": "", "unit": "rial",
             "toman": "127000000", "list_date": "", "file": "a", "note": ""}]), run / "items.csv", PL.ITEM_COLS)

        def pick(cfg, messages, **kw):
            u = messages[1]["content"]
            same = [j for j, line in enumerate(u.splitlines()[2:]) if "TSD" not in line and "تمام" not in line]
            return {"same": same}, 0.0001
        with mock.patch.object(PL, "_openrouter", side_effect=pick), \
                mock.patch.object(PL, "_brand_terms", return_value={}), mock.patch.object(PL, "site_rate", return_value=273000.0):
            s = PL.match(cfg, "pl-psc", wc=wc)
        pl = PL.plan(cfg, "pl-psc")
        got = {int(float(x)) for x in pl.loc[pl["action"].isin(["update", "same"]), "product_id"]}
        self.assertEqual(got, {10, 12})
        self.assertGreater(s["usd"], 0)


class PublishWithPhoto(unittest.TestCase):
    """3 Nov: a new product with a photo (the supplier's or a similar one) goes on the shop at once."""

    def test_similar_photo_publishes(self):
        d = Path(tempfile.mkdtemp())
        cfg = {"_data_dir": d}
        run = PL.run_path(cfg, "pl-psc")
        PL.write_csv(pd.DataFrame([{"action": "new", "key": "NAAAAAAAAAA", "model": "", "brand": "P.S.C",
                                    "list_name": "آچار پلی اتیلن ثابت 75-63-50", "list_toman": 1500000}]), run / "plan.csv", PL.PLAN_COLS)
        (run / "match.json").write_text(json.dumps({"rate": 273000}))
        pic = d / "a.webp"
        pic.write_bytes(b"x")

        class Idx:
            def __init__(self, *a):
                pass

            def match(self, name, n=3):
                return [{"taxonomy_id": "7", "leaf": "آچار", "breadcrumb": "ابزار > آچار"}]
        made = []
        wc = mock.Mock()
        wc.ok.return_value = True
        wc.find_by_sku.return_value = None
        wc.can_upload.return_value = True
        wc.upload_media.return_value = {"id": 55}
        wc.create_product.side_effect = lambda data: made.append(data) or {"id": 901}
        with mock.patch("crawler.searches.TaxIndex", Idx), mock.patch("crawler.searches.load_tax", return_value=None), \
                mock.patch("crawler.wc.category_map", return_value={"7": "12"}), \
                mock.patch("crawler.describe.Writer.ok", return_value=False), \
                mock.patch("crawler.catalog_publish._brand", return_value={"status": "none", "display": ""}), \
                mock.patch.object(PL, "_similar_photos", return_value=[pic]) as sim:
            res = PL.create_new(cfg, "pl-psc", wc=wc)
        self.assertEqual(sim.call_args[0][1], "آچار پلی اتیلن ثابت 75-63-50")
        self.assertEqual(made[0]["status"], "publish")
        self.assertEqual(made[0]["images"], [{"id": 55}])
        self.assertEqual((res["published"], res["similar_photos"]), (1, 1))


class DigitCodeOfTheBrand(unittest.TestCase):
    """3 Nov, Ronix: 'بلوور برقی 1209' in the list is 'دمنده - مکنده رونیکس مدل 1209' on the site."""

    def test_brand_product_asked(self):
        cfg = {"_data_dir": Path(tempfile.mkdtemp())}
        run = PL.run_path(cfg, "pl-rx")
        PL.write_csv(pd.DataFrame([{"brand": "رونیکس", "model": "1209", "key": "1209", "name": "بلوور برقی 600وات",
                                    "price": "", "unit": "toman", "toman": "7498000", "list_date": "", "file": "a", "note": ""}]),
                     run / "items.csv", PL.ITEM_COLS)
        wc = FakeWC([_p(1, "دمنده - مکنده رونیکس مدل 1209", 7000000 / 273000, brand=(8, "رونیکس")),
                     _p(2, "زانو 1209 پلی اتیلن", 100000 / 273000, brand=None)], [{"id": 8, "name": "رونیکس"}])
        asked = []
        with mock.patch("crawler.catalog_publish._brand", side_effect=lambda cfg, raw: {"status": "ok", "display": raw, "fa": raw, "en": "Ronix"}), \
                mock.patch.object(PL, "_openrouter", side_effect=lambda cfg, m, **k: asked.append(m[1]["content"]) or ({"same": [0]}, 0.0)):
            PL.match(cfg, "pl-rx", wc=wc)
        pl = PL.plan(cfg, "pl-rx")
        got = pl.loc[pl["action"].isin(["update", "same"]), "product_id"].astype(float).astype(int).tolist()
        self.assertEqual(got, [1])
        self.assertEqual(len(asked), 1)
        self.assertNotIn("زانو", asked[0])                    # not of the brand: never asked


class CatalogCases(unittest.TestCase):
    """4 Nov, the owner's 16 catalogs: price formats, a row's own maker, the same file twice, stock, photos."""

    def test_price_formats(self):
        self.assertEqual(PL.to_toman("۴۴۹/۹۹۹/۰۰۰", "rial"), 44_999_900)          # Arva
        self.assertEqual(PL.to_toman("359.999.000", "rial"), 35_999_900)            # Hyundai
        self.assertEqual(PL.to_toman("۲۳۹٬۹۸۰٬۰۰۰", "rial"), 23_998_000)           # Nova
        self.assertEqual(PL.to_toman("22800", "thousand_toman"), 22_800_000)        # Boss
        self.assertEqual(PL.to_toman("22.8", "thousand_toman"), 22_800)

    def test_row_maker_wins(self):
        rows = [{"brand": "MUSTANG", "model": "T6106", "name": "انبر دست ۶ اینچ", "price": "14/375/000", "unit": "rial",
                 "list_date": "آبان ۱۴۰۵", "file": "a"},
                {"brand": "توسن", "model": "2720", "name": "صفحه برش", "price": "15/395/000", "unit": "rial",
                 "list_date": "آبان ۱۴۰۵", "file": "a", "stock": "no", "vat": "yes"}]
        df = PL.merge_rows(rows, "auto", "").set_index("key")
        self.assertEqual(df.loc["T6106", "brand"], "MUSTANG")
        self.assertEqual(df.loc["2720", "stock"], "no")
        self.assertIn("ارزش افزوده", df.loc["2720", "note"])

    def test_same_file_twice(self):
        cfg = {"_data_dir": Path(tempfile.mkdtemp())}
        PL.run_path(cfg, "pl-arva")
        self.assertEqual(PL.add_file(cfg, "pl-arva", b"%PDF-1 x", "arva.pdf")["files"], 1)
        r = PL.add_file(cfg, "pl-arva", b"%PDF-1 x", "arva-jame.pdf")
        self.assertEqual((r["files"], r["same_as"]), (0, "arva.pdf"))

    def test_catalog_photo_crop(self):
        from PIL import Image
        import io as _io
        d = Path(tempfile.mkdtemp())
        buf = _io.BytesIO()
        Image.new("RGB", (1000, 1400), "white").save(buf, "JPEG")
        name = PL._crop(d, buf.getvalue(), [100, 100, 600, 500], {"model": "2720", "name": "صفحه برش"})
        self.assertEqual(name, "2720.webp")
        self.assertTrue((d / "photos" / "2720.webp").exists())
        self.assertEqual(PL._crop(d, buf.getvalue(), [0, 0, 50, 50], {"model": "1", "name": "x"}), "")

    def test_list_stock_applied(self):
        cfg = {"_data_dir": Path(tempfile.mkdtemp())}
        run = PL.run_path(cfg, "pl-hy")
        PL.write_csv(pd.DataFrame([
            {"action": "update", "key": "HP5246", "model": "HP-5246", "brand": "HYUNDAI", "list_name": "بکس", "list_toman": 35999900,
             "product_id": 1, "big": "", "stock": "instock", "list_stock": "no"},
            {"action": "update", "key": "HP5230", "model": "HP-5230", "brand": "HYUNDAI", "list_name": "اره", "list_toman": 22799900,
             "product_id": 2, "big": "", "stock": "outofstock", "list_stock": "yes"}]), run / "plan.csv", PL.PLAN_COLS + PL.EXTRA_COLS)
        (run / "match.json").write_text(json.dumps({"rate": 273000}))
        PL.write_csv(pd.DataFrame(columns=PL.ITEM_COLS), run / "items.csv", PL.ITEM_COLS)
        wc = FakeWC([_p(1, "بکس HP-5246", 1), _p(2, "اره HP-5230", 1)], [])
        with mock.patch("crawler.catalog_publish._brand", return_value={"status": "none"}):
            PL.apply(cfg, "pl-hy", wc=wc, complete=False)
        ups = {x["id"]: x for x in wc.updates}
        self.assertEqual(ups[1]["stock_status"], "outofstock")
        self.assertEqual(ups[2]["stock_status"], "instock")
        self.assertIn("regular_price", ups[1])


class MainBrandOnly(unittest.TestCase):
    """4 Nov: Tosan's catalog carries Mustang; only Tosan's other products go out of stock and only Tosan locks."""

    def test_main_brand(self):
        cfg = {"_data_dir": Path(tempfile.mkdtemp())}
        run = PL.run_path(cfg, "pl-tosan")
        rows = [{"brand": "توسن", "model": f"T{i}00", "key": f"T{i}00", "name": "x", "toman": "1000000"} for i in range(5)] + \
               [{"brand": "MUSTANG", "model": "T6106", "key": "T6106", "name": "y", "toman": "1000000"}]
        PL.write_csv(pd.DataFrame(rows), run / "items.csv", PL.ITEM_COLS)
        self.assertEqual(PL.main_brands(cfg, "pl-tosan"), {"توسن"})
        (run / "read.json").write_text(json.dumps({"brand": "MUSTANG"}))
        self.assertEqual(PL.main_brands(cfg, "pl-tosan"), {"MUSTANG"})


class ShortCodesAndOtherMakers(unittest.TestCase):
    """4 Nov, Picoran: 'سه نظام B16' is not terminal block 'B16/6'; Picoran's chisel is not another maker's."""

    def test_short_code_needs_the_kind(self):
        self.assertTrue(PL.weak_key("B16"))
        self.assertFalse(PL.weak_key("VR2402BCK"))
        self.assertFalse(PL.kind_in("ترمینال شاخه‌ای ۶ خانه سایز ۱۶ مدل B16/6", "سه نظام 13 آچاری B16"))

    def test_other_maker_never_fits(self):
        cfg = {}
        with mock.patch("crawler.catalog_publish._brand", return_value={"status": "ok", "display": "پیکوران", "fa": "پیکوران", "en": "Picoran"}):
            self.assertTrue(PL._brand_fits({"name": "قلم", "brands": []}, None, {}, "پیکوران", cfg))
            self.assertFalse(PL._brand_fits({"name": "قلم SDS", "brands": [{"id": 3, "name": "بوش"}]}, None, {}, "پیکوران", cfg))
            self.assertTrue(PL._brand_fits({"name": "قلم", "brands": [{"id": 4, "name": "پیکوران"}]}, {"id": 4}, {}, "پیکوران", cfg))


class CategoryByModel(unittest.TestCase):
    """4 Nov, Pooya: 'هیتر گازی پلی اتیلن 315' has no category with all its words; the model picks one."""

    def test_model_picks(self):
        class Idx:
            def match(self, name, n=3):
                return [{"taxonomy_id": "1", "leaf": "لوله پلی اتیلن", "breadcrumb": "لوله > لوله پلی اتیلن"},
                        {"taxonomy_id": "2", "leaf": "دستگاه جوش پلی اتیلن", "breadcrumb": "جوش > دستگاه جوش پلی اتیلن"}]
        with mock.patch.object(PL, "_openrouter", return_value=({"pick": 1}, 0.0)):
            self.assertEqual(PL._leaf_llm({}, Idx(), "هیتر گازی پلی اتیلن 315")["taxonomy_id"], "2")
        with mock.patch.object(PL, "_openrouter", return_value=({"pick": None}, 0.0)):
            self.assertIsNone(PL._leaf_llm({}, Idx(), "چیزی"))
