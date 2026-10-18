import unittest

from crawler.extract import extract_page, resolve_price_unit

RELATED = """<html><head><title>x</title></head><body>
<div class="product type-product"><div class="summary"><h1 class="product_title">اره شارژی اشتیل GTA26</h1>
<p class="stock out-of-stock">ناموجود</p></div></div>
<section class="related-box"><div class="swiper-slide"><div class="product-detail-area">
<span class="price"><span class="woocommerce-Price-amount amount"><bdi>۲۶۳.۰۰۰ <span>تومان</span></bdi></span></span>
<h3 class="product-name"><a href="/product/gloves/">دستکش</a></h3></div></div></section></body></html>"""

JSONLD_RIAL = """<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",
"name":"اره شارژی اشتیل GTA 43","offers":{"@type":"Offer","price":"69000000","priceCurrency":"IRR"}}</script></head>
<body><div class="elementor-widget elementor-widget-woocommerce-product-title"><h1 class="product_title">اره شارژی اشتیل GTA 43</h1></div>
<div class="elementor-widget elementor-widget-wd_single_product_price"><div class="elementor-widget-container">
<p class="price"><span class="woocommerce-Price-amount amount">6,900,000 <span class="woocommerce-Price-currencySymbol">تومان</span></span></p>
</div></div></body></html>"""


class PriceBox(unittest.TestCase):
    def test_related_product_price_is_not_taken(self):
        rec = extract_page(RELATED)
        assert rec is None or not rec.get("price_amount")


    def test_jsonld_rial_vs_visible_toman(self):
        rec = extract_page(JSONLD_RIAL)
        unit, _ = resolve_price_unit(rec)
        amt = rec["price_amount"] * (1 if unit == "toman" else 0.1)
        assert abs(amt - 6_900_000) < 1
