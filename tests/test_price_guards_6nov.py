"""(6 Nov) Orders came in at wrong prices: a crawled chainsaw at a quarter of the market, a price list page
read in the wrong unit, a guessed price sold as in stock."""
import pandas as pd

from crawler import pipeline as P
from crawler import pricelist as PL
from crawler.publish import guessed_price


def peer(url, dom, name, price):
    return {"url": url, "dom": dom, "price": price, "codes": P._codes(name), "name": name,
            "head": (P.norm_text(name).split() or [""])[0]}


def test_same_model_far_below_other_shops_is_held():
    r = {"url": "https://engika.com/p/1", "name": "اره موتوری اگرو AG-CS5200-45", "price_toman": "4600000",
         "breadcrumb": "ابزار > اره"}
    peers = [peer("https://a.ir/1", "a.ir", "اره موتوری اگرو AG-CS5200 50 سانت", 18_800_000),
             peer("https://b.ir/1", "b.ir", "اره موتوری اگرو AG-CS5200", 22_995_000)]
    out = P.price_sanity([r], peers=peers)
    assert str(out[0].get("price_check", "")).startswith("suspicious")


def test_same_model_in_line_passes():
    r = {"url": "https://engika.com/p/1", "name": "اره موتوری اگرو AG-CS5200-45", "price_toman": "20000000",
         "breadcrumb": "ابزار > اره"}
    peers = [peer("https://a.ir/1", "a.ir", "اره موتوری اگرو AG-CS5200", 18_800_000),
             peer("https://b.ir/1", "b.ir", "اره موتوری اگرو AG-CS5200", 22_995_000)]
    assert not str(P.price_sanity([r], peers=peers)[0].get("price_check", "")).startswith("suspicious")


def test_guessed_price():
    assert guessed_price({"price_estimated": "3 products of similar size (w)"})
    assert guessed_price({"price_estimated": "category median of 4"})
    assert not guessed_price({"price_estimated": "same model 9303 in crawl (2)"})
    assert not guessed_price({"price_estimated": "web search for 9303 (3 prices)"})
    assert not guessed_price({"price_estimated": "reviewed"})
    assert not guessed_price({})


def test_market_check_holds_a_page_read_in_the_wrong_unit(tmp_path, monkeypatch):
    cfg = {"_data_dir": tmp_path, "pricelist": {"market_samples": 2}}
    d = tmp_path / "pricelists" / "pl-t"
    d.mkdir(parents=True)
    rows = [
        {"brand": "توسن", "model": "9303", "key": "9303", "name": "فرز مینیاتوری شارژی ۱۲ ولت", "price": "14298000",
         "unit": "rial", "toman": "1429800", "file": "power.pdf ص18"},
        {"brand": "توسن", "model": "9302", "key": "9302", "name": "فرز مینیاتوری شارژی قلمی", "price": "7598000",
         "unit": "rial", "toman": "759800", "file": "power.pdf ص18"},
        {"brand": "توسن", "model": "2720", "key": "2720", "name": "صفحه برش 2720", "price": "15395000",
         "unit": "rial", "toman": "1539500", "file": "power.pdf ص2"},
    ]
    pd.DataFrame(rows, columns=PL.ITEM_COLS).fillna("").to_csv(d / "items.csv", index=False)
    monkeypatch.setattr(PL, "run_path", lambda cfg, run: d)
    market = {"9303": 14_298_000, "9302": 7_598_000, "2720": 1_539_500}
    import crawler.detective as D
    import crawler.pipeline as PP
    import crawler.fx as FX
    monkeypatch.setattr(FX, "today_rate", lambda cfg: {"usd_rate_toman": 270000})
    monkeypatch.setattr(PP, "_fetchers", lambda cfg: {"iran": None})
    monkeypatch.setattr(PP, "price_peers", lambda cfg, d: [])
    monkeypatch.setattr(D, "_search_prices", lambda cfg, f, rec, rate: [
        (s, market[next(k for k in market if k in rec["name"])]) for s in ("a.ir", "b.ir")])
    import crawler.aggregators as AG
    monkeypatch.setattr(AG, "torob_offers", lambda f, rec, same: [])
    res = PL.market_check(cfg, "pl-t")
    it = pd.read_csv(d / "items.csv", dtype=str).fillna("")
    held = dict(zip(it["key"], it["toman"]))
    assert res["pages_held"] == 1
    assert held["9303"] == "" and held["9302"] == ""          # the page read in the wrong unit waits
    assert held["2720"] == "1539500"                          # the page read right stays
