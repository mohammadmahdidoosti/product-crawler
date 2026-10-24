"""Direct publishing against a fake WooCommerce + WordPress REST server."""
import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pandas as pd

from crawler import publish as PB
from crawler import wc as WCM
from crawler.config import load_config
from crawler.io import FILES, read_csv, write_csv
from crawler.pipeline import caravan_sku, export_rows


class FakeWoo(BaseHTTPRequestHandler):
    products: dict = {}
    media: list = []
    calls: list = []
    refuse = False

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self):
        return base64.b64decode(self.headers.get("Authorization", "Basic ").split()[1] or b"").decode()

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        FakeWoo.calls.append(("GET", u.path))
        if u.path == "/wp-json/wc/v3/products":
            sku = q.get("sku", [""])[0]
            return self._json([p for p in FakeWoo.products.values() if p["sku"] == sku] if sku
                              else list(FakeWoo.products.values())[:1])
        return self._json({"code": "rest_no_route"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        FakeWoo.calls.append(("POST", u.path))
        if FakeWoo.refuse:
            return self._json({"code": "woocommerce_rest_cannot_create", "message": "no"}, 401)
        if u.path == "/wp-json/wc/v3/products":
            assert self._auth() == "ck_test:cs_test"
            d = json.loads(body)
            pid = 100 + len(FakeWoo.products)
            d.update(id=pid, images=[{"id": i["id"]} for i in d.get("images", [])])
            FakeWoo.products[pid] = d
            return self._json(d, 201)
        if u.path == "/wp-json/wp/v2/media":
            assert self._auth() == "caravan-crawler:abcdabcd"
            assert body[:4] == b"RIFF"
            mid = 500 + len(FakeWoo.media)
            FakeWoo.media.append(self.headers.get("Content-Disposition"))
            return self._json({"id": mid}, 201)
        if u.path.startswith("/wp-json/wp/v2/media/"):
            return self._json({"id": int(u.path.rsplit("/", 1)[1])})
        return self._json({"code": "rest_no_route"}, 404)

    def do_PUT(self):
        u = urlparse(self.path)
        d = json.loads(self._body())
        FakeWoo.calls.append(("PUT", u.path))
        pid = int(u.path.rsplit("/", 1)[1])
        FakeWoo.products[pid].update({k: v for k, v in d.items() if k != "images"})
        if "images" in d:
            FakeWoo.products[pid]["images"] = d["images"]
        return self._json(FakeWoo.products[pid])


def product(i, tax="14339", extra="", price="1500000"):
    return {"url": f"https://shop{i}.ir/product/{i}/", "name": f"Drive {i}", "name_fa": f"درایو {i}",
            "price_toman": price, "breadcrumb": "برق > درایو", "extra_breadcrumbs": "",
            "taxonomy_id": tax, "extra_taxonomy_ids": extra, "brand": "LS" if i % 2 else "",
            "model": f"M{i}", "specs_json": json.dumps({"توان": "2.2kW"}, ensure_ascii=False),
            "usd_rate_date": "2026-10-20", "usd_rate_toman": "100000", "price_usd": "15",
            "source_name": "", "price_estimated": ""}


class Publish(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeWoo)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        FakeWoo.products, FakeWoo.media, FakeWoo.calls, FakeWoo.refuse = {}, [], [], False
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "WC_URL": self.base, "WC_KEY": "ck_test", "WC_SECRET": "cs_test", "WP_USER": "caravan-crawler",
            "WP_APP_PASSWORD": "abcd abcd", "DATA_DIR": str(data), "PUBLIC_URL": "https://crawler.test"})
        self.env.start()
        WCM._CAT_CACHE.update(key=None, map={})
        self.cfg = load_config()
        self.cfg["_data_dir"] = data
        d = data / "runs" / "batch-1"
        d.mkdir(parents=True)
        rows = [product(1), product(2, extra="7067"), product(3, tax="999999"), product(4), product(5, price="")]
        write_csv(pd.DataFrame(rows), d / FILES["new"])
        (data / "images" / "aaaaaaaaaaaaaa").mkdir(parents=True)
        for n in (1, 2):
            (data / "images" / "aaaaaaaaaaaaaa" / f"{n}.webp").write_bytes(b"RIFF....WEBPVP8 fake")
        write_csv(pd.DataFrame([{"url": rows[0]["url"], "files": "aaaaaaaaaaaaaa/1.webp | aaaaaaaaaaaaaa/2.webp"}]),
                  d / "05_images.csv")
        self.d = d

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_export_has_stable_sku(self):
        rows = export_rows(self.cfg, "batch-1")
        self.assertEqual(len(rows), 4)                       # the product without a price is not exported
        self.assertEqual(rows[0][1]["SKU"], caravan_sku("https://shop1.ir/product/1/"))
        self.assertRegex(rows[0][1]["SKU"], r"^CRV-[0-9A-F]{10}$")
        self.assertEqual(rows[0][1]["Meta: _caravan_source_url"], "https://shop1.ir/product/1/")

    def test_dry_run_sends_nothing(self):
        res = PB.publish(self.cfg, "batch-1", limit=10, dry_run=True)
        self.assertEqual(res["counts"]["checked"], 3)
        self.assertEqual(res["counts"]["skipped"], 1)        # taxonomy 999999 has no WordPress category
        self.assertFalse([c for c in FakeWoo.calls if c[0] != "GET"])
        self.assertTrue((self.d / "07_publish_check.csv").exists())
        self.assertFalse((self.d / PB.RESULT_FILE).exists())

    def test_publish_creates_drafts_then_updates(self):
        res = PB.publish(self.cfg, "batch-1", limit=2)
        self.assertEqual(res["counts"]["created"], 2)
        p1, p2 = FakeWoo.products[100], FakeWoo.products[101]
        self.assertEqual(p1["status"], "draft")
        self.assertEqual(p1["categories"], [{"id": 1635}])
        self.assertEqual(p2["categories"], [{"id": 1635}, {"id": 1636}])  # extra category from a merged page
        self.assertEqual(p1["regular_price"], "1500000")
        self.assertEqual(p1["attributes"][0]["options"], ["LS"])
        self.assertNotIn("attributes", p2)
        self.assertEqual(len(p1["images"]), 2)               # local photos uploaded to the media library
        self.assertEqual(len(FakeWoo.media), 2)
        self.assertIn(p1["sku"].lower(), FakeWoo.media[0])
        self.assertIn({"key": "_caravan_taxonomy_id", "value": "14339"}, p1["meta_data"])
        out = read_csv(self.d / PB.RESULT_FILE)
        self.assertEqual(list(out["action"]), ["created", "created"])
        self.assertIn("post.php?post=100", out["edit_url"].iloc[0])

        # next call continues with the products not yet sent
        res = PB.publish(self.cfg, "batch-1", limit=5)
        self.assertEqual(res["counts"], {"created": 1, "updated": 0, "skipped": 1, "error": 0, "checked": 0})
        self.assertEqual(len(FakeWoo.products), 3)

        # someone publishes product 100 on the site; a republish updates it without un-publishing it
        FakeWoo.products[100]["status"] = "publish"
        res = PB.publish(self.cfg, "batch-1", limit=1, republish=True)
        self.assertEqual(res["counts"]["updated"], 1)
        self.assertEqual(FakeWoo.products[100]["status"], "publish")
        self.assertEqual(len(FakeWoo.media), 2)              # it already had photos: none uploaded again
        self.assertEqual(len(FakeWoo.products), 3)           # no duplicate
        self.assertEqual(len(read_csv(self.d / PB.RESULT_FILE)), 4)   # one row per SKU

    def test_publish_by_sku(self):
        sku = caravan_sku("https://shop4.ir/product/4/")
        res = PB.publish(self.cfg, "batch-1", skus=[sku])
        self.assertEqual(res["counts"]["created"], 1)
        self.assertEqual(FakeWoo.products[100]["sku"], sku)

    def test_refused_key_stops(self):
        FakeWoo.refuse = True
        res = PB.publish(self.cfg, "batch-1", limit=3)
        self.assertEqual(res["counts"]["error"], 1)          # stops after the first refusal
        self.assertIn("401", read_csv(self.d / PB.RESULT_FILE)["error"].iloc[0])

    def test_category_map_not_under_data(self):
        self.assertEqual(WCM.category_map().get("14339"), "1635")   # from assets/, DATA_DIR has none


if __name__ == "__main__":
    unittest.main()
