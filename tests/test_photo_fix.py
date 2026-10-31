import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ["CARAVAN_NO_MODELS"] = "1"

import pandas as pd


class PhotoFix(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = {"_data_dir": self.tmp, "images": {"public_base": "http://x", "vision_workers": 4, "fix_workers": 3}}
        from crawler.config import run_dir
        self.d = run_dir(self.cfg, "r")
        self.d.mkdir(parents=True, exist_ok=True)
        rows = [{"url": f"u{i}", "files": f"k{i}/1.webp | k{i}/2.webp", "notes": ""} for i in range(6)]
        pd.DataFrame(rows).to_csv(self.d / "05_images.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame([{"url": f"u{i}", "name": f"p{i}"} for i in range(6)]).to_csv(
            self.d / "05_new_products.csv", index=False, encoding="utf-8-sig")
        self.calls = []
        self.lock = threading.Lock()

    def fake_check(self, model, url, name, key):
        with self.lock:
            self.calls.append(url)
        bad = url.endswith("/1.webp")                          # photo 1 of every product has text on it
        return {"overlay_text": bad, "shop_logo": False, "white_background": True, "product_whole": True,
                "matches_name": True, "note": "", "_cost": 0.001}

    def run_fix(self):
        from crawler import aiphotos
        with mock.patch.object(aiphotos, "secret", return_value="k"), \
             mock.patch("crawler.vision.check", side_effect=self.fake_check):
            return aiphotos.fix_photos(self.cfg, "r")

    def test_bad_dropped_and_rerun_checks_only_new(self):
        st = self.run_fix()
        self.assertEqual(len(self.calls), 18)              # 12 photos + a second look at the 6 flagged ones
        self.assertEqual(st["dropped"], 6)
        im = pd.read_csv(self.d / "05_images.csv", dtype=str).fillna("")
        self.assertTrue(all(f == f"k{i}/2.webp" for i, f in enumerate(im["files"])))
        # a new product appears; the old photos are not sent again
        im = pd.concat([im, pd.DataFrame([{"url": "u9", "files": "k9/2.webp", "notes": ""}])])
        im.to_csv(self.d / "05_images.csv", index=False, encoding="utf-8-sig")
        self.calls.clear()
        self.run_fix()
        self.assertEqual(self.calls, ["http://x/img/k9/2.webp"])
        vis = pd.read_csv(self.d / "05_vision.csv", dtype=str)
        self.assertEqual(len(vis), 13)


if __name__ == "__main__":
    unittest.main()
