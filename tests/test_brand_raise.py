from unittest import mock


class FakeWC:
    def __init__(self, prods):
        self.prods = {p["id"]: p for p in prods}
        self.sent = []

    def list_brands(self):
        return [{"id": 5, "name": "رونیکس (Ronix)", "slug": "ronix"}]

    def _call(self, fn, what):
        return fn()

    def _wc(self, method, path, params=None):
        ids = [int(x) for x in params["include"].split(",")]
        return [self.prods[i] for i in ids]

    def batch_update(self, items):
        self.sent += items
        return {"update": items}


def test_raise_and_keep(tmp_path):
    from crawler import brandtools as BT
    prods = [{"id": 1, "name": "دریل رونیکس 2210", "type": "simple", "brands": [{"id": 5}], "regular_price": "10",
              "sale_price": "", "stock_status": "instock", "meta_data": []},
             {"id": 2, "name": "اینورتر جوشکاری رونیکس", "type": "simple", "brands": [{"id": 5}], "regular_price": "20",
              "sale_price": "", "stock_status": "instock",
              "meta_data": [{"key": "_caravan_price_lock", "value": "pl-x"}, {"key": "_caravan_price_toman", "value": "5400000"}]},
             {"id": 3, "name": "پیچ گوشتی رونیکس", "type": "variable", "brands": [{"id": 5}], "regular_price": "",
              "stock_status": "instock", "meta_data": []}]
    wc = FakeWC(prods)
    cfg = {"_data_dir": str(tmp_path)}
    with mock.patch("crawler.pricelist.site_cache", return_value=prods), mock.patch("crawler.pricelist.lock_brand"):
        r = BT.raise_prices(cfg, wc, "رونیکس", 22, go=False)
        assert r["to_change"] == 2 and r["locked_in"] == 1 and r["variable_left"] == 1 and not wc.sent
        r = BT.raise_prices(cfg, wc, "رونیکس", 22, go=True)
    assert {x["id"]: x["regular_price"] for x in wc.sent} == {1: "12.20", 2: "24.40"}
    toman = [m["value"] for x in wc.sent if x["id"] == 2 for m in x["meta_data"] if m["key"] == "_caravan_price_toman"]
    assert toman == ["6588000"]
    with mock.patch("crawler.pricelist.site_cache", return_value=prods):
        r = BT.out_of_stock(cfg, wc, ["رونیکس"], keep="اینورتر")
    assert r["brands"][0]["to_change"] == 1                      # the drill only; inverter kept, variable left
