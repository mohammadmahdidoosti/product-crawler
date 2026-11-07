import pandas as pd


def test_the_name_brand_wins_over_a_conflicting_page_brand():
    from crawler.brandfix import fill_brands
    known = [("رابین", "رابین"), ("راکسر", "راکسر"), ("رابین", "rabin")]
    df = pd.DataFrame([{"name": "دستگاه پولیش روتاری راکسر 1400 وات مدل 7200", "brand": "برند رابین ( Rabin )"},
                       {"name": "دریل رابین 1050 وات مدل R1007", "brand": "برند رابین ( Rabin )"}])
    out = fill_brands(df, known)
    assert list(out["brand"]) == ["راکسر", "برند رابین ( Rabin )"]
