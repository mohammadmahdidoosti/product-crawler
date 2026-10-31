"""crawler/autopilot.py: night crawls walk the batch list, crawl only, and stop on failure or cost."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from crawler import autopilot as AP


class Job:
    def __init__(self):
        self.state = {"running": False, "run": "", "step": "", "ok": None, "error": "", "ended": 0, "started": 0}
        self.prog = {"step": "", "done": 0, "total": 0}


class AutopilotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"_data_dir": Path(self.tmp.name)}
        self.jobs = {"crawl": Job(), "site": Job(), "health": Job()}
        self.started = []
        self.batches = {11: {"batch": 11, "open": 3, "reviewed": False, "queued": False},
                        12: {"batch": 12, "open": 0, "reviewed": False, "queued": False}}

    def tearDown(self):
        self.tmp.cleanup()

    def start_job(self, step, run, opts):
        self.started.append((step, run))
        self.jobs["crawl"].state.update(running=True, run=run, step=step, started=time.time())
        return None

    def finish(self, ok=True):
        time.sleep(0.01)
        self.jobs["crawl"].state.update(running=False, ok=ok, ended=time.time(), error="" if ok else "boom")

    def tick(self, usd=0.1):
        summ = {"batches": list(self.batches.values())}
        with mock.patch("crawler.routing_state.summary", return_value=summ), \
             mock.patch("crawler.routing_state.review_batch") as rv, \
             mock.patch("crawler.routing_state.make_queue") as mq, \
             mock.patch.object(AP, "_crawl_cost", return_value=usd):
            AP.tick(self.cfg, self.jobs, self.start_job)
            return rv, mq

    def test_walks_probe_then_crawl_then_next(self):
        AP.configure(self.cfg, {"batches": [11, 12], "enabled": True})
        self.tick()
        self.assertEqual(self.started[-1], ("probe", "routing"))
        self.tick()                                   # still running: nothing new
        self.assertEqual(len(self.started), 1)
        self.finish()
        rv, mq = self.tick()
        self.assertEqual(self.started[-1], ("all", "batch-11"))
        rv.assert_called_once()
        self.finish()
        self.tick()                                   # batch 11 recorded done
        st = AP.load(self.cfg)
        self.assertEqual([d["batch"] for d in st["done"]], [11])
        self.tick()                                   # batch 12 has no open rows: straight to the crawl
        self.assertEqual(self.started[-1], ("all", "batch-12"))
        self.finish()
        self.tick()
        self.tick()
        st = AP.load(self.cfg)
        self.assertFalse(st["enabled"])
        self.assertEqual(st["stopped"], "all batches crawled")
        self.assertNotIn("publish", [s for s, _ in self.started])

    def test_stops_on_failure_and_cost(self):
        AP.configure(self.cfg, {"batches": [12, 11], "enabled": True, "max_usd": 0.5})
        self.tick()
        self.finish(ok=True)
        self.tick(usd=0.9)
        st = AP.load(self.cfg)
        self.assertFalse(st["enabled"])
        self.assertIn("cap", st["stopped"])
        AP.configure(self.cfg, {"enabled": True})
        self.tick()                                   # batch 11: probe
        self.finish(ok=False)
        self.tick()
        st = AP.load(self.cfg)
        self.assertFalse(st["enabled"])
        self.assertIn("probe failed", st["stopped"])

    def test_waits_for_a_publish(self):
        AP.configure(self.cfg, {"batches": [12], "enabled": True})
        self.jobs["site"].state["running"] = True
        self.tick()
        self.assertEqual(self.started, [])

    def test_status_has_no_logs(self):
        s = AP.status(self.cfg, self.jobs)
        self.assertIn("lanes", s)
        self.assertNotIn("logs", s["lanes"]["crawl"])


if __name__ == "__main__":
    unittest.main()
