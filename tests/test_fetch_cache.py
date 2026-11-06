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
