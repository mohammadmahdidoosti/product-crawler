"""Prices set from a supplier's price list are never overwritten by the crawler (3 Nov)."""
import unittest
from unittest import mock

from crawler.wc import PRICE_LOCK_KEY, price_locked


class PriceLocked(unittest.TestCase):
    def test_marks(self):
        self.assertTrue(price_locked({"meta_data": [{"key": PRICE_LOCK_KEY, "value": "1"}]}))
        self.assertTrue(price_locked({"meta_data": [{"key": PRICE_LOCK_KEY, "value": "vivarex 1405-08"}]}))
        self.assertFalse(price_locked({"meta_data": [{"key": PRICE_LOCK_KEY, "value": "0"}]}))
        self.assertFalse(price_locked({"meta_data": [{"key": "other", "value": "1"}]}))
        self.assertFalse(price_locked({}))
        self.assertFalse(price_locked(None))


class TorobPendingKeepsLocked(unittest.TestCase):
    def test_locked_price_not_written(self):
        from crawler import torobcheck as T
        wc = mock.Mock()
        wc.ok.return_value = True
        wc.get_product.return_value = {"meta_data": [{"key": PRICE_LOCK_KEY, "value": "1"}]}
        with mock.patch("crawler.fx.today_rate", return_value={"usd_rate_toman": 273000}):
            out = T.apply_pending({}, "12", {"ratio": "10.0", "median_toman": "5000000"}, 0.1, wc=wc)
        self.assertIn("قفل", out)
        wc.update_product.assert_not_called()

    def test_matching_price_still_published(self):
        from crawler import torobcheck as T
        wc = mock.Mock()
        wc.ok.return_value = True
        T.apply_pending({}, "12", {"ratio": "1.0", "median_toman": "5000000"}, 0.1, wc=wc)
        wc.update_product.assert_called_once_with(12, {"status": "publish"})


if __name__ == "__main__":
    unittest.main()
