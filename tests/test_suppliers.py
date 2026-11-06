from crawler import suppliers as S


def test_kind_and_phones():
    assert S.kind_of("نمایندگی رسمی بوش در ایران", "https://boschiran.com") == "official"
    assert S.kind_of("پخش عمده ابزار ورکس", "https://x.ir") == "wholesale"
    assert S.kind_of("لیست قیمت", "https://x.ir/list.pdf") == "price_list"
    assert S.kind_of("کانال", "https://t.me/worx_ir") == "channel"
    assert S._phones("تلفن: ۰۲۱-۶۶۷۱۲۳۴۵ و ۰۹۱۲۱۲۳۴۵۶۷") == ["02166712345", "09121234567"]
