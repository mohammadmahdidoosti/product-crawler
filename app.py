#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web service around crawler.py — enter a site URL, get its products back.

Standard library HTTP server only (no framework), so it runs anywhere Python runs.

Environment variables:
  PORT                  port to listen on (Render sets this)            default 8000
  APP_PASSWORD          password for the UI/API (HTTP Basic, any username).  empty = no auth
  DATA_DIR              where job files live                           default /tmp/crawler-jobs
  MAX_CONCURRENT_JOBS   jobs running at the same time                  default 2
  CRAWLER_PROXY         optional http(s) proxy for all outgoing crawls
  ALLOW_PRIVATE_URLS    "1" to allow localhost / private IPs (testing only)
  MAX_IMAGE_MB          cap on downloaded images per job                default 1500

API (JSON):
  POST   /api/jobs                {"url": "...", "mode": "auto", "max_pages": 2000, "delay": 1.0,
                                   "images": "none|main|all", "enrich": true}
  GET    /api/jobs                list
  GET    /api/jobs/<id>           status + log + preview rows
  POST   /api/jobs/<id>/stop
  DELETE /api/jobs/<id>
  GET    /api/jobs/<id>/download/<csv|xlsx|json|zip>
  GET    /healthz
"""

from __future__ import annotations

import base64
import collections
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse

import crawler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", "/tmp/crawler-jobs")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
MAX_CONCURRENT = max(1, int(os.environ.get("MAX_CONCURRENT_JOBS", "2")))
PROXY = os.environ.get("CRAWLER_PROXY") or None
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_URLS") == "1"
MAX_JOBS_KEPT = 50

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("app")


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

class JobLogHandler(logging.Handler):
    def __init__(self, job: "Job"):
        super().__init__()
        self.job = job
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.job.logs.append(self.format(record))
        except Exception:
            pass


class Job:
    def __init__(self, url: str, mode: str, max_pages: int, delay: float,
                 images: str = "main", enrich: bool = True):
        self.id = uuid.uuid4().hex[:10]
        self.url = url
        self.host = crawler.norm_host(urlparse(url).netloc)
        self.mode, self.max_pages, self.delay = mode, max_pages, delay
        self.images, self.enrich = images, enrich
        self.phase = ""                   # crawl | images | export
        self.status = "queued"            # queued | running | done | failed | stopped
        self.error = ""
        self.created = time.time()
        self.started = self.finished = None
        self.count = 0
        self.logs: collections.deque[str] = collections.deque(maxlen=400)
        self.stop_event = threading.Event()
        self.dir = os.path.join(DATA_DIR, self.id)
        self.jsonl = os.path.join(self.dir, "products.jsonl")
        self.crawler: crawler.SiteCrawler | None = None
        os.makedirs(self.dir, exist_ok=True)

    def live_count(self) -> int:
        if self.crawler is not None and self.status == "running":
            return self.crawler.writer.count
        return self.count

    def summary(self) -> dict:
        end = self.finished or time.time()
        return {
            "id": self.id, "url": self.url, "host": self.host, "mode": self.mode,
            "max_pages": self.max_pages, "delay": self.delay, "status": self.status,
            "images": self.images, "enrich": self.enrich, "phase": self.phase,
            "has_zip": os.path.exists(os.path.join(self.dir, "products-images.zip")),
            "error": self.error, "count": self.live_count(), "created": self.created,
            "started": self.started, "finished": self.finished,
            "elapsed": round(end - self.started) if self.started else 0,
        }

    def preview(self, n: int = 50) -> list[dict]:
        rows = []
        if os.path.exists(self.jsonl):
            with open(self.jsonl, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    row = {k: r.get(k, "") for k in
                           ("name", "price", "min_price", "max_price", "currency",
                            "in_stock", "categories", "url", "source", "image_main", "image_count")}
                    try:
                        row["spec_count"] = len(json.loads(r["specs"])) if r.get("specs") else 0
                    except ValueError:
                        row["spec_count"] = 0
                    row["has_description"] = bool(r.get("description"))
                    row["file_count"] = len([x for x in (r.get("files") or "").split(" | ") if x])
                    rows.append(row)
                    if len(rows) >= n:
                        break
        return rows


JOBS: "collections.OrderedDict[str, Job]" = collections.OrderedDict()
JOBS_LOCK = threading.Lock()
SLOTS = threading.Semaphore(MAX_CONCURRENT)


def run_job(job: Job):
    logger_name = f"job.{job.id}"
    lg = logging.getLogger(logger_name)
    handler = JobLogHandler(job)
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    lg.info("در صف اجرا… / queued")
    acquired = False
    try:
        while not job.stop_event.is_set():
            if SLOTS.acquire(timeout=1):
                acquired = True
                break
        if not acquired:
            job.status = "stopped"
            return
        job.status, job.started = "running", time.time()
        args = SimpleNamespace(
            user_agent=crawler.DEFAULT_USER_AGENT, proxy=PROXY, insecure=False, timeout=30,
            delay=job.delay, workers=3, max_pages=job.max_pages, mode=job.mode,
            resume=False, ignore_robots=False, out=job.dir, enrich=job.enrich,
        )
        sc = crawler.SiteCrawler(job.url, args, stop_event=job.stop_event,
                                 logger_name=logger_name, out_path=job.jsonl)
        job.crawler = sc
        job.phase = "crawl"
        job.count = sc.run()
        if job.images != "none" and job.count and not job.stop_event.is_set():
            job.phase = "images"
            sc.download_images(job.images)
        job.phase = "export"
        lg.info("ساخت فایل‌های خروجی… / building output files")
        rows = crawler.read_jsonl(job.jsonl)
        crawler.write_csv(rows, os.path.join(job.dir, "products.csv"))
        crawler.write_xlsx(rows, os.path.join(job.dir, "products.xlsx"))
        with open(os.path.join(job.dir, "products.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        img_dir = os.path.join(job.dir, "images")
        if os.path.isdir(img_dir):
            zpath = os.path.join(job.dir, "products-images.zip")
            with zipfile.ZipFile(zpath + ".part", "w", zipfile.ZIP_STORED) as z:
                for root, _, files in os.walk(img_dir):
                    for fn in sorted(files):
                        if fn.endswith("_thumb.png"):
                            continue
                        full = os.path.join(root, fn)
                        z.write(full, os.path.relpath(full, job.dir))
                z.write(os.path.join(job.dir, "products.xlsx"), "products.xlsx")
            os.replace(zpath + ".part", zpath)
            lg.info("ZIP عکس‌ها: %.1f MB", os.path.getsize(zpath) / 1048576)
        job.phase = ""
        job.status = "stopped" if job.stop_event.is_set() else "done"
    except Exception as e:  # report, never crash the server
        log.exception("job %s failed", job.id)
        job.status, job.error = "failed", f"{type(e).__name__}: {e}"
        lg.error("خطا: %s", job.error)
    finally:
        if job.crawler is not None:
            job.count = job.crawler.writer.count
        job.crawler = None
        job.finished = time.time()
        if acquired:
            SLOTS.release()
        lg.removeHandler(handler)


def prune_jobs():
    with JOBS_LOCK:
        while len(JOBS) > MAX_JOBS_KEPT:
            old_id, old = next(iter(JOBS.items()))
            if old.status in ("queued", "running"):
                break
            JOBS.pop(old_id)
            shutil.rmtree(old.dir, ignore_errors=True)


def validate_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise ValueError("آدرس خالی است")
    if "://" in url and not re.match(r"^https?://", url, re.I):
        raise ValueError("فقط آدرس‌های http و https قابل قبول است")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("آدرس معتبر نیست")
    if not ALLOW_PRIVATE:
        try:
            infos = socket.getaddrinfo(p.hostname, None)
        except socket.gaierror:
            raise ValueError(f"دامنه پیدا نشد: {p.hostname}")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValueError("آدرس‌های داخلی/خصوصی مجاز نیستند")
    return f"{p.scheme}://{p.netloc}/"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as _f:
    INDEX_HTML = _f.read().encode("utf-8")

DOWNLOADS = {
    "csv": ("products.csv", "text/csv; charset=utf-8"),
    "xlsx": ("products.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "json": ("products.json", "application/json; charset=utf-8"),
    "zip": ("products-images.zip", "application/zip"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ProductCrawler/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if not self.path.startswith(("/api/jobs/", "/healthz")):   # skip noisy polling
            log.info("%s %s", self.address_string(), fmt % args)

    # ---- helpers
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, fpath: str, ctype: str, download_name: str):
        """Stream a file from disk (zips can be large)."""
        size = os.path.getsize(fpath)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(fpath, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 256 * 1024)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, msg: str):
        self._json(code, {"error": msg})

    def _authorized(self) -> bool:
        if not APP_PASSWORD:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
                if hmac.compare_digest(pw.encode(), APP_PASSWORD.encode()):
                    return True
            except Exception:
                pass
        self._send(401, "رمز عبور لازم است".encode("utf-8"), "text/plain; charset=utf-8",
                   {"WWW-Authenticate": 'Basic realm="Product Crawler", charset="UTF-8"'})
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise ValueError("درخواست بیش از حد بزرگ است")
        raw = self.rfile.read(n) if n else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("بدنه‌ی درخواست باید JSON باشد")
        return data

    def _job(self, job_id: str) -> Job | None:
        with JOBS_LOCK:
            return JOBS.get(job_id)

    # ---- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json(200, {"ok": True})
        if not self._authorized():
            return
        if path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if path == "/api/jobs":
            with JOBS_LOCK:
                jobs = [j.summary() for j in reversed(JOBS.values())]
            return self._json(200, {"jobs": jobs, "max_concurrent": MAX_CONCURRENT})
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{10})", path)
        if m:
            job = self._job(m.group(1))
            if not job:
                return self._error(404, "کار پیدا نشد")
            return self._json(200, {**job.summary(), "logs": list(job.logs), "preview": job.preview()})
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{10})/download/(csv|xlsx|json|zip)", path)
        if m:
            job = self._job(m.group(1))
            if not job:
                return self._error(404, "کار پیدا نشد")
            fname, ctype = DOWNLOADS[m.group(2)]
            fpath = os.path.join(job.dir, fname)
            if not os.path.exists(fpath):
                return self._error(409, "خروجی هنوز آماده نیست")
            stamp = time.strftime("%Y%m%d-%H%M", time.localtime(job.created))
            safe_host = re.sub(r"[^A-Za-z0-9.-]+", "_", job.host)
            suffix = "-images.zip" if m.group(2) == "zip" else f".{m.group(2)}"
            return self._send_file(fpath, ctype, f"{safe_host}-{stamp}{suffix}")
        self._error(404, "مسیر پیدا نشد")

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return
        if path == "/api/jobs":
            try:
                body = self._body()
                url = validate_url(body.get("url", ""))
                mode = body.get("mode", "auto")
                if mode not in ("auto", "api", "sitemap", "crawl"):
                    raise ValueError("حالت نامعتبر")
                max_pages = int(body.get("max_pages", 2000))
                delay = float(body.get("delay", 1.0))
                if not (1 <= max_pages <= 20000):
                    raise ValueError("حداکثر صفحه باید بین ۱ تا ۲۰۰۰۰ باشد")
                if not (0.2 <= delay <= 30):
                    raise ValueError("تأخیر باید بین ۰٫۲ تا ۳۰ ثانیه باشد")
                images = body.get("images", "main")
                if images not in ("none", "main", "all"):
                    raise ValueError("گزینه‌ی عکس نامعتبر")
                enrich = bool(body.get("enrich", True))
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._error(400, str(e))
            job = Job(url, mode, max_pages, delay, images, enrich)
            with JOBS_LOCK:
                JOBS[job.id] = job
            threading.Thread(target=run_job, args=(job,), daemon=True, name=f"job-{job.id}").start()
            prune_jobs()
            return self._json(201, job.summary())
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{10})/stop", path)
        if m:
            job = self._job(m.group(1))
            if not job:
                return self._error(404, "کار پیدا نشد")
            job.stop_event.set()
            return self._json(200, job.summary())
        self._error(404, "مسیر پیدا نشد")

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{10})", path)
        if not m:
            return self._error(404, "مسیر پیدا نشد")
        with JOBS_LOCK:
            job = JOBS.get(m.group(1))
            if not job:
                return self._error(404, "کار پیدا نشد")
            if job.status in ("queued", "running"):
                return self._error(409, "اول کار را متوقف کنید")
            JOBS.pop(job.id)
        shutil.rmtree(job.dir, ignore_errors=True)
        self._json(200, {"deleted": job.id})


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    port = int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    log.info("listening on :%d (auth %s, %d concurrent jobs)", port,
             "on" if APP_PASSWORD else "OFF", MAX_CONCURRENT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
