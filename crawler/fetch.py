"""Polite HTTP: robots.txt, per-domain delay, retries and a disk cache.

The cache means a rerun of a step does not hit the sites again, which is
what makes running the pipeline by hand and fixing things in between cheap.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)


def domain_of(url: str) -> str:
    d = urlparse(url).netloc.lower()
    return d[4:] if d.startswith("www.") else d


class Fetcher:
    def __init__(self, cfg: dict, route: str = "iran"):
        h = cfg.get("http", {})
        self.delay = float(h.get("delay_seconds", 3))
        self.timeout = float(h.get("timeout_seconds", 25))
        self.retries = int(h.get("retries", 2))
        self.cache_days = float(h.get("cache_days", 14))
        self.respect_robots = bool(h.get("respect_robots", True))
        # page cache: only for re-runs, so it may live outside the persistent disk (CACHE_DIR)
        import os
        self.cache_dir: Path = Path(os.environ["CACHE_DIR"]) if os.environ.get("CACHE_DIR") else cfg["_data_dir"] / "cache"
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": h.get("user_agent", "CaravanSanatBot/0.1 (+https://caravansanat.com)"),
            "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.6",
        })
        proxy = (h.get("proxy") or {}).get(route)
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        # thread-safe politeness: one lock per site, so several sites are fetched in parallel
        # while each site still gets at most one request per `delay` seconds
        self.domain_delays = {k.lower(): float(v) for k, v in (h.get("domain_delays") or {}).items()}
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._glock = threading.Lock()

    @staticmethod
    def site_key(dom: str) -> str:
        """'cnheli.en.made-in-china.com' -> 'made-in-china.com' (one politeness budget per site)."""
        parts = dom.split(".")
        if len(parts) >= 3 and parts[-2] in ("co", "ac", "org", "gov", "net") and len(parts[-1]) == 2:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])

    # ------------------------------------------------------------ robots
    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        with self._glock:
            known = base in self._robots
        if not known:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=self.timeout)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots.txt -> allowed
            except requests.RequestException:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.s.headers["User-Agent"], url)

    # ------------------------------------------------------------ cache
    def _cache_path(self, key: str) -> Path:
        h = hashlib.sha1(key.encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def _cached(self, key: str) -> dict | None:
        p = self._cache_path(key)
        if p.exists() and (time.time() - p.stat().st_mtime) < self.cache_days * 86400:
            return json.loads(p.read_text(encoding="utf-8"))
        return None

    def _store(self, key: str, data: dict) -> None:
        p = self._cache_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------ get
    def _wait(self, dom: str) -> None:
        k = self.site_key(dom)
        delay = self.domain_delays.get(k, self.delay)
        with self._glock:
            lock = self._locks[k]
        with lock:
            last = self._last.get(k)
            if last is not None:
                gap = time.time() - last
                if gap < delay:
                    time.sleep(delay - gap)
            self._last[k] = time.time()

    def get(self, url: str, *, params: dict | None = None, as_json: bool = False,
            use_cache: bool = True) -> dict | None:
        """Return {'url','status','text'} (or 'json') or None when not fetched."""
        key = url + ("?" + json.dumps(params, sort_keys=True, ensure_ascii=False) if params else "")
        if use_cache and (hit := self._cached(key)):
            return hit
        if not self.allowed(url):
            log.info("robots.txt disallows %s", url)
            return None
        dom = domain_of(url)
        for attempt in range(self.retries + 1):
            self._wait(dom)
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                log.warning("fetch failed %s (%s)", url, e)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (429, 503) and attempt < self.retries:
                time.sleep(10 * (attempt + 1))
                continue
            data = {"url": r.url, "status": r.status_code}
            if r.status_code != 200:
                log.info("HTTP %s for %s", r.status_code, url)
                return data
            if as_json:
                try:
                    data["json"] = r.json()
                except ValueError:
                    data["status"] = -1
                    return data
            else:
                r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
                data["text"] = r.text
            self._store(key, data)
            return data
        return None

    def post_json(self, url: str, payload: dict, headers: dict) -> dict | None:
        key = url + json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if hit := self._cached(key):
            return hit
        try:
            r = self.s.post(url, json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("POST failed %s (%s)", url, e)
            return None
        if r.status_code != 200:
            log.warning("POST %s -> HTTP %s %s", url, r.status_code, r.text[:200])
            return None
        data = {"url": url, "status": 200, "json": r.json()}
        self._store(key, data)
        return data
