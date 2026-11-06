"""Polite HTTP: robots.txt, per-domain delay, retries and a disk cache.

The cache means a rerun of a step does not hit the sites again, which is
what makes running the pipeline by hand and fixing things in between cheap.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)


_PAGE_TYPES = ("html", "json", "xml", "text", "javascript")


def _read_capped(r, max_bytes: int) -> bytes | None:
    """The body of a page, or None for a file that is not a page or a body over max_bytes (read no further)."""
    ctype = str(r.headers.get("Content-Type") or "").lower()
    if ctype and not any(t in ctype for t in _PAGE_TYPES):
        r.close()
        return None
    try:
        if int(r.headers.get("Content-Length") or 0) > max_bytes:
            r.close()
            return None
    except ValueError:
        pass
    buf, n = [], 0
    for chunk in r.iter_content(256 * 1024):
        n += len(chunk)
        if n > max_bytes:
            r.close()
            return None
        buf.append(chunk)
    return b"".join(buf)


def canon_url(url: str) -> str:
    """One spelling per page: '%d9%85' and '%D9%85' (and raw Persian) are the same URL."""
    from urllib.parse import quote, unquote, urlsplit, urlunsplit
    try:
        p = urlsplit(url.strip())
        path = quote(unquote(p.path), safe="/:@-._~!$&'()*+,;=")
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))
    except Exception:
        return url


def domain_of(url: str) -> str:
    d = urlparse(url).netloc.lower()
    return d[4:] if d.startswith("www.") else d


_DEAD_LOCK = threading.Lock()


def _read_dead(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def _remember_dead(p: Path, site: str, days: float) -> None:
    with _DEAD_LOCK:
        d = _read_dead(p)
        now = time.time()
        d = {k: v for k, v in d.items() if v.get("until", 0) > now}          # expired ones drop out
        d[site] = {"until": now + days * 86400, "since": time.strftime("%Y-%m-%d %H:%M"),
                   "times": int(d.get(site, {}).get("times", 0)) + 1}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


def prune_cache(cfg: dict) -> dict:
    """(6 Nov) Saved pages older than http.cache_days are deleted (once a night): they only make a re-run
    cheaper, and the cache was filling the disk. Paid searches (search_cache) are not touched."""
    import os
    root = Path(os.environ["CACHE_DIR"]) if os.environ.get("CACHE_DIR") else cfg["_data_dir"] / "cache"
    limit = time.time() - float(cfg.get("http", {}).get("cache_days", 3)) * 86400
    n = size = 0
    if root.exists():
        for f in root.rglob("*"):
            try:
                if f.is_file() and f.stat().st_mtime < limit:
                    size += f.stat().st_size
                    f.unlink()
                    n += 1
            except OSError:
                pass
    log.info("page cache: %d old file(s) deleted, %.1f GB freed", n, size / 1e9)
    return {"files": n, "gb": round(size / 1e9, 2)}


def robots_rules(text: str, ua: str) -> list[tuple[bool, str]]:
    """(allow, path prefix) rules of the group for our agent (else '*')."""
    groups, cur, agents, last_ua = {}, [], [], False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if not last_ua:
                agents, cur = [], []
            agents.append(v.lower())
            for a in agents:
                groups.setdefault(a, cur)
            last_ua = True
            continue
        last_ua = False
        if k in ("allow", "disallow") and agents:
            cur.append((k == "allow", v))
    name = ua.split("/")[0].lower()
    return next((g for a, g in groups.items() if a != "*" and a in name), groups.get("*", []))


def robots_allows(rules: list[tuple[bool, str]], url: str) -> bool:
    """Google's reading of robots.txt: the longest matching rule wins, Allow on a tie. Python's own parser
    takes the first match, so 'Disallow: /' followed by 'Allow: /v4/base-product/search' (Torob's API, 6 Nov)
    shut out paths the site allows."""
    p = urlparse(url)
    path = (p.path or "/") + (f"?{p.query}" if p.query else "")
    best, allow = -1, True
    for ok, rule in rules:
        if rule == "" and not ok:
            continue                                     # 'Disallow:' with nothing = everything allowed
        rx = "^" + re.escape(rule).replace(r"\*", ".*")
        if rx.endswith(r"\$"):
            rx = rx[:-2] + "$"
        if re.match(rx, path) and (len(rule) > best or (len(rule) == best and ok)):
            best, allow = len(rule), ok
    return allow


class Fetcher:
    def __init__(self, cfg: dict, route: str = "iran"):
        self._cfg = cfg
        h = cfg.get("http", {})
        self.delay = float(h.get("delay_seconds", 3))
        self.timeout = float(h.get("timeout_seconds", 25))
        self.retries = int(h.get("retries", 2))
        self.cache_days = float(h.get("cache_days", 14))
        self.respect_robots = bool(h.get("respect_robots", True))
        # page cache: only for re-runs, so it may live outside the persistent disk (CACHE_DIR)
        import os
        self.cache_dir: Path = Path(os.environ["CACHE_DIR"]) if os.environ.get("CACHE_DIR") else cfg["_data_dir"] / "cache"
        self.max_bytes = int(float(h.get("max_page_mb", 8)) * 1024 * 1024)
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
        # a site that keeps timing out or failing is skipped for the rest of the run, instead of costing
        # timeout x retries on every one of its pages
        self.dead_after = int(h.get("dead_after_failures", 4))
        self._fails: dict[str, int] = defaultdict(int)
        self._dead: set[str] = set()
        # ... and remembered for `dead_days` across runs (DATA_DIR/dead_sites.json), so the next batch
        # does not pay the same timeouts again; after that the site is tried afresh
        self.dead_days = float(h.get("dead_days", 3))
        self._dead_file: Path = cfg["_data_dir"] / "dead_sites.json"
        now = time.time()
        for k, v in _read_dead(self._dead_file).items():
            if v.get("until", 0) > now:
                self._dead.add(k)

    def _failed(self, dom: str) -> None:
        k = self.site_key(dom)
        with self._glock:
            self._fails[k] += 1
            if self.dead_after and self._fails[k] >= self.dead_after and k not in self._dead:
                self._dead.add(k)
                if self.dead_days > 0:
                    _remember_dead(self._dead_file, k, self.dead_days)
                log.warning("site %s skipped for this run (%d failures in a row)", k, self._fails[k])

    def _worked(self, dom: str) -> None:
        self._fails[self.site_key(dom)] = 0

    def is_dead(self, url: str) -> bool:
        return self.site_key(domain_of(url)) in self._dead

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
                    rp._caravan_rules = robots_rules(r.text, self.s.headers["User-Agent"])
                else:
                    rp = None  # no robots.txt -> allowed
            except requests.RequestException:
                rp = None
            self._robots[base] = rp
            if rp is not None:                                  # the site's own Crawl-delay is respected
                try:
                    cd = rp.crawl_delay(self.s.headers["User-Agent"])
                except Exception:                               # noqa: BLE001
                    cd = None
                if cd:
                    k = self.site_key(p.netloc.lower().removeprefix("www."))
                    self.domain_delays[k] = max(float(cd), self.domain_delays.get(k, self.delay))
        rp = self._robots[base]
        if rp is None:
            return True
        rules = getattr(rp, "_caravan_rules", None)
        if rules is not None:                            # longest match wins (see robots_allows)
            return robots_allows(rules, url)
        return rp.can_fetch(self.s.headers["User-Agent"], url)

    # ------------------------------------------------------------ cache
    def _cache_path(self, key: str) -> Path:
        h = hashlib.sha1(key.encode()).hexdigest()
        # paid searches are kept on the persistent disk: a page cache in CACHE_DIR (/tmp on the
        # server) is wiped by every deploy, and each re-run then paid for the same searches again
        if "serper.dev" in key:
            return self._cfg["_data_dir"] / "search_cache" / h[:2] / f"{h}.json"
        # (6 Nov) pages are kept gzipped: the plain cache grew 4-5 GB a day and was filling the server's disk
        return self.cache_dir / h[:2] / f"{h}.json.gz"

    def _cached(self, key: str) -> dict | None:
        p = self._cache_path(key)
        days = 30 if "serper.dev" in key else self.cache_days
        try:
            if p.suffix == ".gz" and not p.exists():
                p = p.with_suffix("")                                   # a page saved before 6 Nov, not gzipped
            if p.exists() and (time.time() - p.stat().st_mtime) < days * 86400:
                if p.suffix == ".gz":
                    return json.loads(gzip.decompress(p.read_bytes()).decode("utf-8"))
                return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # (6 Nov) a half-written cache file (two crawls share the cache) stopped batch 33 with
            # "Expecting value": a broken entry is a miss, the page is fetched again
            log.warning("broken cache entry %s, fetching again", p.name)
        return None

    def _store(self, key: str, data: dict) -> None:
        p = self._cache_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{threading.get_ident()}.tmp")       # write whole, then swap in:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")      # a reader never sees half
        tmp.write_bytes(gzip.compress(raw, 6) if p.suffix == ".gz" else raw)
        os.replace(tmp, p)

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
        if self.is_dead(url):
            return None
        if not self.allowed(url):
            log.info("robots.txt disallows %s", url)
            return None
        dom = domain_of(url)
        for attempt in range(self.retries + 1):
            if self.is_dead(url):
                return None
            self._wait(dom)
            try:
                r = self.s.get(url, params=params, timeout=self.timeout, stream=True)
                body = _read_capped(r, self.max_bytes)       # (5 Nov) one huge page took the crawl to 4.4 GB
            except requests.RequestException as e:
                log.warning("fetch failed %s (%s)", url, e)
                self._failed(dom)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (429, 503) and attempt < self.retries:
                time.sleep(10 * (attempt + 1))
                continue
            data = {"url": r.url, "status": r.status_code}
            if r.headers.get("X-WP-Total"):              # WordPress lists: the catalog's size (crawler/shops.py)
                data["wp_total"] = r.headers["X-WP-Total"]
            if r.status_code >= 500:
                self._failed(dom)
            else:
                self._worked(dom)
            if r.status_code != 200:
                log.info("HTTP %s for %s", r.status_code, url)
                return data
            if body is None:                              # a file (pdf, zip, video) or a page over the cap
                log.info("skipped %s: %s", url, r.headers.get("Content-Type", "") or "too big")
                data["status"] = -2
                return data
            r._content = body
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
        if "serper.dev" in url:                         # a paid search really made (a cached answer is free)
            try:
                from .probe import _count
                _count(self._cfg)
            except Exception:                             # noqa: BLE001
                pass
        return data
