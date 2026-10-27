"""Site fix: a preview built with the crawler's own photo / description steps, nothing sent before
approval, and the apply step sends only photos or descriptions (never a price) to products that
still need them."""
import io
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
from PIL import Image

from crawler import health as H
from crawler import sitefix as SF
from crawler import wc as WCM
from crawler.config import load_config
from crawler.io import read_csv, write_csv

DESC = "<p>" + "این پمپ برای انتقال آب در ساختمان و باغ ساخته شده است. " * 8 + "</p>"


def png() -> bytes:
    im = Image.new("RGB", (600, 600), "white")
    for x in range(200, 400):
        for y in range(150, 450):
            im.putpixel((x, y), (30, 60, 120))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


class FakeWoo(BaseHTTPRequestHandler):
    products: dict = {}
    calls: list = []
    media: list = []
    change_price_on_update = False

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        FakeWoo.calls.append(("GET", u.path, q))
        assert q.get("context") == "edit", "prices must be read as stored"
        if u.path == "/wp-json/wc/v3/products":
            ids = [int(x) for x in q["include"].split(",")]
            return self._json([FakeWoo.products[i] for i in ids if i in FakeWoo.products])
        pid = int(u.path.rsplit("/", 1)[1])
        return self._json(FakeWoo.products[pid])

    def do_PUT(self):
        u = urlparse(self.path)
        data = json.loads(self._body())
        FakeWoo.calls.append(("PUT", u.path, data))
        pid = int(u.path.rsplit("/", 1)[1])
        p = FakeWoo.products[pid]
        if "images" in data:
            p["images"] = [{"id": i["id"], "src": f"https://x/{i['id']}.webp"} for i in data["images"]]
        p.update({k: v for k, v in data.items() if k != "images"})
        if FakeWoo.change_price_on_update:
            p["regular_price"] = "999999"
        return self._json(p)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        FakeWoo.calls.append(("POST", u.path, len(body)))
        if u.path == "/wp-json/wp/v2/media":
            FakeWoo.media.append(len(body))
            return self._json({"id": 500 + len(FakeWoo.media)})
        return self._json({}, 404)


def site_product(pid, name, **kw):
    p = {"id": pid, "name": name, "status": "publish", "type": "simple", "sku": f"S{pid}",
         "permalink": f"https://caravansanat.com/product/{pid}", "description": DESC, "short_description": "",
         "categories": [{"id": 101, "name": "پمپ آب"}], "images": [{"id": 1, "src": "https://x/1.jpg"}],
         "brands": [], "attributes": [], "regular_price": "45.5"}
    p.update(kw)
    return p


