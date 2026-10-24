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
    brands: list = []
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
        if u.path == "/wp-json/wc/v3/products/brands":
            page = int(q.get("page", ["1"])[0])
            return self._json(FakeWoo.brands[(page - 1) * 100: page * 100])
        if u.path == "/wp-json/wc/v3/products" and "brand" in q:
            bid = int(q["brand"][0])
            return self._json([dict(p, brands=[{"id": i} for i in p.get("_b", [])]) for p in FakeWoo.products.values()
                               if bid in p.get("_b", [])])
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
        if u.path == "/wp-json/wc/v3/products/batch":
            d = json.loads(body)
            out = []
            for it in d.get("update", []):
                if it["id"] in FakeWoo.products:
                    FakeWoo.products[it["id"]].update(it)
                    out.append(FakeWoo.products[it["id"]])
                else:
                    out.append({"id": it["id"], "error": {"code": "invalid", "message": "no such product"}})
            return self._json({"update": out})
        if u.path == "/wp-json/wc/v3/products/brands":
            d = json.loads(body)
            t = {"id": 900 + len(FakeWoo.brands), "name": d["name"]}
            FakeWoo.brands.append(t)
            return self._json(t, 201)
        if u.path == "/wp-json/wc/v3/products":
            assert self._auth() == "ck_test:cs_test"
            d = json.loads(body)
            pid = 100 + len(FakeWoo.products)
            if not all(isinstance(b, dict) and "id" in b for b in d.get("brands", [])):   # as the real site
                return self._json({"code": "rest_invalid_param", "message": "brands"}, 400)
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

    def do_DELETE(self):
        u = urlparse(self.path)
        FakeWoo.calls.append(("DELETE", u.path))
        tid = int(u.path.rsplit("/", 1)[1])
        FakeWoo.brands = [t for t in FakeWoo.brands if t["id"] != tid]
        return self._json({"deleted": True})

    def do_PUT(self):
        u = urlparse(self.path)
        d = json.loads(self._body())
        FakeWoo.calls.append(("PUT", u.path))
        if not all(isinstance(b, dict) and "id" in b for b in d.get("brands", [])):
            return self._json({"code": "rest_invalid_param", "message": "brands"}, 400)
        pid = int(u.path.rsplit("/", 1)[1])
        if "brands" in d:
            FakeWoo.products[pid]["_b"] = [x["id"] for x in d["brands"]]
        FakeWoo.products[pid].update({k: v for k, v in d.items() if k != "images"})
        if "images" in d:
            FakeWoo.products[pid]["images"] = d["images"]
        return self._json(FakeWoo.products[pid])


