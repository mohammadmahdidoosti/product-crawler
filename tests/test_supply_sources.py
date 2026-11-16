from crawler import supply_sources as S

SEEDIA = """<html><body><nav>خانه | اعضاء | تماس با ما ۰۲۱-۸۸۸۸۸۸۸۸</nav>
<div class="acc"><div class="acc-title">ملی شیمی کشاورز</div><div class="acc-body" style="display:none">
<p>نام شرکت : ملی شیمی کشاورز</p><p>شماره ثابت : ۴۴۶۲۸۰۵۵</p><p>ایمیل: info@mshimi.com</p></div></div>
<div class="acc"><div class="acc-title">یاس اندیشان سریر سپهر</div><div class="acc-body">
<p>نام شرکت : یاس اندیشان سریر سپهر</p><p>مدیر عامل : مازیار</p><p>شماره ثابت : ۰۲۱-۴۴۶۲۹۷۶۱</p>
<p>ایمیل: ma_nooshin@yahoo.com</p></div></div></body></html>"""


def test_contact_blocks_reads_accordion_members():
    b = S.contact_blocks(SEEDIA)
    names = [x["name"] for x in b]
    assert "ملی شیمی کشاورز" in names and "یاس اندیشان سریر سپهر" in names
    y = next(x for x in b if x["name"] == "یاس اندیشان سریر سپهر")
    assert y["phones"] == ["02144629761"] and y["emails"] == ["ma_nooshin@yahoo.com"]
    assert not any("88888888" in "".join(x["phones"]) for x in b)          # the nav bar is not a member


def test_broader_and_mentions():
    assert S.broader("بذر هویج") == ["بذر"]
    assert S.broader("کودپاش") == []
    assert S.mentions("فروش انواع بذر سبزیجات", "بذر هویج")
    assert not S.mentions("کود شیمیایی", "بذر هویج")


def test_channel_filter_drops_news():
    assert S.channel_ok("مبین کشت (توزیع بذر، کود و سم)", "")
    assert S.channel_ok("فروش انواع بذرهای وارداتی", "")
    assert not S.channel_ok("صدای بناب", "فروش بذر")
    assert not S.channel_ok("پایگاه خبری اسونیا", "قیمت کودپاش")
    assert not S.channel_ok("گلخانه مجازی", "سلام دوستان")          # no selling word at all


def test_channel_of():
    assert S._channel_of("https://t.me/s/mobinkesht/123") == ("t.me", "mobinkesht")
    assert S._channel_of("https://t.me/mobinkesht") == ("t.me", "mobinkesht")
    assert S._channel_of("https://eitaa.com/shokhmiran") == ("eitaa.com", "shokhmiran")
    assert S._channel_of("https://t.me/joinchat/abc") == ("", "")
    assert S._channel_of("https://example.com/a") == ("", "")


def test_parse_import_iranfair_cards():
    data = {"host": "calendar.iranfair.com", "rows": [
        {"url": "https://calendar.iranfair.com/fa/companies/profile/1",
         "text": "آرکا بذر ایرانیان\nتولید کننده انواع بذر های سبزیجات\narkabazriranin@gmail.com\narkabazriranian.com\n4134220928\nنمایش پروفایل"},
        {"url": "https://calendar.iranfair.com/fa/companies/profile/2",
         "text": "تعاونی گل بذر محلات\nتولید انواع بذر گلهای زینتی\n-\n086-43224004\nنمایش پروفایل"}]}
    r = S.parse_import(data)
    assert [x["source"] for x in r] == ["iranfair", "iranfair"]
    assert r[0]["name"] == "آرکا بذر ایرانیان" and r[0]["site"] == "arkabazriranian.com"
    assert r[0]["emails"] == ["arkabazriranin@gmail.com"]
    assert r[1]["phones"] == ["08643224004"]


def test_parse_import_messenger_text():
    data = {"host": "web.bale.ai", "text": "جستجوی سراسری\nMORAYCO / تولید و واردات سمپاش و کودپاش\n۱۱ عضو | morayco@\n"
                                          "اخبار کشاورزی\n۲۰۰ عضو | @akhbarkeshavarzi"}
    r = S.parse_import(data)
    assert [x["channel"] for x in r] == ["@morayco"]                 # the news channel is dropped
    assert "سمپاش" in r[0]["name"] and r[0]["source"] == "bale"


def test_merge_joins_by_phone_and_channel():
    rows = [S.row("آرکا بذر", "iranfair", phones=["04134220928"], score=3),
            S.row("Arka Seed", "google", phones=["04134220928"], site="arkabazriranian.com", score=1),
            S.row("مبین کشت", "telegram", kind="channel", channel="https://t.me/mobinkesht", score=5),
            S.row("مبین کشت", "bale", kind="channel", channel="@mobinkesht", score=2),
            S.row("یک فروشگاه", "maps", phones=["02112345678"])]
    m = S.merge(rows)
    assert len(m) == 3
    arka = next(x for x in m if "04134220928" in x["phones"])
    assert arka["name"] == "آرکا بذر" and arka["site"] == "arkabazriranian.com"
    assert set(arka["sources"]) == {"iranfair", "google"}
    mob = next(x for x in m if x["channel"])
    assert set(mob["sources"]) == {"telegram", "bale"}
    assert m[-1]["name"] == "یک فروشگاه"                              # one source, low score: last


def test_merge_verdict_first():
    rows = [S.row("الف", "maps", phones=["02111111111"]), S.row("ب", "maps", phones=["02122222222"])]
    m = S.merge(rows, {"02122222222": {"verdict": "good"}})
    assert m[0]["name"] == "ب" and m[0]["verdict"] == "good"


def test_google_queries_are_product_queries():
    qs = [q for _, q in S.google_queries("کودپاش")]
    assert all("کودپاش" in q for q in qs) and any("تولید کننده" in q for q in qs)


def test_find_with_no_sources_only_merges_imports(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(S, "SOURCES", {"google": lambda c, f, k: called.append(k) or {"rows": [], "searches": 1}})
    import crawler.pipeline as P
    monkeypatch.setattr(P, "_fetchers", lambda cfg: {"iran": None})
    cfg = {"_data_dir": tmp_path}
    S.save_import(cfg, "بذر", {"host": "calendar.iranfair.com", "rows": [{"url": "u", "text": "بذرینو\nبذر و پیاز گل\n۰۷۱-۳۸۲۶۲۶۶۶"}]})
    out = S.find(cfg, "بذر", [])
    assert called == [] and [x["name"] for x in out["merged"]] == ["بذرینو"]
    out = S.find(cfg, "بذر", None)
    assert called == ["بذر"] and len(out["merged"]) == 1
