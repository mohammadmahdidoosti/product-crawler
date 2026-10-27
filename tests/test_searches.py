"""Failed site searches: the plugin's real export is cleaned, grouped and matched, nothing is
crawled before a person approves it, and an approved row becomes a crawl queue row."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from crawler import searches as S
from crawler.config import load_config
from crawler.io import read_csv

FIX = Path(__file__).parent / "fixtures"
EXPORT = (FIX / "webisearch_not_in_catalog.csv").read_bytes()      # 132 real terms (no personal data)


class Searches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        shutil.copy(FIX / "search_taxonomy.csv", data / "taxonomy.csv")
        self.env = mock.patch.dict(os.environ, {"DATA_DIR": str(data)})
        self.env.start()
        self.cfg = load_config()
        self.cfg["_data_dir"] = data
        self.cfg["taxonomy"]["file"] = str(data / "taxonomy.csv")
        S.upload(self.cfg, EXPORT, "export.csv")
        self.r = S.review(self.cfg)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def row(self, term):
        hit = self.r[self.r["members"].str.split(" | ", regex=False).map(lambda m: term in m)]
        self.assertEqual(len(hit), 1, term)
        return hit.iloc[0]

    def test_reads_the_plugin_export(self):
        exp = S.read_export(pd.read_csv(FIX / "webisearch_not_in_catalog.csv", dtype=str, keep_default_na=False,
                                        encoding="utf-8-sig"))
        self.assertEqual(len(exp), 132)
        self.assertEqual(exp.iloc[0].to_dict(), {"term": "برقو", "searches": 9, "last_search": "2026-08-18 06:42:00"})
        other = pd.DataFrame({"عبارت جستجو": ["a", "b"], "جستجوها": ["1", "2"], "وضعیت": ["نتیجه نزدیک", "در محصولات شما نیست"]})
        self.assertEqual(list(S.read_export(other)["term"]), ["b"])      # other groups are left out

    def test_one_search_typed_many_ways_is_one_row(self):
        r = self.row("فلسایز 355")
        self.assertEqual(len(r["members"].split(" | ")), 13)           # typed one after another, 10-16 13:31-13:32
        self.assertEqual(r["searches"], "17")
        self.assertEqual(r["doubt"], "yes")
        self.assertNotEqual(r["proposal"], "approve")
        self.assertIn("گیج فش", self.row("گیج ف")["members"])           # a start of the other
        self.assertEqual(len(self.row("Airtac4v210-08")["members"].split(" | ")), 4)   # one model code
        cp = self.row("لوله cpvc75")                                      # spaces only
        self.assertIn("لوله cpvc 75", cp["members"])
        self.assertNotIn("پشت‌سرهم", cp["note"])                          # not doubtful: the same thing
        self.assertNotEqual(self.row("دیاگ")["key"], self.row("ردیاب")["key"])   # alike in time, not in text

    def test_cleaning(self):
        self.assertEqual(self.row("کمربندسایز 90")["cleaned"], "کمربند سایز 90")
        self.assertEqual(self.row("شیرخودکار‌یک‌اینچ‌پلیمری")["cleaned"], "شیر خودکار یک اینچ پلیمری")
        self.assertEqual(self.row("فلنج ۱۶۰UPVC")["cleaned"], "فلنج 160 upvc")
        self.assertEqual(self.row("uhdr")["cleaned"], "عایق")                     # English keyboard
        self.assertEqual(self.row("نراز لیزری")["cleaned"], "تراز لیزری")
        self.assertEqual(self.row("چرتغ سیگنال")["cleaned"], "چراغ سیگنال")
        for t in ("سوکت دوبل", "شیر موتوری", "بوبین airmod", "ارسال ایمیل باز کردن"):  # real words stay
            self.assertEqual(S.norm_text(self.row(t)["cleaned"]), S.basic(t), t)
        self.assertIn("اصلاح املایی", self.row("نراز لیزری")["note"])          # every fix is shown

    def test_kinds_and_proposals(self):
        self.assertEqual(self.row("دیدن فاکتور خرید")["kind"], "not_product")
        self.assertEqual(self.row("دیدن فاکتور خرید")["proposal"], "reject")
        self.assertEqual(self.row("ETE5PVCB")["kind"], "model")
        self.assertEqual(self.row("اشنایدر")["kind"], "brand")
        self.assertEqual(self.row("شمثث")["kind"], "unclear")
        b = self.row("برقو")
        self.assertEqual((b["proposal"], b["taxonomy_id"], b["doubt"]), ("approve", "2", ""))
        self.assertEqual(self.row("لوله ۶۳ CPVC")["taxonomy_id"], "9")
        self.assertEqual(self.row("۶۳ آمپر محافظ کنتور")["taxonomy_id"], "5")
        k = self.row("کارتریج هیدرولیک")
        self.assertEqual((k["taxonomy_id"], k["proposal"]), ("", "check"))
        self.assertIn("دسته‌ی تازه", k["note"])
        self.assertEqual(self.row("فیوز gpv")["proposal"], "check")             # weak match: a person decides
        self.assertEqual(self.r["decision"].tolist(), [""] * len(self.r))      # nothing decided by itself

    def test_nothing_queued_without_approval(self):
        with self.assertRaises(ValueError):
            S.make_queue(self.cfg)
        self.assertFalse((Path(self.tmp.name) / "runs").exists() and any((Path(self.tmp.name) / "runs").iterdir()))

    def test_decide_and_queue(self):
        k = {t: self.row(t)["key"] for t in ("برقو", "لوله ۶۳ CPVC", "لوله cpvc75", "کارتریج هیدرولیک", "شمثث")}
        S.decide(self.cfg, [{"key": k["برقو"], "decision": "approved"},
                            {"key": k["لوله ۶۳ CPVC"], "decision": "approved"},
                            {"key": k["لوله cpvc75"], "decision": "approved", "query": "لوله cpvc سایز 75"},
                            {"key": k["کارتریج هیدرولیک"], "decision": "approved"},     # no category: not crawled
                            {"key": k["شمثث"], "decision": "rejected"}])
        sm = S.summary(self.cfg)
        self.assertEqual((sm["approved"], sm["rejected"], sm["ready"], sm["approved_no_category"]), (4, 1, 3, 1))
        with self.assertRaises(ValueError):
            S.decide(self.cfg, [{"key": k["برقو"], "decision": "approved", "taxonomy_id": "99999"}])
        res = S.make_queue(self.cfg, run="search-test")
        self.assertEqual((res["categories"], res["searches"]), (2, 3))
        q = read_csv(Path(self.tmp.name) / "runs" / "search-test" / "01_queue.csv")
        cp = q[q["taxonomy_id"] == "9"].iloc[0]
        # one queue row per category: the approved terms are its searches (a person's wording kept)
        self.assertEqual({cp["query"], cp["extra_queries"]}, {"لوله cpvc سایز 75", "لوله 63 cpvc"})
        self.assertEqual(q[q["taxonomy_id"] == "2"].iloc[0]["route"], "iran")
        # queued rows are not queued twice nor changed
        self.assertEqual(S.summary(self.cfg)["queued"], 3)
        self.assertEqual(S.decide(self.cfg, [{"key": k["برقو"], "decision": "rejected"}])["skipped"], [k["برقو"]])
        with self.assertRaises(ValueError):
            S.make_queue(self.cfg)

    def test_decisions_survive_a_new_upload(self):
        key = self.row("برقو")["key"]
        S.decide(self.cfg, [{"key": key, "decision": "rejected"}])
        S.upload(self.cfg, EXPORT, "again.csv")
        self.assertEqual(S.review(self.cfg).set_index("key").loc[key, "decision"], "rejected")

    def test_category_picker_search(self):
        idx = S.TaxIndex(S.load_tax(self.cfg), self.cfg)
        self.assertEqual([x["taxonomy_id"] for x in idx.search("فیوز")], ["4", "5"])

    def test_search_step_uses_the_approved_terms(self):
        from crawler import pipeline as P
        seen = []
        S.decide(self.cfg, [{"key": self.row("برقو")["key"], "decision": "approved"}])
        S.make_queue(self.cfg, run="search-x")
        with mock.patch.object(P, "search_row", lambda cfg, f, row, en, probes: seen.append(row) or []), \
                mock.patch.object(P, "_english_queries", lambda cfg, rows: {}):
            P.step_search(self.cfg, "search-x")
        self.assertEqual(seen[0]["query"], "برقو")
        self.assertTrue(seen[0]["synonyms"].startswith("برقو برقی"))          # the taxonomy's synonyms still follow


class SearchesApi(unittest.TestCase):
    def test_routes(self):
        import app as A
        tmp = tempfile.mkdtemp()
        shutil.copy(FIX / "search_taxonomy.csv", Path(tmp) / "taxonomy.csv")
        with mock.patch.object(A, "DATA_DIR", Path(tmp)):
            h = mock.MagicMock()
            out = {}

            def call(method, path, body=b"", headers=None):
                h.path, h.headers = path, {"Content-Length": str(len(body)), **(headers or {})}
                h.rfile.read.return_value = body
                h._json = lambda obj, code=200: out.update(code=code, obj=obj)
                h._err = lambda msg, code=400: out.update(code=code, obj={"error": msg})
                h._download = lambda data, name, ctype: out.update(code=200, obj=data)
                h._authed = lambda: True
                h._body = lambda: body
                h._jbody = lambda: json.loads(body or b"{}")
                getattr(A.Handler, f"do_{method}")(h)
                return out["code"], out["obj"]

            code, r = call("POST", "/api/searches/upload", EXPORT, {"X-Filename": "x.csv"})
            self.assertEqual(code, 200, r)
            self.assertEqual(r["meta"]["terms"], 132)
            code, r = call("GET", "/api/searches?filter=approve")
            self.assertTrue(r["rows"])
            self.assertEqual({x["proposal"] for x in r["rows"]}, {"approve"})
            key = r["rows"][0]["key"]
            code, r = call("POST", "/api/searches/decide", json.dumps({"rows": [{"key": key, "decision": "approved"}]}).encode())
            self.assertEqual((code, r["approved"]), (200, 1))
            with mock.patch.object(A, "start_job", return_value=None) as sj:
                code, r = call("POST", "/api/searches/queue", b"{}")
                self.assertEqual(code, 200)
                sj.assert_not_called()                                        # building a queue starts nothing
            code, r = call("GET", "/api/searches/taxonomy?q=" + "فیوز")
            self.assertEqual(len(r["rows"]), 2)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
