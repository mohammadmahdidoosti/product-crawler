from unittest import mock

PAGE = '''<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="baradabzartehran/101">
<a class="tgme_widget_message_photo_wrap" style="width:800px;background-image:url('https://cdn.example/p1.jpg')"></a>
<div class="tgme_widget_message_text js-message_text" dir="auto">اینورتر زوبر 200 آمپر<br/>قیمت 12,500,000 تومان</div>
<time datetime="2026-11-05T10:00:00+00:00" class="time">10:00</time></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="baradabzartehran/102">
<div class="tgme_widget_message_text js-message_text" dir="auto">سلام دوستان</div>
<time datetime="2026-11-06T10:00:00+00:00" class="time">10:00</time></div></div>'''


def test_channel_photos_and_priced_texts(tmp_path):
    from crawler import pricelist as P
    added = []

    def resp(url, timeout=60):
        m = mock.Mock(status_code=200)
        m.text = PAGE if "/s/" in url and "before" not in url else ""
        m.content = b"img"
        return m
    with mock.patch("requests.Session.get", side_effect=resp), \
            mock.patch.object(P, "add_file", side_effect=lambda c, r, d, n: added.append((n, d)) or {"files": 1}), \
            mock.patch.object(P, "settings", return_value={"telegram_posts": 50, "max_files": 80}), \
            mock.patch.object(P, "run_path", return_value=tmp_path):
        res = P.telegram_channel({}, "pl-x", "baradabzartehran")
    assert res["posts"] == 2 and res["photos"] == 1 and res["texts"] == 1
    names = [n for n, _ in added]
    assert "tg-baradabzartehran-101-2026-11-05.jpg" in names and "tg-baradabzartehran.txt" in names
    txt = dict(added)["tg-baradabzartehran.txt"].decode()
    assert "12,500,000" in txt and "سلام" not in txt


def test_a_photo_of_another_maker_is_not_used():
    from crawler.pricelist import other_maker
    assert other_maker("ARVA", {"پوکا", "pukka", "poka"})
    assert not other_maker("PUKKA", {"پوکا", "pukka"})
    assert not other_maker("", {"پوکا"})
