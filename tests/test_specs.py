import unittest

from crawler.specs import clean_rows, feature_items, merged, without_features

HTML = ("<p>مولتی متر</p><h3>ویژگی‌ها</h3><ul><li>نمایشگر ۲۰۰۰ شمارشی</li>"
        "<li>اندازه‌گیری ولتاژ <strong>DC</strong> تا ۶۰۰ ولت</li><li>دارای یک سال گارانتی</li></ul>"
        "<h3>کاربردها</h3><ul><li>تعمیرات</li></ul>")


class FeatureItems(unittest.TestCase):
    def test_list_under_heading(self):
        self.assertEqual(feature_items(HTML), ["نمایشگر ۲۰۰۰ شمارشی", "اندازه‌گیری ولتاژ DC تا ۶۰۰ ولت",
                                               "دارای یک سال گارانتی"])

    def test_no_heading(self):
        self.assertEqual(feature_items("<p>متن</p><ul><li>x</li></ul>"), [])


class CleanRows(unittest.TestCase):
    items = ["نمایشگر ۲۰۰۰ شمارشی", "اندازه‌گیری ولتاژ DC تا ۶۰۰ ولت"]

    def test_invented_number_dropped(self):
        rows = [{"name": "نمایشگر", "value": "۲۰۰۰ شمارش"}, {"name": "ولتاژ DC", "value": "تا ۱۰۰۰ ولت"}]
        self.assertEqual(clean_rows(rows, self.items), [{"name": "نمایشگر", "value": "۲۰۰۰ شمارش"}])

    def test_duplicate_and_empty(self):
        rows = [{"name": "نمایشگر", "value": "۲۰۰۰"}, {"name": "نمایشگر", "value": "LCD"}, {"name": "", "value": "x"}]
        self.assertEqual(len(clean_rows(rows, self.items)), 1)


class Numbers(unittest.TestCase):
    def test_persian_decimal_and_words(self):
        items = ["جریان AC از ۳٫۰۰۰ تا ۶۰۰٫۰ آمپر", "دارای یک سال گارانتی"]
        rows = [{"name": "جریان AC", "value": "3 تا 600 آمپر"}, {"name": "گارانتی", "value": "۱ سال"}]
        self.assertEqual(len(clean_rows(rows, items)), 2)


class Merged(unittest.TestCase):
    def test_brand_kept_and_rows_added(self):
        a = [{"id": 0, "name": "برند", "options": ["یونیتی"], "visible": True, "variation": False, "position": 0}]
        out = merged(a, [{"name": "برند", "value": "x"}, {"name": "گارانتی", "value": "۱ سال"}])
        self.assertEqual([x["name"] for x in out], ["برند", "گارانتی"])
        self.assertEqual(out[0]["options"], ["یونیتی"])
        self.assertEqual(out[1]["options"], ["۱ سال"])


class WithoutFeatures(unittest.TestCase):
    items = ["a", "b", "c"]

    def test_removed_when_rows_cover_the_list(self):
        rows = [{"name": "x", "value": "1"}, {"name": "y", "value": "2"}]
        out = without_features(HTML, self.items, rows)
        self.assertNotIn("ویژگی", out)
        self.assertIn("کاربردها", out)

    def test_kept_when_few_rows(self):
        self.assertEqual(without_features(HTML, self.items, [{"name": "x", "value": "1"}]), HTML)


if __name__ == "__main__":
    unittest.main()
