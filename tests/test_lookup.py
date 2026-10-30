import tempfile, unittest
from pathlib import Path
import pandas as pd


class Lookup(unittest.TestCase):
    def test_by_name_and_id(self):
        from crawler.lookup import find
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "runs" / "batch-5"; d.mkdir(parents=True)
            pd.DataFrame([{"url": "https://siminabzar.com/p/1081", "name_fa": "رول یدک غلطک ۲۵ سانت دکور کد 1081",
                           "seller_name": "سیمین ابزار", "source_domain": "siminabzar.com", "price_toman": "400000",
                           "crawled_at": "2026-10-28"}]).to_csv(d / "05_new_products.csv", index=False)
            pd.DataFrame([{"source_url": "https://siminabzar.com/p/1081", "product_id": "111096", "sku": "CRV-0992E23E34"}]
                         ).to_csv(d / "07_published.csv", index=False)
            cfg = {"_data_dir": Path(t)}
            r = find(cfg, "غلطک 1081")
            self.assertEqual(r[0]["supplier"], "سیمین ابزار")
            self.assertEqual(find(cfg, "111096")[0]["url"], "https://siminabzar.com/p/1081")
            self.assertEqual(find(cfg, "CRV-0992E23E34")[0]["product_id"], "111096")
            self.assertEqual(find(cfg, "دریل"), [])


if __name__ == "__main__":
    unittest.main()
