"""(18 Nov) No paid photo edit at publishing: a product whose photo still needs it is held back, unless picked by SKU."""
import pandas as pd
from crawler import publish


def _setup(tmp_path, monkeypatch, calls):
    pd.DataFrame([{"url": "u1", "notes": "paid_fix_pending", "files": "a.webp"},
                  {"url": "u2", "notes": "", "files": "b.webp"}]).to_csv(tmp_path / "05_images.csv", index=False)
    items = [({"url": "u1"}, {"SKU": "S1", "Name": "n1"}), ({"url": "u2"}, {"SKU": "S2", "Name": "n2"})]
    import crawler.pipeline as P
    import crawler.aiphotos as A
    monkeypatch.setattr(P, "export_rows", lambda cfg, run: items)
    monkeypatch.setattr(A, "fix_photos", lambda cfg, run, paid=True, only=None: calls.append((paid, sorted(only or []))))
    return items


def test_held_when_paid_fix_off(tmp_path, monkeypatch):
    calls = []
    items = _setup(tmp_path, monkeypatch, calls)
    cfg = {"images": {"paid_fix_at_publish": False, "defer_check": True}}
    out = publish._paid_photo_fixes(cfg, "r", tmp_path, items)
    assert [r["SKU"] for _, r in out] == ["S2"]
    assert calls == [(False, ["u1", "u2"])]                    # the deferred check, no paid edit
    held = pd.read_csv(tmp_path / publish.PHOTO_HELD)
    assert list(held["sku"]) == ["S1"]


def test_explicit_sku_still_fixed(tmp_path, monkeypatch):
    calls = []
    items = _setup(tmp_path, monkeypatch, calls)
    cfg = {"images": {"paid_fix_at_publish": False}}
    out = publish._paid_photo_fixes(cfg, "r", tmp_path, items, explicit=True)
    assert [r["SKU"] for _, r in out] == ["S1", "S2"]
    assert calls == [(True, ["u1"])]
