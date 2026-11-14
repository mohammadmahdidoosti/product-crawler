"""(14 Nov) Keyword categories: made on the site only when a product goes there, parents first, a new main
category at the top, nothing made twice."""
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from crawler import catmake as CM
from crawler.io import read_csv, write_csv

COLS = ["ID", "Level 1", "Level 2", "Level 3", "Level 4", "Full Breadcrumb Path", "Parent_ID", "Category_Name"]


class FakeResp:
    def __init__(self, j):
        self._j, self.ok, self.status_code = j, True, 201

    def json(self):
        return self._j


class FakeWC:
    def __init__(self):
        self.made, self.next = [], 900

    def _call(self, fn, what):
        return fn()

    def _wc(self, method, path, json=None, params=None):
        if method == "GET":
            return [t for t in self.made if t["name"] == params["search"] and t["parent"] == params["parent"]]
        same = [t for t in self.made if t["name"] == json["name"] and t["parent"] == json["parent"]]
        if same:                                        # what WooCommerce answers for a name it has
            r = FakeResp({"code": "term_exists", "data": {"status": 400, "resource_id": same[0]["id"]}})
            r.ok, r.status_code = False, 400
            return r
        self.next += 1
        t = {"id": self.next, "name": json["name"], "parent": json["parent"]}
        self.made.append(t)
        return FakeResp(t)


class EnsureWP(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        rows = [
            ["1", "جوشکاری و برش", "", "", "", "جوشکاری و برش", "", "جوشکاری و برش"],
            ["2", "جوشکاری و برش", "الکترود", "", "", "جوشکاری و برش > الکترود", "1", "الکترود"],
            ["3", "جوشکاری و برش", "الکترود", "الکترود 7018", "", "جوشکاری و برش > الکترود > الکترود 7018", "2", "الکترود 7018"],
            ["4", "رنگ و پوشش", "", "", "", "رنگ و پوشش", "", "رنگ و پوشش"],
            ["5", "رنگ و پوشش", "رنگ اکریلیک", "", "", "رنگ و پوشش > رنگ اکریلیک", "4", "رنگ اکریلیک"],
        ]
        write_csv(pd.DataFrame(rows, columns=COLS), self.d / "taxonomy.csv")
        write_csv(pd.DataFrame([{"ID": "1", "wp_id": "50"}]), self.d / "wp_categories.csv")
        self.cfg = {"_data_dir": self.d, "taxonomy": {"file": str(self.d / "taxonomy.csv")}}

    def test_parents_first_and_new_main_at_top(self):
        wc = FakeWC()
        res = CM.ensure_wp(self.cfg, ["3", "5", "3"], wc=wc)
        self.assertEqual(res["made"], 4)
        by = {t["name"]: t for t in wc.made}
        self.assertEqual(by["الکترود"]["parent"], 50)                   # under the existing main category
        self.assertEqual(by["الکترود 7018"]["parent"], by["الکترود"]["id"])
        self.assertEqual(by["رنگ و پوشش"]["parent"], 0)                  # a new main category: at the top
        self.assertEqual(by["رنگ اکریلیک"]["parent"], by["رنگ و پوشش"]["id"])
        wp = dict(zip(*[read_csv(self.d / "wp_categories.csv")[c] for c in ("ID", "wp_id")]))
        self.assertEqual(set(wp), {"1", "2", "3", "4", "5"})
        self.assertEqual(CM.ensure_wp(self.cfg, ["3", "5"], wc=wc)["made"], 0)   # nothing twice
        self.assertEqual(len(wc.made), 4)


    def test_made_by_another_lane_meanwhile(self):
        wc = FakeWC()
        wc.made.append({"id": 77, "name": "الکترود", "parent": 50})    # another lane made it a moment ago
        CM.ensure_wp(self.cfg, ["3"], wc=wc)
        wp = dict(zip(*[read_csv(self.d / "wp_categories.csv")[c] for c in ("ID", "wp_id")]))
        self.assertEqual(wp["2"], "77")
        self.assertEqual(sum(t["name"] == "الکترود" for t in wc.made), 1)


if __name__ == "__main__":
    unittest.main()
