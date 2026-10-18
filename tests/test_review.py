import tempfile
import unittest
from pathlib import Path

import pandas as pd


class Review(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cfg = {"_data_dir": self.root, "images": {"public_base": "https://x"}}
        d = self.root / "runs" / "r1"
        d.mkdir(parents=True)
        pd.DataFrame([{"url": "u1", "name": "اره", "breadcrumb": "a > اره", "price_toman": "100", "price_estimated": "",
                       "source_domain": "s"},
                      {"url": "u2", "name": "موتور", "breadcrumb": "a > موتور", "price_toman": "200",
                       "price_estimated": "3 similar"}]).to_csv(d / "05_new_products.csv", index=False)
        pd.DataFrame([{"url": "u1", "key": "k1", "files": "k1/1.webp | k1/2.webp", "source_images": "", "notes": "text_overlay"},
                      {"url": "u2", "key": "k2", "files": "k2/1.webp", "source_images": "", "notes": ""}]
                     ).to_csv(d / "05_images.csv", index=False)
        self.d = d

    def test_lists_and_applies_decisions(self):
        from unittest import mock

        from crawler import review as RV
        with mock.patch("crawler.review.run_dir", lambda cfg, run: self.d):
            r = RV.items(self.cfg, "r1", "issues")
            self.assertEqual({x["url"] for x in r["items"]}, {"u1", "u2"})
            self.assertEqual(r["counts"]["images"], 1)
            self.assertEqual(r["counts"]["price"], 1)
            RV.save_decision(self.cfg, "r1", {"url": "u1", "action": "ok", "images": [2], "price": "150"})
            RV.save_decision(self.cfg, "r1", {"url": "u2", "action": "drop"})
            rows, imgs = RV.apply(self.d, [{"url": "u1", "price_toman": "100"}, {"url": "u2", "price_toman": "200"}],
                                  {"u1": "https://x/img/k1/1.webp, https://x/img/k1/2.webp"})
        self.assertEqual([x["url"] for x in rows], ["u1"])
        self.assertEqual(rows[0]["price_toman"], "150")
        self.assertEqual(imgs["u1"], "https://x/img/k1/2.webp")
