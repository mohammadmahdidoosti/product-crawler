"""What 11-13 Nov taught, in code (14 Nov): cost of one run, a PDF's text over its picture reading, a price list's
'-new' run keeps only the list's models, a model with no exact category still gets one."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from crawler import costs as C
from crawler import pricelist as PL
from crawler.io import read_csv, write_csv


class CostSince(unittest.TestCase):
    def test_since_keeps_only_this_run(self):
        d = Path(tempfile.mkdtemp())
        write_csv(pd.DataFrame([
            {"time": "2026-11-11 08:00", "step": "describe", "units": "100", "usd": "5.0", "estimated": "", "note": ""},
            {"time": "2026-11-13 12:00", "step": "describe", "units": "10", "usd": "0.4", "estimated": "", "note": ""},
        ]), d / C.FILE, C.COLS)
        self.assertEqual(C.summary(d)["usd"], 5.4)                        # the batch's whole life
        self.assertEqual(C.summary(d, since="2026-11-13 00:00")["usd"], 0.4)   # this run only


def _pdf(path: Path, lines: list[str]) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for ln in lines:
        c.drawString(40, y, ln)
        y -= 20
    c.save()


class TextCheck(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cfg = {"_data_dir": self.d}
        self.run = PL.run_path(self.cfg, "pl-zim")
        (self.run / "files").mkdir(parents=True, exist_ok=True)

    def items(self, rows):
        write_csv(pd.DataFrame(rows).reindex(columns=PL.ITEM_COLS, fill_value=""), self.run / "items.csv", PL.ITEM_COLS)

    def test_text_beats_the_picture(self):
        _pdf(self.run / "files" / "zim.pdf", ["(Rial) price list", "PZD 9012 449,500,000 1", "PZJ 1175 179,500,000 4"])
        self.items([
            {"brand": "z", "model": "PZD 9012", "key": "PZD9012", "toman": "4395000", "unit": "rial", "file": "zim.pdf ص1"},
            {"brand": "z", "model": "PZJ 1175", "key": "PZJ1175", "toman": "17950000", "unit": "rial", "file": "zim.pdf ص1"},
            {"brand": "z", "model": "PZC 1420", "key": "PZC1420", "toman": "5000000", "unit": "rial", "file": "zim.pdf ص1"},
        ])
        with mock.patch.object(PL, "_page_unit", lambda t: "rial"):
            res = PL.text_check(self.cfg, "pl-zim")
        self.assertEqual((res["fixed"], res["same"], res["not_in_text"]), (1, 1, 1))
        it = read_csv(self.run / "items.csv").set_index("model")
        self.assertEqual(it.loc["PZD 9012", "toman"], "44950000")       # x10 misread fixed from the text
        self.assertEqual(it.loc["PZC 1420", "toman"], "5000000")        # not in the text: left as read
        self.assertTrue((self.run / "items.csv.bak-text").exists())

    def test_scanned_pdf_is_left_alone(self):
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(self.run / "files" / "scan.pdf"))
        c.rect(10, 10, 100, 100)                                          # a page with no text
        c.save()
        self.items([{"brand": "a", "model": "AC-2610", "key": "AC2610", "toman": "6150000", "file": "scan.pdf ص1"}])
        self.assertEqual(PL.text_check(self.cfg, "pl-zim")["checked"], 0)

    def test_page_unit(self):
        self.assertEqual(PL._page_unit("قیمت (به ریال)"), "rial")
        self.assertEqual(PL._page_unit("(لایر هب)"), "rial")                 # pdfplumber's reversed text
        self.assertEqual(PL._page_unit("قیمت به تومان"), "toman")
        self.assertEqual(PL._page_unit("price"), "")


class NewRunKeepsTheList(unittest.TestCase):
    def test_only_list_models_at_list_price(self):
        d = Path(tempfile.mkdtemp())
        cfg = {"_data_dir": d}
        src = PL.run_path(cfg, "pl-zim")
        write_csv(pd.DataFrame([{"model": "PZH 8105", "toman": "32950000"}, {"model": "PZS-1024", "toman": "38450000"}])
                  .reindex(columns=PL.ITEM_COLS, fill_value=""), src / "items.csv", PL.ITEM_COLS)
        from crawler.config import run_dir
        nd = run_dir(cfg, "pl-zim-new")
        nd.mkdir(parents=True, exist_ok=True)
        write_csv(pd.DataFrame([
            {"url": "a", "name": "کارواش زیمبرگ مدل PZH8105", "model": "PZH8105", "price_toman": "30000000"},
            {"url": "b", "name": "کارواش زیمبرگ PZH-8105 فروشگاه دیگر", "model": "", "price_toman": "31000000"},
            {"url": "c", "name": "اره موتوری مکلاچ CS450", "model": "CS450", "price_toman": "34700000"},
            {"url": "d", "name": "پروفیل بر زیمبرگ PZS-1024", "model": "PZS-1024", "price_toman": "40000000"},
        ]), nd / "05_new_products.csv")
        res = PL.lock_new_to_list(cfg, "pl-zim-new")
        self.assertEqual(res, {"kept": 2, "other": 2})
        kept = read_csv(nd / "05_new_products.csv")
        self.assertEqual(dict(zip(kept["url"], kept["price_toman"])), {"a": "32950000", "d": "38450000"})
        self.assertEqual(set(read_csv(nd / "05_other_products.csv")["url"]), {"b", "c"})   # twin model, other maker
        self.assertIsNone(PL.lock_new_to_list(cfg, "batch-12"))                     # not a list's run: untouched


if __name__ == "__main__":
    unittest.main()


class QueueFallsBackToModel(unittest.TestCase):
    def test_no_exact_leaf_still_gets_a_category(self):
        d = Path(tempfile.mkdtemp())
        cfg = {"_data_dir": d}
        tax = pd.DataFrame([{"taxonomy_id": 7, "breadcrumb": "ابزار برقی > مینی فرز", "leaf": "مینی فرز",
                             "level1": "ابزار برقی", "site_product_count": 3}])
        plan = pd.DataFrame([{"action": "new", "model": "PZA 7085", "brand": "زیمبرگ",
                              "list_name": "مینی فرز ۱۱۵ میلیمتری ۸۵۰ وات"},
                             {"action": "new", "model": "XYZ 1", "brand": "زیمبرگ", "list_name": "چیز ناشناخته"}])

        class Idx:
            def __init__(self, *a):
                pass

            def match(self, q, n=25):
                return []                                       # no category whose every word is in the name
        with mock.patch("crawler.searches.load_tax", lambda cfg: tax), \
                mock.patch("crawler.searches.TaxIndex", Idx), \
                mock.patch.object(PL, "plan", lambda cfg, run: plan), \
                mock.patch("crawler.taxonomy.resolve_route", lambda t, r: "default"), \
                mock.patch.object(PL, "_leaf_llm", lambda cfg, idx, name, **k: {"taxonomy_id": 7} if "فرز" in name else None), \
                mock.patch.object(PL, "_broad_llm", lambda cfg, name, **k: None):
            res = PL.queue_new(cfg, "pl-zim")
        self.assertEqual((res["queued"], res["no_category"]), (1, ["XYZ 1"]))


class LaneQueue(unittest.TestCase):
    def test_claim_once_and_finish(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        import batch_lanes as BL
        q = BL.qdir({"_data_dir": Path(tempfile.mkdtemp())}, "t")
        for b in ("101", "102", "9"):
            (q / "todo" / b).touch()
        got = [BL.claim(q), BL.claim(q), BL.claim(q), BL.claim(q)]
        self.assertEqual(got, ["9", "101", "102", None])              # each once, small numbers first
        BL.finish(q, "9", "failed", "prepare: timed out")
        self.assertEqual((q / "failed" / "9").read_text(encoding="utf-8"), "prepare: timed out")
        with BL.Slot(q, 2), BL.Slot(q, 2):                              # two slots: both taken at once
            pass
