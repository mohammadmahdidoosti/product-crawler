"""Dollar prices are made with the rate of the day they are sent, not the crawl day's."""
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from crawler import fx
from crawler.pipeline import _usd_price


class SendRate(unittest.TestCase):
    def test_toman_price_uses_send_rate(self):
        r = {"price_toman": "2700000", "price_usd": "10", "usd_rate_toman": "270000"}
        self.assertEqual(_usd_price(r, 200000), "13.5")
        self.assertEqual(_usd_price(r), "10")                     # no send rate: as crawled

    def test_foreign_price_kept(self):
        r = {"price_toman": "5400000", "price_usd": "20", "source_type": "foreign", "price_unit_detected": "usd"}
        self.assertEqual(_usd_price(r, 200000), "20")

    def test_manual_rate_change_counts_same_day(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = {"_data_dir": Path(t), "fx": {"provider": "manual", "manual_rate_toman": 270000}}
            self.assertEqual(fx.today_rate(cfg)["usd_rate_toman"], 270000)
            cfg["fx"]["manual_rate_toman"] = 200000
            self.assertEqual(fx.today_rate(cfg)["usd_rate_toman"], 200000)
            self.assertEqual(fx.today_rate(cfg)["usd_rate_toman"], 200000)


if __name__ == "__main__":
    unittest.main()
