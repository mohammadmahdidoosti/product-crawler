from pathlib import Path
from crawler.fetch import Fetcher


def test_broken_cache_entry_is_a_miss(tmp_path):
    f = Fetcher({"_data_dir": tmp_path, "http": {}})
    f.cache_dir = tmp_path / "cache"
    f._store("http://x/a", {"url": "http://x/a", "status": 200, "text": "ok"})
    assert f._cached("http://x/a")["text"] == "ok"
    f._cache_path("http://x/a").write_text("", encoding="utf-8")      # half-written by another crawl
    assert f._cached("http://x/a") is None
    assert not list((tmp_path / "cache").rglob("*.tmp"))


def test_pages_are_gzipped_and_old_plain_ones_still_read(tmp_path):
    import json, time, os
    from crawler.fetch import prune_cache
    f = Fetcher({"_data_dir": tmp_path, "http": {}})
    f.cache_dir = tmp_path / "cache"
    f._store("http://x/b", {"url": "http://x/b", "status": 200, "text": "صفحه " * 200})
    p = f._cache_path("http://x/b")
    assert p.name.endswith(".json.gz") and p.stat().st_size < 400
    assert f._cached("http://x/b")["text"].startswith("صفحه")
    old = f._cache_path("http://x/c").with_suffix("")             # saved before gzip
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps({"url": "http://x/c", "status": 200, "text": "قدیمی"}), encoding="utf-8")
    assert f._cached("http://x/c")["text"] == "قدیمی"
    os.utime(old, (time.time() - 5 * 86400,) * 2)
    r = prune_cache({"_data_dir": tmp_path, "http": {"cache_days": 3}})
    assert r["files"] == 1 and not old.exists() and p.exists()


def test_robots_longest_match_like_google():
    from crawler.fetch import robots_rules, robots_allows
    txt = "User-agent: *\nDisallow: /\nAllow: /v4/base-product/search\nAllow: /v4/base-product/details\n"
    r = robots_rules(txt, "CaravanSanatBot/0.1")
    assert robots_allows(r, "https://api.torob.com/v4/base-product/search/?q=x")
    assert robots_allows(r, "https://api.torob.com/v4/base-product/details/?prk=1")
    assert not robots_allows(r, "https://api.torob.com/v4/user/me")
    assert robots_allows(robots_rules("User-agent: *\nDisallow:\n", "x"), "https://a.ir/p")
    assert not robots_allows(robots_rules("User-agent: *\nDisallow: /cart\n", "x"), "https://a.ir/cart?x=1")
