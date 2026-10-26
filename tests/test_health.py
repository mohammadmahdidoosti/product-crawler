"""Site health scan: reads the whole catalog with GET only and flags the right products."""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pandas as pd

from crawler import health as H
from crawler import wc as WCM
from crawler.config import load_config

TEH = ZoneInfo("Asia/Tehran")
GOOD_DESC = "<p>" + "این محصول یک شیر برقی صنعتی با بدنه‌ی برنجی است. " * 6 + "</p>"
IMG = [{"id": 9, "src": "https://caravansanat.com/wp-content/uploads/a.webp"}]
CAT = [{"id": 101, "name": "شیر برقی", "slug": "solenoid"}]


def prod(i, **kw):
    p = {"id": i, "name": f"محصول {i}", "status": "publish", "type": "simple", "sku": f"S{i}",
         "permalink": f"https://caravansanat.com/product/{i}", "description": GOOD_DESC, "short_description": "",
         "categories": CAT, "images": IMG, "regular_price": "120", "price": "120", "date_modified": "2026-10-01T10:00:00"}
    p.update(kw)
    return p


class FakeShop(BaseHTTPRequestHandler):
    products: list = []
    methods: list = []

    def log_message(self, *a):
        pass

    def _any(self):
        FakeShop.methods.append(self.command)
        self.send_response(405)
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = _any

    def do_GET(self):
        FakeShop.methods.append("GET")
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        assert u.path == "/wp-json/wc/v3/products", u.path
        assert q.get("status") == "any" and q.get("_fields")
        per, page = int(q["per_page"]), int(q["page"])
        items = FakeShop.products[(page - 1) * per: page * per]
        b = json.dumps(items, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-WP-Total", str(len(FakeShop.products)))
        self.send_header("X-WP-TotalPages", str(-(-len(FakeShop.products) // per)))
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class Health(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeShop)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        pd.DataFrame([{"ID": "3578", "wp_id": "101"}, {"ID": "3579", "wp_id": "102"}]).to_csv(
            data / "wp_categories.csv", index=False, encoding="utf-8-sig")
        self.env = mock.patch.dict(os.environ, {"WC_URL": self.base, "WC_KEY": "ck", "WC_SECRET": "cs",
                                                "DATA_DIR": str(data)})
        self.env.start()
        WCM._CAT_CACHE.update(key=None, map={})
        self.cfg = load_config()
        self.cfg["_data_dir"] = data
        self.cfg["health"] = {**(self.cfg.get("health") or {}), "pause_seconds": 0, "per_page": 100}
        FakeShop.methods = []
        good = [prod(i) for i in range(1, 221)]
        good[0] = prod(1, images=[])                                                   # no photo
        good[1] = prod(2, images=[{"src": "https://x/woocommerce-placeholder.png"}])  # placeholder only
        good[2] = prod(3, description="", short_description="<p> </p>")               # no description
        good[3] = prod(4, description="<p>کوتاه</p>")                                  # short: doubt
        good[4] = prod(5, categories=[])                                                # no category
        good[5] = prod(6, categories=[{"id": 15, "name": "Uncategorized", "slug": "uncategorized"}])
        good[6] = prod(7, categories=[{"id": 555, "name": "قدیمی", "slug": "old"}])     # outside taxonomy: doubt
        good[7] = prod(8, regular_price="45000000")                                     # toman in a dollar field
        good[8] = prod(9, regular_price="")                                             # no price
        good[9] = prod(10, type="variable", regular_price="", price="80")               # variable: fine
        good[10] = prod(11, status="draft", images=[], description="")                  # two problems, a draft
        FakeShop.products = good

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_scan_flags_and_reads_only(self):
        res = H.scan(self.cfg, now=datetime(2026, 10, 27, 3, 30, tzinfo=TEH))
        self.assertEqual(set(FakeShop.methods), {"GET"})                 # nothing written to the site
        self.assertEqual(FakeShop.methods.count("GET"), 3)               # 220 products, 100 per page
        self.assertTrue(res["complete"])
        self.assertEqual(res["products_read"], 220)
        df = pd.read_csv(Path(self.tmp.name) / "health" / "2026-10-27_0330" / "products.csv",
                         encoding="utf-8-sig", dtype=str, keep_default_na=False)
        got = {int(i): set(p.split("|")) for i, p in zip(df["id"], df["problems"])}
        self.assertEqual(got, {1: {"no_image"}, 2: {"no_image"}, 3: {"no_description"}, 4: {"short_description"},
                               5: {"no_category"}, 6: {"no_category"}, 7: {"outside_taxonomy"},
                               8: {"price_toman_suspect"}, 9: {"no_price"}, 11: {"no_image", "no_description"}})
        doubt = set(df[df["doubt"] == "yes"]["id"].astype(int))
        self.assertEqual(doubt, {4, 7, 8})
        self.assertEqual(res["counts"]["no_image"], 3)
        self.assertEqual(res["by_problem_status"]["no_image"], {"publish": 2, "draft": 1})
        self.assertIn("post=11&action=edit", df[df["id"] == "11"]["edit_link"].iloc[0])

    def test_second_scan_counts_new_and_fixed(self):
        H.scan(self.cfg, now=datetime(2026, 10, 27, 3, 30, tzinfo=TEH))
        FakeShop.products[0] = prod(1)                                   # photo added by hand
        FakeShop.products[50] = prod(51, images=[])                      # a new problem
        res = H.scan(self.cfg, now=datetime(2026, 10, 28, 3, 30, tzinfo=TEH))
        self.assertEqual(res["previous"], "2026-10-27_0330")
        self.assertEqual(res["new_since_previous"]["no_image"], 1)
        self.assertEqual(res["new_since_previous"]["fixed"], 1)
        self.assertEqual(H.reports(self.cfg), ["2026-10-28_0330", "2026-10-27_0330"])

    def test_stop_marks_incomplete(self):
        res = H.scan(self.cfg, stop=lambda: True, now=datetime(2026, 10, 27, 3, 30, tzinfo=TEH))
        self.assertFalse(res["complete"])
        self.assertEqual(FakeShop.methods, [])

    def test_nightly_window(self):
        at = lambda h, m=0, d=27: datetime(2026, 10, d, h, m, tzinfo=TEH)   # noqa: E731
        self.assertFalse(H.due(self.cfg, at(3, 0)))
        self.assertTrue(H.due(self.cfg, at(3, 30)))
        self.assertFalse(H.due(self.cfg, at(15, 0)))                     # missed the night: not in busy hours
        H.mark_nightly(self.cfg, at(3, 31))
        self.assertFalse(H.due(self.cfg, at(4, 0)))                      # once a day
        self.assertTrue(H.due(self.cfg, at(3, 45, d=28)))

    def test_plain_text(self):
        self.assertEqual(H.plain_text("<p>شیر&nbsp;برقی</p>[vc_row]<style>a{}</style> ۲ اینچ"), "شیر برقی ۲ اینچ")


if __name__ == "__main__":
    unittest.main()
