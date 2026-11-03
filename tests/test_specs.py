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


class AtPublish(unittest.TestCase):
    def test_prefetch_once_then_apply(self):
        import tempfile
        from unittest import mock
        from crawler import specs as SP
        rows = [{"name": "نمایشگر", "value": "۲۰۰۰ شمارش"}, {"name": "ولتاژ DC", "value": "تا ۶۰۰ ولت"}]
        with tempfile.TemporaryDirectory() as t, mock.patch.object(SP, "to_pairs", return_value=(rows, 0.0001)) as tp:
            cache, usd, calls = SP.prefetch(t, [("مولتی متر", HTML), ("بی‌فهرست", "<p>x</p>")], "k")
            self.assertEqual(calls, 1)
            cache2, _, calls2 = SP.prefetch(t, [("مولتی متر", HTML)], "k")
            self.assertEqual(calls2, 0)                    # cached: a re-send costs nothing
            self.assertEqual(tp.call_count, 1)
        data = {"description": HTML, "attributes": [{"name": "برند", "options": ["یونیتی"], "visible": True,
                                                     "variation": False}]}
        out = SP.apply(data, cache2)
        self.assertEqual([a["name"] for a in out["attributes"]], ["برند", "نمایشگر", "ولتاژ DC"])
        self.assertNotIn("ویژگی", out["description"])

    def test_apply_without_rows_leaves_payload(self):
        from crawler import specs as SP
        data = {"description": HTML, "attributes": []}
        self.assertEqual(SP.apply(dict(data), {}), data)


class SiteRun(unittest.TestCase):
    def test_only_products_with_a_list_change_and_backup_kept(self):
        import json, tempfile
        from pathlib import Path
        from unittest import mock
        from crawler import specs as SP

        class FakeWC:
            def __init__(self):
                self.sent = []

            def products_page(self, page, per_page, fields, status):
                if page > 1:
                    return [], 2, 1
                return [{"id": 1, "name": "a", "description": HTML, "attributes": []},
                        {"id": 2, "name": "b", "description": "<p>بی‌فهرست</p>", "attributes": []}], 2, 1

            def batch_update(self, items):
                self.sent += items
                return {"update": [{"id": x["id"]} for x in items]}

        rows = [{"name": "نمایشگر", "value": "۲۰۰۰"}, {"name": "ولتاژ DC", "value": "تا ۶۰۰ ولت"}]
        with tempfile.TemporaryDirectory() as t, mock.patch.object(SP, "to_pairs", return_value=(rows, 0.0)), \
                mock.patch("crawler.config.secret", return_value="k"):
            wc = FakeWC()
            st = SP.site_run({"_data_dir": t}, wc=wc)
            self.assertEqual((st["read"], st["with_list"], st["changed"]), (2, 1, 1))
            self.assertEqual(wc.sent[0]["id"], 1)
            self.assertEqual(json.loads((Path(t) / "specs_backup.jsonl").read_text().splitlines()[0])["id"], 1)


if __name__ == "__main__":
    unittest.main()


class DescriptionTable(unittest.TestCase):
    """3 Nov: the «مشخصات فنی» table in the text goes into the product's spec table (no slug names)."""
    H = ('<p>متن</p>\n<h3>مشخصات فنی</h3>\n<table>\n<tr>\n<th>برند</th>\n<td>کالن</td>\n</tr>\n'
         '<tr><th>pa_response-type</th><td>واکنش سریع</td></tr><tr><th>pa_response-temp</th><td>68 درجه</td></tr>'
         '<tr><th>pa_zzz-qqq</th><td>چیزی</td></tr></table>')

    def test_slug_names(self):
        from crawler.specs import fa_name
        self.assertEqual(fa_name("pa_working-pressure"), "فشار کاری")
        self.assertEqual(fa_name("pa_response-temp"), "دمای عملکرد")
        self.assertEqual(fa_name("pa_made-in"), "کشور سازنده")
        self.assertIsNone(fa_name("pa_zzz-qqq"))
        self.assertEqual(fa_name("وزن"), "وزن")

    def test_table_moves_to_attributes(self):
        from crawler.specs import apply
        d = apply({"description": self.H, "attributes": [{"name": "برند", "options": ["کالن"]},
                                                          {"name": "دمای کار", "options": ["۶۸ درجه"]}]}, {})
        self.assertEqual(d["description"], "<p>متن</p>")
        names = [a["name"] for a in d["attributes"]]
        self.assertIn("نوع عملکرد", names)
        self.assertNotIn("دمای عملکرد", names)          # its value is already shown (Persian digits)
        self.assertEqual(names.count("برند"), 1)
        self.assertFalse(any(n.startswith("pa_") for n in names))
