"""Two job lanes (crawl / site) and resuming a job after a restart."""
import importlib
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock


class Lanes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name, "APP_PASSWORD": ""})
        self.env.start()
        import app
        self.app = importlib.reload(app)
        self.seen, self.gate = [], threading.Event()

        def fake_one(job, cfg, step, run, opts):
            job.state["current"] = step
            self.seen.append((job.lane, step, run))
            self.gate.wait(5)
        self.patch = mock.patch.object(self.app.Job, "_one", fake_one)
        self.patch.start()
        self.patch_stats = mock.patch.object(self.app.P, "stats", lambda cfg, run: "")
        self.patch_stats.start()

    def tearDown(self):
        self.gate.set()
        for j in self.app.JOBS.values():
            if j.thread:
                j.thread.join(5)
        self.patch.stop()
        self.patch_stats.stop()
        self.env.stop()
        self.tmp.cleanup()

    def wait(self, cond, t=5):
        end = time.time() + t
        while time.time() < end and not cond():
            time.sleep(0.02)
        return cond()

    def test_publish_runs_beside_crawl(self):
        A = self.app
        self.assertIsNone(A.start_job("crawl", "batch-3", {}))
        self.assertIsNone(A.start_job("publish", "batch-2", {"limit": 5}))     # other lane, other run: fine
        self.assertTrue(self.wait(lambda: len(self.seen) == 2))
        self.assertEqual({x[0] for x in self.seen}, {"crawl", "site"})
        self.assertIsNotNone(A.start_job("images", "batch-4", {}))           # crawl lane busy
        self.assertIsNotNone(A.start_job("status", "batch-3", {}))           # same run as the crawl: refused
        self.assertTrue((A.DATA_DIR / "jobs" / "crawl.json").exists())
        self.gate.set()
        self.assertTrue(self.wait(lambda: not any(j.state["running"] for j in A.JOBS.values())))
        self.assertFalse((A.DATA_DIR / "jobs" / "crawl.json").exists())     # finished: nothing to resume

    def test_resume_after_restart(self):
        A = self.app
        (A.DATA_DIR / "jobs").mkdir(parents=True)
        (A.DATA_DIR / "jobs" / "crawl.json").write_text(json.dumps(
            {"step": "all", "run": "batch-2", "opts": {"keep_queue": True, "from_step": "search"},
             "tries": 0, "current": "images"}), encoding="utf-8")
        self.gate.set()
        A.JOBS["crawl"].resume()
        self.assertTrue(self.wait(lambda: not A.JOBS["crawl"].state["running"] and self.seen))
        # continued from the step that was cut short; from_step dropped so no file was cleared
        self.assertEqual([s for _, s, _ in self.seen], ["images", "describe", "prices", "export"])

    def test_no_endless_resume(self):
        A = self.app
        (A.DATA_DIR / "jobs").mkdir(parents=True)
        (A.DATA_DIR / "jobs" / "site.json").write_text(json.dumps(
            {"step": "publish", "run": "batch-2", "opts": {}, "tries": 2, "current": "publish"}), encoding="utf-8")
        A.JOBS["site"].resume()
        self.assertFalse(A.JOBS["site"].state["running"])
        self.assertFalse((A.DATA_DIR / "jobs" / "site.json").exists())


if __name__ == "__main__":
    unittest.main()
