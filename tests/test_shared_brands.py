"""(26 Nov) The site's brands are read once per 15 minutes for every process on the machine, not per batch."""
import os
import time

from crawler.publish import _shared_brands
from crawler.wc import WC


class FakeWC(WC):
    def __init__(self):
        super().__init__()
        self.base = "https://shop.test"
        self.reads = 0

    def _list_brands_site(self):
        self.reads += 1
        return [{"id": 1, "name": "Bosch"}]


def test_read_once_then_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WC_BRANDS_CACHE_DIR", str(tmp_path))
    a, b = FakeWC(), FakeWC()
    assert _shared_brands(a) == [{"id": 1, "name": "Bosch"}]
    assert _shared_brands(b) == [{"id": 1, "name": "Bosch"}]       # another process: from the file
    assert (a.reads, b.reads) == (1, 0)


def test_made_brand_added_and_old_list_read_again(tmp_path, monkeypatch):
    monkeypatch.setenv("WC_BRANDS_CACHE_DIR", str(tmp_path))
    a, b = FakeWC(), FakeWC()
    a.list_brands(max_age=900)
    a.remember_brand({"id": 2, "name": "Makita"})
    assert [t["id"] for t in b.list_brands(max_age=900)] == [1, 2]
    assert b.reads == 0
    f = a._brands_file()
    old = time.time() - 1000
    os.utime(f, (old, old))
    assert [t["id"] for t in b.list_brands(max_age=900)] == [1]     # older than 15 min: the site again
    assert b.reads == 1


def test_old_list_used_while_another_process_reads(tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setenv("WC_BRANDS_CACHE_DIR", str(tmp_path))
    a, b = FakeWC(), FakeWC()
    a.list_brands(max_age=900)
    f = a._brands_file()
    old = time.time() - 1000
    os.utime(f, (old, old))
    with open(f.with_suffix(".lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)                                  # someone is reading the site now
        assert b.list_brands(max_age=900) == [{"id": 1, "name": "Bosch"}]
    assert b.reads == 0


def test_no_max_age_reads_the_site(tmp_path, monkeypatch):
    monkeypatch.setenv("WC_BRANDS_CACHE_DIR", str(tmp_path))
    a = FakeWC()
    a.list_brands(max_age=900)
    a.list_brands()
    assert a.reads == 2


def test_brand_names_skip_plain_words(tmp_path, monkeypatch):
    """(28 Nov) Plain words in the site's brand table (سیم، ریز) are not brands; a real one stays."""
    import pandas as pd
    from crawler import pipeline as P
    tx = tmp_path / "tax.csv"
    rows = ["ID,Level 1,Level 2,Full Breadcrumb Path"]
    for i, g in enumerate(["الف", "ب", "ج"]):
        rows += [f"{i}1,{g},x,{g} > x > سیم ریز {i}"]
    rows += ["99,د,x,د > x > ترانسمیتر اندرس هاوزر"]
    tx.write_text("\n".join(rows), encoding="utf-8")
    P._COMMON_WORDS.clear()
    monkeypatch.setattr("crawler.brands_fa.load", lambda cfg: pd.DataFrame({"name_fa": ["سیم", "ریز", "اندرس هاوزر"]}))
    names = P._brand_names({"taxonomy": {"file": str(tx)}}, pd.DataFrame())
    P._COMMON_WORDS.clear()
    assert "اندرس هاوزر" in names and "سیم" not in names and "ریز" not in names