def product(i, tax="14339", extra="", price="1500000"):
    return {"url": f"https://shop{i}.ir/product/{i}/", "name": f"Drive {i}", "name_fa": f"درایو {i}",
            "price_toman": price, "breadcrumb": "برق > درایو", "extra_breadcrumbs": "",
            "taxonomy_id": tax, "extra_taxonomy_ids": extra, "brand": "Nanjing Antu Elevator Parts" if i % 2 else "",
            "brand_source": "supplier" if i % 2 else "", "source_type": "foreign", "sellers": "Nanjing Antu Elevator Parts Co., Ltd.",
            "model": f"M{i}", "specs_json": json.dumps({"توان": "2.2kW"}, ensure_ascii=False),
            "usd_rate_date": "2026-10-20", "usd_rate_toman": "100000", "price_usd": "15" if price else "",
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
        FakeWoo.brands = [{"id": 800, "name": "سناکوه (SKH)"}]
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
        from crawler import brands_fa as BF
        BF.edit(self.cfg, [{"source": "Nanjing Antu Elevator Parts", "kind": "foreign", "en": "Antu", "fa": "آنتو"}])
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
        self.assertEqual(p1["regular_price"], "15")         # dollars: the site multiplies by its rate
        self.assertEqual(p1["name"], "درایو 1 برند آنتو (Antu)")
        self.assertEqual(p2["name"], "درایو 2")              # no brand: name unchanged
        self.assertEqual(p1["attributes"][0]["options"], ["آنتو (Antu)"])
        self.assertEqual(p1["brands"], [{"id": 901}])        # new brand term made on the site
        self.assertEqual(FakeWoo.brands[-1]["name"], "آنتو (Antu)")
        self.assertNotIn("attributes", p2)
        self.assertIn({"key": "_caravan_supplier", "value": "Nanjing Antu Elevator Parts Co., Ltd."}, p1["meta_data"])
        self.assertIn({"key": "_caravan_price_toman", "value": "1500000"}, p1["meta_data"])
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
        self.assertEqual(len(FakeWoo.brands), 2)             # the brand term is reused, not made twice
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

    def test_brand_not_in_table_is_skipped(self):
        from crawler import brands_fa as BF
        BF.path(self.cfg).unlink()
        res = PB.publish(self.cfg, "batch-1", limit=10, dry_run=True)
        self.assertEqual(res["counts"]["checked"], 2)        # products 2 and 4 have no brand at all
        self.assertTrue(any("جدول برندها" in r["error"] for r in res["rows"]))

    def test_publish_all(self):
        PB.publish(self.cfg, "batch-1", limit=10)
        self.assertEqual({p["status"] for p in FakeWoo.products.values()}, {"draft"})
        res = PB.set_status(self.cfg, "batch-1", "publish")
        self.assertEqual(res["changed"], 3)
        self.assertEqual({p["status"] for p in FakeWoo.products.values()}, {"publish"})
        self.assertEqual(set(read_csv(self.d / PB.RESULT_FILE)["status"]), {"publish", ""})   # '' = the skipped one

    def test_merge_site_brands(self):
        FakeWoo.brands = [{"id": 1583, "name": "BSPOOL (بی اس پول)"}, {"id": 1584, "name": "BSPOOL(بی اس پول)"}]
        FakeWoo.products = {1: {"id": 1, "sku": "a", "name": "x", "status": "publish", "_b": [1583]},
                            2: {"id": 2, "sku": "b", "name": "y", "status": "publish", "_b": [1584]},
                            3: {"id": 3, "sku": "c", "name": "z", "status": "draft", "_b": [1583, 77]}}
        w = WCM.WC()
        rep = w.merge_brands(1583, 1584)                     # dry run by default: nothing changes
        self.assertEqual([p["id"] for p in rep["products"]], [1, 3])
        self.assertFalse(rep["deleted"])
        self.assertEqual(len(FakeWoo.brands), 2)
        rep = w.merge_brands(1583, 1584, dry_run=False)
        self.assertTrue(rep["deleted"])
        self.assertEqual([t["id"] for t in FakeWoo.brands], [1584])
        self.assertEqual(FakeWoo.products[1]["_b"], [1584])
        self.assertEqual(FakeWoo.products[3]["_b"], [77, 1584])    # other brands of the product kept
        self.assertEqual(FakeWoo.products[2]["_b"], [1584])

    def test_category_map_not_under_data(self):
        self.assertEqual(WCM.category_map().get("14339"), "1635")   # from assets/, DATA_DIR has none


if __name__ == "__main__":
    unittest.main()


class Brands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config()
        self.cfg["_data_dir"] = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def recs(self):
        return [
            {"brand": "Nanjing Antu Elevator Parts", "brand_source": "supplier", "source_type": "foreign",
             "sellers": "Nanjing Antu Elevator Parts Co., Ltd.", "name": "Float switch"},
            {"brand": "FUEN", "brand_source": "page", "source_type": "foreign", "sellers": "Fuen Co.", "name": "x"},
            {"brand": "FUEN", "brand_source": "page", "source_type": "foreign", "sellers": "Fuen Co.", "name": "y"},
            {"brand": "رونیکس", "brand_source": "page", "source_type": "iran", "sellers": "ابزار آرین", "name": "دریل"},
            {"brand": "ابزار آرین", "brand_source": "supplier", "source_type": "iran", "sellers": "ابزار آرین", "name": "z"},
            {"brand": "سناکوه (SKH)", "brand_source": "page", "source_type": "iran", "sellers": "s", "name": "w"},
            {"brand": "kalayefani.com", "brand_source": "page", "source_type": "iran", "sellers": "کالای فنی", "name": "v"},
        ]

    def test_build_and_apply(self):
        from crawler import brands_fa as BF
        asked = []

        def fake_ask(self_, items):
            asked.extend(items)
            m = {"Nanjing Antu Elevator Parts": ("Antu", "آنتو"), "FUEN": ("FUEN", "فوئن")}
            return {it["i"]: {"en": m[it["brand"]][0], "fa": m[it["brand"]][1]} for it in items}

        with mock.patch.object(BF.Namer, "ask", fake_ask):
            res = BF.build(self.cfg, self.recs())
        self.assertEqual(res["brands"], 6)
        self.assertEqual(sorted(a["brand"] for a in asked), ["FUEN", "Nanjing Antu Elevator Parts"])
        self.assertTrue(next(a for a in asked if a["brand"].startswith("Nanjing"))["company"])
        t = BF.table(self.cfg)
        self.assertEqual(t[BF.key("Nanjing Antu Elevator Parts")]["display"], "آنتو (Antu)")
        self.assertIn("Co., Ltd.", t[BF.key("Nanjing Antu Elevator Parts")]["suppliers"])   # who it really is
        self.assertEqual(t[BF.key("FUEN")]["products"], "2")
        self.assertEqual(t[BF.key("رونیکس")]["display"], "رونیکس")                      # Persian: as it is
        self.assertEqual(t[BF.key("ابزار آرین")]["display"], "")                           # a shop is no brand
        self.assertEqual(t[BF.key("سناکوه (SKH)")]["display"], "سناکوه (SKH)")
        self.assertEqual(t[BF.key("kalayefani.com")]["kind"], "shop")                     # a website is no brand
        self.assertEqual(t[BF.key("kalayefani.com")]["display"], "")
        self.assertNotIn("kalayefani.com", [x["brand"] for x in asked])
        # a website the table already holds as a 'brand' (made before this rule) is turned into a shop
        df = BF.load(self.cfg)
        i = df.index[df["source"] == "kalayefani.com"][0]
        df.loc[i, ["kind", "en", "fa", "by"]] = ["foreign", "Kalayefani", "کالایفانی", "deepseek"]
        BF.save(self.cfg, df)
        with mock.patch.object(BF.Namer, "ask", fake_ask):
            BF.build(self.cfg, self.recs())
        self.assertEqual(BF.table(self.cfg)[BF.key("kalayefani.com")]["display"], "")
        from crawler.brandfix import clean_brand
        self.assertEqual(clean_brand("kalayefani.com"), "")
        self.assertEqual(clean_brand("FUEN"), "FUEN")

        br = BF.resolve(self.recs()[0], t)
        self.assertEqual(BF.name_with_brand("فلوتر آسانسوری یک اینچ", br), "فلوتر آسانسوری یک اینچ برند آنتو (Antu)")
        self.assertEqual(BF.name_with_brand("فلوتر Antu یک اینچ", br), "فلوتر Antu یک اینچ")
        self.assertEqual(BF.name_with_brand("فلوتر آنتو", br), "فلوتر آنتو")
        self.assertEqual(BF.name_with_brand("دریل", BF.resolve(self.recs()[4], t)), "دریل")

        # a hand edit survives a rebuild, even with redo
        BF.edit(self.cfg, [{"source": "FUEN", "en": "Fuen", "fa": "فوئِن"}])
        with mock.patch.object(BF.Namer, "ask", fake_ask):
            BF.build(self.cfg, self.recs(), redo=True)
        self.assertEqual(BF.table(self.cfg)[BF.key("FUEN")]["display"], "فوئِن (Fuen)")

    def test_one_name_per_brand(self):
        from crawler import brands_fa as BF
        rows = [
            {"source": "RONIX", "kind": "foreign", "en": "Ronix", "fa": "رونیکس", "by": "deepseek", "products": "1"},
            {"source": "RONIX – رونیکس", "kind": "foreign", "en": "Ronix", "fa": "رونیکس", "by": "rule", "products": "6"},
            {"source": "رونیکس", "kind": "iranian", "fa": "رونیکس", "by": "rule", "products": "19"},
            {"source": "اره رونیکس", "kind": "iranian", "fa": "اره رونیکس", "by": "rule", "products": "5"},
            {"source": "بوش", "kind": "iranian", "fa": "بوش", "by": "rule", "products": "9"},
            {"source": "BOSCH", "kind": "foreign", "en": "Bosch", "fa": "بوش", "by": "deepseek", "products": "2"},
            {"source": "NOVA", "kind": "foreign", "en": "NOVA", "fa": "نوا", "by": "deepseek", "products": "1"},
            {"source": "Nova Elevator Parts", "kind": "foreign", "en": "Nova", "fa": "نوا", "by": "deepseek", "products": "4"},
            {"source": "SKH", "kind": "foreign", "en": "SKH", "fa": "اس‌کا‌اچ", "by": "deepseek", "products": "1"},
            {"source": "سناکوه (SKH)", "kind": "foreign", "en": "SKH", "fa": "سناکوه", "by": "rule", "products": "1",
             "site": "سناکوه (SKH)"},
            {"source": "تمامی محصولات", "kind": "iranian", "fa": "تمامی محصولات", "by": "rule", "products": "1"},
            {"source": "موتور گیرلس بهران", "kind": "iranian", "fa": "موتور گیرلس بهران", "by": "rule", "products": "1"},
            {"source": "مونتاناری", "kind": "iranian", "fa": "مونتاناری", "by": "rule", "products": "4"},
            {"source": "Mega-Tek", "kind": "foreign", "en": "Mega-Tek", "fa": "مگاتک", "by": "manual", "products": "1",
             "updated": "2026-10-24 12:00"},
            {"source": "Mega-Tek Instrument (Chengdu)", "kind": "foreign", "en": "Megatek", "fa": "مگاتک",
             "by": "deepseek", "products": "2"},
        ]
        out = BF.normalize(pd.DataFrame(rows))
        d = dict(zip(out["source"], out["display"]))
        for s in ("RONIX", "RONIX – رونیکس", "رونیکس", "اره رونیکس"):
            self.assertEqual(d[s], "رونیکس")                    # Iranian brand (brands.csv): Persian only
        self.assertEqual(d["بوش"], "بوش (Bosch)")
        self.assertEqual(d["BOSCH"], "بوش (Bosch)")
        self.assertEqual(d["NOVA"], d["Nova Elevator Parts"])
        self.assertEqual(d["Nova Elevator Parts"], "نوا (Nova)")
        self.assertEqual(d["SKH"], "سناکوه (SKH)")              # the name the site already uses
        self.assertEqual(d["تمامی محصولات"], "")
        self.assertEqual(d["موتور گیرلس بهران"], "بهران")
        self.assertEqual(d["مونتاناری"], "مونتاناری (Montanari)")
        self.assertEqual(d["Mega-Tek Instrument (Chengdu)"], "مگاتک (Mega-Tek)")   # a hand edit names its group

    def test_site_match(self):
        from crawler import brands_fa as BF
        terms = [{"id": 1, "name": "سناکوه (SKH)"}, {"id": 2, "name": "Makita"}, {"id": 3, "name": "ماکیتا"}]
        self.assertEqual(BF.match_site(terms, "سناکوه", "SKH", "سناکوه (SKH)")["id"], 1)
        self.assertEqual(BF.match_site(terms, "ماکیتا", "Makita", "ماکیتا (Makita)")["id"], 2)
        self.assertIsNone(BF.match_site(terms, "آنتو", "Antu", "آنتو (Antu)"))
        self.assertEqual(BF.split_display("Mega-Tek Instrument (Chengdu)"), ("", "Mega-Tek Instrument (Chengdu)"))
        self.assertEqual(BF.split_display("فوئن (FUEN)"), ("فوئن", "FUEN"))
        self.assertEqual(BF.split_display("رونیکس"), ("رونیکس", ""))


class Names(unittest.TestCase):
    def test_clean_name(self):
        from crawler.names import clean_name
        cases = {
            "پک 10 کاره ماکیتا مدل DCG405 (198 ولت) شارژی ا Pack of 10 pieces Makita DCG405 (( اقساط یک ساله ))":
                "پک 10 کاره ماکیتا مدل DCG405 (198 ولت) شارژی",
            "قیمت وخرید اره باغبانی 20 سانتی متری شارژی براشلس توسن مدل 9560 - ابزار توسن":
                "اره باغبانی 20 سانتی متری شارژی براشلس توسن مدل 9560",
            "خرید چکی اره زنجیری 30 سانتی براشلس ویوارکس مدل VR2412V-CS با چک صیادی در کرج و تهران":
                "اره زنجیری 30 سانتی براشلس ویوارکس مدل VR2412V-CS",
            "موتور آسانسور گیرلس بهران 10 نفره { قیمت + سرعت های 1 و 1.6 }":
                "موتور آسانسور گیرلس بهران 10 نفره (سرعت های 1 و 1.6)",
            "موتور آسانسور بهران 5.5 کیلووات { قیمت و موجودی روز }": "موتور آسانسور بهران 5.5 کیلووات",
            "اره عمودبر بوش مدل GST 65 BE فروشگاه اینترنتی سیمین ابزار": "اره عمودبر بوش مدل GST 65 BE",
            "خرید و قیمت اره عمودبر بوش مدل GST 8000 E اصل آلمان": "اره عمودبر بوش مدل GST 8000 E",
            "موتور گیرلس آسانسور ویتور قیمت": "موتور گیرلس آسانسور ویتور",
            "قیمت رسیور کرکره برقی ساید پاور POWER + مشخصات کامل": "رسیور کرکره برقی ساید پاور POWER",
            # what the product is stays, including that it is not original
            "قیمت و مشخصات اره موتوری اشتیل مدل MS 381 (های کپی)": "اره موتوری اشتیل مدل MS 381 (های کپی)",
            "فلوتر آسانسوری یک اینچ، پلاستیک": "فلوتر آسانسوری یک اینچ، پلاستیک",
            "موتور مونو درایو 16 نفره سرعت 1 متر بر ثانیه { دیسکی }": "موتور مونو درایو 16 نفره سرعت 1 متر بر ثانیه (دیسکی)",
        }
        for raw, want in cases.items():
            self.assertEqual(clean_name(raw, ["ابزار توسن"]), want, raw)

    def test_brand_from_name(self):
        from crawler import brands_fa as BF
        t = {BF.key("mato drive"): {"source": "mato drive", "kind": "foreign", "fa": "ماتو درایو", "en": "Mato Drive",
                                     "display": "ماتو درایو (Mato Drive)"},
             BF.key("ABB"): {"source": "ABB", "kind": "foreign", "fa": "ای‌بی‌بی", "en": "ABB", "display": "ای‌بی‌بی (ABB)"},
             BF.key("x.com"): {"source": "x.com", "kind": "shop", "fa": "", "en": "", "display": ""}}
        self.assertEqual(BF.from_name("موتور آسانسور گیرلس دیسکی ماتودرایو16 نفره", t)["display"], "ماتو درایو (Mato Drive)")
        self.assertEqual(BF.from_name("موتور گیربکس abb", t)["display"], "ای‌بی‌بی (ABB)")
        self.assertIsNone(BF.from_name("موتور گیربکس cabbage", t))         # whole words only
        self.assertEqual(BF.from_name("اره برقی بوش مدل X", t)["display"], "بوش (Bosch)")   # brands.csv
        n = "موتور آسانسور گیرلس دیسکی ماتودرایو16 نفره"
        self.assertEqual(BF.name_with_brand(n, BF.from_name(n, t)), n)        # not 'برند ماتو درایو' again
        br = {"status": "ok", "fa": "ماتو درایو", "en": "Mato Drive", "display": "ماتو درایو (Mato Drive)", "source": "x"}
        self.assertEqual(BF.name_with_brand(n, br), n)