class SiteFix(unittest.TestCase):
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
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"WC_URL": self.base, "WC_KEY": "ck", "WC_SECRET": "cs",
                                                "WP_USER": "u", "WP_APP_PASSWORD": "p", "DATA_DIR": str(self.data),
                                                "PUBLIC_URL": "https://crawler.test", "OPENROUTER_API_KEY": "",
                                                "SERPER_API_KEY": ""})
        self.env.start()
        WCM._CAT_CACHE.update(key=None, map={})
        self.cfg = load_config()
        self.cfg["_data_dir"] = self.data
        self.cfg["images"]["remove_background"] = False      # no ONNX model in the tests
        FakeWoo.calls, FakeWoo.media, FakeWoo.change_price_on_update = [], [], False
        FakeWoo.products = {
            11: site_product(11, "پمپ آب خانگی پنتاکس PM45", images=[]),
            12: site_product(12, "الکتروپمپ محیطی دیزل 1 اسب", images=[]),
            13: site_product(13, "کلید فلوتر برقی", images=[]),
            21: site_product(21, "شیر برقی 2 اینچ", description=""),
            22: site_product(22, "شیر توپی برنجی", description="<p>شیر خوب</p>"),
        }
        # a health report as the scan writes it
        rep = H.health_dir(self.cfg) / "2026-10-27_0330"
        rep.mkdir(parents=True)
        rows = [{"id": "11", "status": "publish", "problems": "no_image"},
                {"id": "12", "status": "publish", "problems": "no_image"},
                {"id": "13", "status": "draft", "problems": "no_image|no_description"},
                {"id": "21", "status": "publish", "problems": "no_description"},
                {"id": "22", "status": "publish", "problems": "short_description"}]
        write_csv(pd.DataFrame(rows).reindex(columns=H.COLS, fill_value=""), rep / "products.csv", H.COLS)
        (rep / "summary.json").write_text("{}", encoding="utf-8")
        # the crawl pool knows product 11 (with a photo) from an earlier run
        pool = pd.DataFrame([{"name": "پمپ آب خانگی پنتاکس مدل PM45", "url": "https://shop-a.ir/p/pm45",
                              "image_urls": json.dumps(["https://shop-a.ir/pm45.jpg"]), "source_domain": "shop-a.ir",
                              "source_type": "iran", "specs_json": json.dumps({"توان": "0.5 اسب"}, ensure_ascii=False),
                              "description": "پمپ خانگی", "brand": "پنتاکس", "model": "PM45", "price_toman": "1000000"}])
        write_csv(pool, self.data / "crawl_pool.csv")
        self.pool_before = (self.data / "crawl_pool.csv").read_bytes()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def web(self, cfg, f, name, need_photo):
        if "الکتروپمپ" in name:
            return {"name": name, "brand": "", "model": "", "specs_json": "{}", "description": "",
                    "short_description": "", "image_urls": json.dumps(["https://shop-b.ir/ep.jpg"]),
                    "source_domain": "shop-b.ir", "seller_name": "فروشگاه ب", "url": "https://shop-b.ir/p/ep", "_how": "name"}
        return None

    def prepare(self, kind, **kw):
        with mock.patch.object(SF, "find_on_web", self.web), \
                mock.patch("crawler.images.download", lambda f, u, min_side=250: png()):
            return SF.prepare(self.cfg, kind, run=f"fix-{kind}-20261027-1000", **kw)

    def test_same_product(self):
        self.assertTrue(SF.same_product("پمپ آب خانگی پنتاکس PM45", "پمپ آب پنتاکس مدل PM-45 خانگی")[0])
        self.assertFalse(SF.same_product("پمپ آب خانگی پنتاکس PM45", "پمپ آب خانگی پنتاکس PM60")[0])
        self.assertTrue(SF.same_product("شیر توپی برنجی", "خرید شیر توپی برنجی")[0])
        self.assertFalse(SF.same_product("شیر توپی برنجی", "شیر برقی برنجی")[0])
        self.assertFalse(SF.same_product("پمپ", "پمپ آب خانگی دو اسب پنتاکس")[0])

    def test_photo_preview_sends_nothing(self):
        res = self.prepare("photo", limit=10)
        self.assertEqual(res["rows"], 3)
        self.assertEqual(res["found"], {"pool": 1, "web": 1, "none": 1})
        self.assertEqual({c[0] for c in FakeWoo.calls}, {"GET"})          # nothing written to the site
        pv = SF.preview(self.cfg, res["run"]).set_index("key")
        self.assertTrue(pv.loc["11", "proposal"].startswith("https://crawler.test/img/"))
        self.assertEqual(pv.loc["11", "doubt"], "")
        self.assertIn("pool", pv.loc["11", "found_by"])
        self.assertEqual(pv.loc["13", "proposal"], "")
        self.assertIn("عکسی ساخته نشد", pv.loc["13", "note"])
        self.assertEqual(list(pv.index[:2]), ["12", "11"])                 # published first, newest first
        # the crawler's own state is left alone
        self.assertEqual((self.data / "crawl_pool.csv").read_bytes(), self.pool_before)
        self.assertFalse((self.data / "no_category.csv").exists())
        self.assertEqual(sorted(p.name for p in (self.data / "runs").iterdir()), [res["run"]])

    def test_text_preview(self):
        res = self.prepare("text")
        pv = SF.preview(self.cfg, res["run"]).set_index("key")
        self.assertEqual(sorted(pv.index), ["13", "21", "22"])
        for k in pv.index:                                   # no model key here: the free template, flagged
            self.assertTrue(pv.loc[k, "html"].startswith("<p>"))
            self.assertIn("قالب ساده", pv.loc[k, "note"])
        new = read_csv(self.data / "runs" / res["run"] / "05_new_products.csv").set_index("product_id")
        self.assertIn("شیر خوب", new.loc["22", "description"])         # its own short text is a source
        self.assertEqual({c[0] for c in FakeWoo.calls}, {"GET"})

    def test_apply_only_approved_and_only_photos(self):
        res = self.prepare("photo")
        run = res["run"]
        with self.assertRaises(ValueError):
            SF.decide(self.cfg, run, [{"key": "11", "decision": "maybe"}])
        SF.decide(self.cfg, run, [{"key": "11", "decision": "approved"}, {"key": "12", "decision": "approved"},
                                  {"key": "13", "decision": "approved"}])     # 13 has nothing to send
        FakeWoo.products[12]["images"] = [{"id": 9, "src": "x"}]            # someone added a photo meanwhile
        out = SF.apply(self.cfg, run)
        self.assertEqual((out["applied"], out["skipped"], out["failed"]), (1, 1, 0))
        puts = [c for c in FakeWoo.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(list(puts[0][2]), ["images"])                       # never a price, name or category
        self.assertEqual(FakeWoo.products[11]["regular_price"], "45.5")
        self.assertEqual(SF.apply(self.cfg, run)["applied"], 1)             # a second click sends nothing new
        self.assertEqual(len([c for c in FakeWoo.calls if c[0] == "PUT"]), 1)

    def test_apply_text_and_price_guard(self):
        res = self.prepare("text")
        run = res["run"]
        SF.decide(self.cfg, run, [{"key": "21", "decision": "approved"}, {"key": "22", "decision": "approved"}])
        FakeWoo.change_price_on_update = True                                # e.g. a plugin re-saving prices
        out = SF.apply(self.cfg, run)
        self.assertTrue(out["halted"])
        self.assertEqual(out["failed"], 1)
        self.assertEqual(len([c for c in FakeWoo.calls if c[0] == "PUT"]), 1)   # it stopped after the first
        put = [c for c in FakeWoo.calls if c[0] == "PUT"][0][2]
        self.assertTrue(set(put) <= {"description", "short_description"})

    def test_routes_and_lanes(self):
        import app as A
        self.assertIn("sitefix_apply", A.SITE_STEPS)
        self.assertNotIn("sitefix", A.SITE_STEPS)                            # previews run in the crawl lane
        for step in ("publish", "all", "status", "images"):                 # a fix run is never published as new
            self.assertIn("اصلاح سایت", A.start_job(step, "fix-photo-20261027-1000", {}))


if __name__ == "__main__":
    unittest.main()
