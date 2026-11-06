from crawler import suppliers as S


def test_kind_and_phones():
    assert S.kind_of("نمایندگی رسمی بوش در ایران", "https://boschiran.com") == "official"
    assert S.kind_of("پخش عمده ابزار ورکس", "https://x.ir") == "wholesale"
    assert S.kind_of("لیست قیمت", "https://x.ir/list.pdf") == "price_list"
    assert S.kind_of("کانال", "https://t.me/worx_ir") == "channel"
    assert S._phones("تلفن: ۰۲۱-۶۶۷۱۲۳۴۵ و ۰۹۱۲۱۲۳۴۵۶۷") == ["02166712345", "09121234567"]


def test_context_goes_into_queries_and_offtopic_words():
    from crawler.suppliers import OFFTOPIC, queries
    assert all("سمپاش لیفان" in q for _, q in queries("لیفان", "سمپاش"))
    assert OFFTOPIC.search("بهترین تعمیرگاه های لیفان در تهران")
    assert OFFTOPIC.search("خدمات پس از فروش بوش")
    assert not OFFTOPIC.search("نمایندگی فروش سمپاش لیفان")


def test_brand_as_whole_word():
    from crawler.suppliers import names_brand
    assert not names_brand("نمایندگی فروش ویوارکس vivarex", "https://abzarmousavi.ir/x", ["ورکس", "Worx"])
    assert names_brand("دریل شارژی ورکس مدل", "https://a.ir", ["ورکس", "Worx"])
    assert names_brand("خرید", "https://shop.ir/worx-drill", ["ورکس", "Worx"])
