"""3 Nov: the supplier lookup takes the site's name (with 'برند …'), an edit link, or asks the site."""
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from crawler import lookup as L


class Lookup(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        r = d / "runs" / "batch-17"
        r.mkdir(parents=True)
        pd.DataFrame([{"url": "https://x.ir/p/1", "name": "چاله کن دستی بدون موتور", "source_domain": "x.ir",
                       "seller_name": "فروشگاه ایکس"}]).to_csv(r / "05_new_products.csv", index=False)
        pd.DataFrame([{"source_url": "https://x.ir/p/1", "product_id": 151196, "sku": "CRV-1",
                       "name": "چاله کن دستی بدون موتور برند لاکی گلد (Lucky Gold)"}]).to_csv(r / "07_published.csv", index=False)
        self.cfg = {"_data_dir": d}

    def test_site_name(self):
        self.assertEqual(L.find(self.cfg, "چاله کن دستی بدون موتور برند لاکی گلد (Lucky Gold)")[0]["supplier"], "فروشگاه ایکس")

    def test_edit_link(self):
        r = L.find(self.cfg, "https://caravansanat.com/wp-admin/post.php?post=151196&action=edit")
        self.assertEqual(r[0]["domain"], "x.ir")

    def test_from_site(self):
        class W:
            def ok(self):
                return True

            def get_product(self, i):
                return {"name": "موتور", "sku": "", "meta_data": [{"key": "_caravan_source_url", "value": "https://y.cn/a"},
                                                                {"key": "_caravan_supplier", "value": "Antu"}]}
        r = L.find(self.cfg, "98223", wc=W())
        self.assertEqual((r[0]["supplier"], r[0]["url"], r[0]["product_id"]), ("Antu", "https://y.cn/a", "98223"))


if __name__ == "__main__":
    unittest.main()
