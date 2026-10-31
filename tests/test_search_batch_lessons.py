"""Lessons of the first failed-searches batch (search-20261029-1615)."""
import tempfile
import unittest
from pathlib import Path


class CopiedPrice(unittest.TestCase):
    def test_one_price_on_many_brands_is_held(self):
        from crawler.pipeline import price_sanity
        recs = [{"url": f"u{i}", "name": f"پمپ کارتریجی {b}", "brand": b, "price_toman": "12200000",
                 "breadcrumb": "پمپ > پمپ پره ای"} for i, b in enumerate(["Ponar", "Dexter", "Veljan", "Rexroth"])]
        recs += [{"url": "x", "name": "پمپ پره ای Yuken", "brand": "Yuken", "price_toman": "9000000",
                  "breadcrumb": "پمپ > پمپ پره ای"}]
        out = {r["url"]: r for r in price_sanity(recs)}
        for i in range(4):
            self.assertTrue(out[f"u{i}"].get("price_check", "").startswith("suspicious"))
        self.assertNotIn("کپی", out["x"].get("price_check", ""))

    def test_same_price_same_brand_is_fine(self):
        from crawler.pipeline import price_sanity
        recs = [{"url": f"u{i}", "name": f"فیوز سیلندری ETI {s}", "brand": "ETI", "price_toman": "300000",
                 "breadcrumb": "فیوز > فیوز سیلندری"} for i, s in enumerate(["10A", "16A", "20A", "25A"])]
        for r in price_sanity(recs):
            self.assertNotIn("کپی", r.get("price_check", ""))


    def test_copied_by_one_shop_only(self):
        from crawler.pipeline import price_sanity
        recs = [{"url": f"https://a.ir/p{i}", "name": f"ترانسمیتر {b}", "brand": b, "price_toman": "64825000",
                 "breadcrumb": "ابزار دقیق > ترانسمیتر فشار"} for i, b in enumerate(["Endress", "Wika", "Yokogawa"])]
        recs += [{"url": "https://b.ir/wika", "name": "ترانسمیتر Wika", "brand": "Wika", "price_toman": "64825000",
                  "breadcrumb": "ابزار دقیق > ترانسمیتر فشار"}]
        out = {r["url"]: r for r in price_sanity(recs)}
        self.assertIn("کپی", out["https://a.ir/p0"].get("price_check", ""))
        self.assertNotIn("کپی", out["https://b.ir/wika"].get("price_check", ""))


class CarParts(unittest.TestCase):
    def test_car_gas_spring(self):
        from crawler.pipeline import car_part
        self.assertTrue(car_part("جک گازی درب صندوق سوزوکی ویتارا", "جک > جک گازی"))
        self.assertTrue(car_part("جک گازی اتاق عقب وانت 900 نیوتن نیسان", "جک > جک گازی"))
        self.assertEqual(car_part("جک سوسماری خودرو 3 تن", "جک > جک سوسماری"), "")
        self.assertEqual(car_part("جک گازی مینیاتوری 700 نیوتن", "جک > جک گازی"), "")
        self.assertEqual(car_part("کمک فنر پژو", "لوازم یدکی خودرو > کمک فنر"), "")


class ShopTail(unittest.TestCase):
    def test_from_dealer_tail(self):
        from crawler.names import clean_name
        n = clean_name("خرید چراغ سیگنال سفید EMAS مدل B0B0XB با ارزان ترین قیمت از نمایندگی فروش امس")
        self.assertEqual(n, "چراغ سیگنال سفید EMAS مدل B0B0XB")


class Ledger(unittest.TestCase):
    def test_costs_add_up(self):
        from crawler import costs
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            costs.add(d, "search", 40, 0.04, estimated=True)
            costs.add(d, "describe", 60, 0.02)
            costs.add(d, "describe", 5, 0.002)
            s = costs.summary(d)
            self.assertAlmostEqual(s["usd"], 0.062, places=4)
            self.assertEqual(s["steps"]["describe"]["units"], 65)
            self.assertAlmostEqual(s["estimated"], round(0.04 / 0.062, 3), places=3)


if __name__ == "__main__":
    unittest.main()


class UnbrandedRoundPrice(unittest.TestCase):
    def test_unbranded_same_price_not_held(self):
        from crawler.pipeline import price_sanity
        recs = [{"url": f"u{i}", "name": n, "brand": "", "price_toman": "450000", "breadcrumb": "اتصالات > زانو"}
                for i, n in enumerate(["زانو ۹۰ پلی اتیلن", "سه راهی پلی اتیلن", "بوشن پلی اتیلن", "درپوش پلی اتیلن"])]
        for r in price_sanity(recs):
            self.assertNotIn("کپی", r.get("price_check", ""))


class NotBrandWords(unittest.TestCase):
    def test_pid_is_not_a_brand(self):
        from crawler.routing import rule_route
        self.assertNotEqual(rule_route("ابزار دقیق > کنترلر دما > کنترلر دما pid").get("kind"), "brand")
        self.assertEqual(rule_route("ابزار دقیق > کنترلر دما > کنترلر دما autonics").get("kind"), "brand")


class ShopNamedProduct(unittest.TestCase):
    def test_page_name_replaces_shop_name(self):
        from bs4 import BeautifulSoup
        from crawler.extract import name_from_page, shop_named
        self.assertTrue(shop_named("نیدصنعت", "نید صنعت"))
        self.assertFalse(shop_named("دریل بتن کن ماکیتا", "نید صنعت"))
        soup = BeautifulSoup("<title>نید صنعت - دریل بتن کن ماکیتا HR2470</title><h1>نید صنعت</h1>", "html.parser")
        self.assertEqual(name_from_page(soup, "نید صنعت"), "دریل بتن کن ماکیتا HR2470")
