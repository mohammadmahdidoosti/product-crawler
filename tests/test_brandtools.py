import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crawler import brandtools as BT
from crawler import pricelist as PL

TERMS = [{"id": 1, "name": "ماکیتا (Makita)", "slug": "makita"}, {"id": 2, "name": "هیوندای (Hyundai)", "slug": "hyundai"},
         {"id": 3, "name": "پی فارم", "slug": "pfarm"}, {"id": 4, "name": "برند (Brand)", "slug": "brand"},
         {"id": 5, "name": "اشتیل (Stihl)", "slug": "stihl"}]
ROWS = [{"id": 10, "name": "دریل ماکیتا 6413", "brands": [{"id": 1}], "stock_status": "instock", "type": "simple"},
        {"id": 11, "name": "فرز ماکیتا GA9020", "brands": [], "stock_status": "instock", "type": "simple"},
        {"id": 12, "name": "تیلر ماکیتا", "brands": [{"id": 1}], "stock_status": "instock", "type": "variable"},
        {"id": 13, "name": "اره موتوری زنجیری هیوندای مدل TURBO 850 برند پی فارم", "brands": [{"id": 3}],
         "stock_status": "instock", "type": "simple"},
        {"id": 14, "name": "اره زنجیری بنزینی مرسل مدل HR6000 برند پی فارم", "brands": [{"id": 3}],
         "stock_status": "instock", "type": "simple"},
        {"id": 15, "name": "پمپ آب", "brands": [{"id": 2}], "stock_status": "instock", "type": "simple"}]


class FakeWC:
    def __init__(self):
        self.sent = []

    def list_brands(self):
        return TERMS

    def _call(self, f, what):
        return []

    def _wc(self, *a, **k):
        return []

    def batch_update(self, items):
        self.sent += items
        return {"update": [{"id": x["id"]} for x in items]}


class BrandTools(unittest.TestCase):
    def setUp(self):
        self.cfg = {"_data_dir": Path(tempfile.mkdtemp())}
        self.p = mock.patch.object(PL, "site_cache", return_value=ROWS)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_find_terms(self):
        self.assertEqual([t["id"] for t in BT.find_terms(TERMS, ["ماکیتا"])], [1])
        self.assertEqual([t["id"] for t in BT.find_terms(TERMS, ["makita"])], [1])

    def test_out_of_stock_dry_and_go(self):
        wc = FakeWC()
        dry = BT.out_of_stock(self.cfg, wc, ["ماکیتا"])
        self.assertEqual(dry["brands"][0]["products"], 3)          # by term, by name without a brand, the variable one
        self.assertEqual(dry["brands"][0]["to_change"], 2)
        self.assertEqual(wc.sent, [])
        BT.out_of_stock(self.cfg, wc, ["ماکیتا"], go=True)
        self.assertEqual(sorted(x["id"] for x in wc.sent), [10, 11])

    def test_name_filter(self):
        r = BT.out_of_stock(self.cfg, FakeWC(), ["ماکیتا"], name_filter="فرز")
        self.assertEqual(r["brands"][0]["products"], 1)

    def test_shop_not_brand(self):
        wc = FakeWC()
        with mock.patch("crawler.brands_fa.load") as ld, mock.patch("crawler.brands_fa.save"):
            import pandas as pd
            ld.return_value = pd.DataFrame(columns=["source", "kind"])
            r = BT.shop_not_brand(self.cfg, wc, ["پی فارم"], go=True)
        self.assertEqual(r["products"], 2)
        by = {x["id"]: x for x in wc.sent}
        self.assertEqual(by[13]["brands"], [{"id": 2}])            # Hyundai, from its name
        self.assertEqual(by[13]["name"], "اره موتوری زنجیری هیوندای مدل TURBO 850")
        self.assertEqual(by[14]["brands"], [])                      # 'برند' is not a brand


if __name__ == "__main__":
    unittest.main()
