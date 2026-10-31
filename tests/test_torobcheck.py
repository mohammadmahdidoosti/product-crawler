"""Torob price check: queue, matching and verdicts (no network)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd


class TorobCheck(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.cfg = {"_data_dir": Path(self.t.name)}
        self.p = pd.DataFrame({"id": ["1", "2"], "name": ["دریل بتن کن ماکیتا HR2470", "فرز آهنگری بوش GWS 2000"],
                               "our_toman": [20_000_000.0, 9_000_000.0]})

    def tearDown(self):
        self.t.cleanup()

    def test_flow(self):
        from crawler import torobcheck as TC
        with mock.patch.object(TC, "products", return_value=self.p):
            j = TC.next_item(self.cfg)
            self.assertEqual(j["id"], "1")                       # the dearer first
            self.assertEqual(TC.next_item(self.cfg)["id"], "2")  # 1 is handed out
            r = TC.result(self.cfg, {"id": "1", "items": [{"name1": "اره عمودبر", "random_key": "a"},
                                                          {"name1": "دریل بتن کن ماکیتا مدل HR2470", "random_key": "b"}]})
            self.assertEqual(r["details"], "b")
            r = TC.result(self.cfg, {"id": "1", "prk": "b", "offers": [{"price": 15_000_000}, {"price": 16_000_000},
                                                                      {"price": 17_000_000}]})
            self.assertEqual(r["verdict"], "high")               # 20m against a 16m median
            r = TC.result(self.cfg, {"id": "2", "items": [{"name1": "پیچ گوشتی", "random_key": "z"}]})
            self.assertEqual(r["verdict"], "no_match")
            s = TC.summary(self.cfg)
            self.assertEqual(s["checked"], 2)
            self.assertIn("wait", TC.next_item(self.cfg))         # both checked: nothing left this month

    def test_token(self):
        from crawler import torobcheck as TC
        with mock.patch.dict(os.environ, {"APP_PASSWORD": "pw"}):
            self.assertTrue(TC.token_ok(TC.token("pw")))
            self.assertFalse(TC.token_ok("x"))


if __name__ == "__main__":
    unittest.main()


class PendingVerdict(unittest.TestCase):
    def test_apply_pending(self):
        from crawler import torobcheck as TC

        class W:
            def __init__(self):
                self.calls = []

            def ok(self):
                return True

            def update_product(self, pid, data):
                self.calls.append((pid, data))
                return {}
        cfg = {"_data_dir": Path(tempfile.mkdtemp())}
        with mock.patch("crawler.fx.today_rate", return_value={"usd_rate_toman": 270000}):
            w = W()
            self.assertIn("منتشر شد", TC.apply_pending(cfg, "7", {"ratio": "1.05", "median_toman": "10000000"}, 0.15, w))
            self.assertEqual(w.calls[-1], (7, {"status": "publish"}))
            self.assertIn("اصلاح", TC.apply_pending(cfg, "8", {"ratio": "0.1", "median_toman": "27000000"}, 0.15, w))
            self.assertEqual(w.calls[-1], (8, {"status": "publish", "regular_price": "100.00"}))
            self.assertIn("در انتظار", TC.apply_pending(cfg, "9", {"ratio": "2.5", "median_toman": "1000000"}, 0.15, w))
            self.assertEqual(len(w.calls), 2)
