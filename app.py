#!/usr/bin/env python3
"""Web UI for the Caravan Sanat crawler (standard-library HTTP server).

Environment variables:
  PORT               port (Render sets it)                        default 8000
  APP_PASSWORD       password for the whole UI (HTTP Basic, any username)
  DATA_DIR           where taxonomy, settings, cache and runs live default ./data
  ANTHROPIC_API_KEY  Claude API key (AI filter, translation)
  SERPER_API_KEY     web-search API key (optional)
  FX_API_KEY         used inside the dollar-API URL as {FX_API_KEY} (optional)

On Render's free plan the disk is wiped on every restart or deploy: download
the run files (and a backup of settings) after each run.
"""
from __future__ import annotations

import base64
import hmac
import io
import json
import logging
import os
import re
import shutil
import threading
import time
import traceback
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd
import yaml

from crawler import pipeline as P
from crawler.config import ROOT, load_config
from crawler.fetch import Fetcher
from crawler.io import ENCODING, FILES
from crawler.taxonomy import build_queue, load_taxonomy

DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
STATIC = ROOT / "static"
RUN_NAME = re.compile(r"^[\w\-]{1,60}$", re.UNICODE)
STEPS = ["queue", "search", "crawl", "filter", "dedupe", "export"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("app")

# ---------------------------------------------------------------- config


def paths() -> dict[str, Path]:
    return {"taxonomy": DATA_DIR / "taxonomy.csv", "sources": DATA_DIR / "sources.yaml",
            "settings": DATA_DIR / "settings.json", "site": DATA_DIR / "site_export.csv"}


def read_settings() -> dict:
    p = paths()["settings"]
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def build_cfg() -> dict:
    cfg = load_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg["_data_dir"] = DATA_DIR
    (DATA_DIR / "cache").mkdir(exist_ok=True)
    (DATA_DIR / "runs").mkdir(exist_ok=True)
    ps = paths()
    if ps["sources"].exists():
        cfg["sources"] = yaml.safe_load(ps["sources"].read_text(encoding="utf-8")) or {}
    cfg["taxonomy"]["file"] = str(ps["taxonomy"])
    cfg["site_catalog"]["file"] = str(ps["site"]) if ps["site"].exists() else None
    s = read_settings()
    for section in ("fx", "search", "http", "llm", "price", "dedupe", "routes"):
        if isinstance(s.get(section), dict):
            if section == "http" and "proxy" in s["http"]:
                cfg["http"]["proxy"] = {**(cfg["http"].get("proxy") or {}), **s["http"]["proxy"]}
                rest = {k: v for k, v in s["http"].items() if k != "proxy"}
                cfg["http"].update(rest)
            else:
                cfg.setdefault(section, {}).update(s[section])
    return cfg


# ---------------------------------------------------------------- job runner


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.state = {"running": False, "step": "", "run": "", "started": 0, "ended": 0,
                      "ok": None, "error": ""}
        self.logs: deque[str] = deque(maxlen=400)
        h = logging.Handler()
        h.emit = lambda r: self.logs.append(f"{time.strftime('%H:%M:%S')} {r.levelname} {r.getMessage()}")
        h.setLevel(logging.INFO)
        logging.getLogger("crawler").addHandler(h)

    def start(self, step: str, run: str, opts: dict) -> str | None:
        with self.lock:
            if self.state["running"]:
                return "یک مرحله در حال اجراست؛ صبر کنید یا متوقفش کنید."
            P.STOP.clear()
            P.PROGRESS.update(step=step, done=0, total=0)
            self.logs.clear()
            self.state = {"running": True, "step": step, "run": run, "started": time.time(),
                          "ended": 0, "ok": None, "error": ""}
            self.thread = threading.Thread(target=self._run, args=(step, run, opts), daemon=True)
            self.thread.start()
        return None

    def _run(self, step: str, run: str, opts: dict) -> None:
        try:
            cfg = build_cfg()
            if step == "queue":
                P.step_queue(cfg, run, mode=opts.get("mode", "gap"), level1=opts.get("level1") or None,
                             path_contains=opts.get("path") or None,
                             ids=[i for i in re.split(r"[\s,،]+", opts.get("ids") or "") if i] or None,
                             include_flagged=bool(opts.get("include_flagged")),
                             limit=int(opts["limit"]) if str(opts.get("limit") or "").isdigit() else None)
            elif step == "search":
                P.step_search(cfg, run)
            elif step == "crawl":
                P.step_crawl(cfg, run, use_llm_fallback=bool(opts.get("llm_fallback")))
            elif step == "filter":
                P.step_filter(cfg, run)
            elif step == "dedupe":
                P.step_dedupe(cfg, run, None, skip_filter=bool(opts.get("skip_filter")))
            elif step == "export":
                P.step_export(cfg, run)
            if not P.STOP.is_set() and P.PROGRESS.get("total"):
                P.PROGRESS["done"] = P.PROGRESS["total"]
            for line in P.stats(cfg, run).splitlines():
                self.logs.append(line)
            self.state["ok"] = not P.STOP.is_set()
        except Exception as e:
            self.state["ok"] = False
            self.state["error"] = str(e)
            self.logs.append("ERROR " + str(e))
            log.error(traceback.format_exc())
        finally:
            self.state["running"] = False
            self.state["ended"] = time.time()

    def status(self) -> dict:
        return {**self.state, "progress": dict(P.PROGRESS), "logs": list(self.logs)[-200:]}


JOB = Job()
_TAX_CACHE: dict = {"mtime": None, "df": None}


def taxonomy_df():
    p = paths()["taxonomy"]
    if not p.exists():
        return None
    m = p.stat().st_mtime
    if _TAX_CACHE["mtime"] != m:
        _TAX_CACHE.update(mtime=m, df=load_taxonomy(p))
    return _TAX_CACHE["df"]


# ---------------------------------------------------------------- helpers

def run_path(run: str) -> Path:
    if not RUN_NAME.match(run):
        raise ValueError("bad run name")
    return DATA_DIR / "runs" / run


def file_rows(p: Path) -> int:
    try:
        return len(pd.read_csv(p, encoding=ENCODING, dtype=str, usecols=[0]))
    except Exception:
        return 0


def list_runs() -> list[dict]:
    out = []
    d = DATA_DIR / "runs"
    if not d.exists():
        return out
    for r in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if r.is_dir():
            out.append({"name": r.name, "modified": r.stat().st_mtime,
                        "files": {f.name: file_rows(f) for f in sorted(r.glob("*.csv"))}})
    return out


def test_site(domain: str) -> dict:
    """Which source type works for a shop? Tries the Woo Store API, then Woo search."""
    domain = re.sub(r"^https?://", "", domain.strip()).strip("/")
    cfg = build_cfg()
    f = Fetcher(cfg, "iran")
    out = {"domain": domain, "store_api": False, "woo_search": False, "robots_ok": None,
           "sample": [], "suggested": None, "note": ""}
    base = f"https://{domain}"
    out["robots_ok"] = f.allowed(base + "/")
    r = f.get(base + "/wp-json/wc/store/v1/products", params={"per_page": 3}, as_json=True, use_cache=False)
    if r and r.get("status") == 200 and isinstance(r.get("json"), list):
        out["store_api"] = True
        out["sample"] = [{"name": i.get("name"), "price": (i.get("prices") or {}).get("price"),
                          "currency": (i.get("prices") or {}).get("currency_code")} for i in r["json"][:3]]
        out["suggested"] = "woo_store_api"
    else:
        r2 = f.get(base + "/", params={"s": "a", "post_type": "product"}, use_cache=False)
        if r2 and r2.get("status") == 200 and "woocommerce" in (r2.get("text") or "").lower():
            out["woo_search"] = True
            out["suggested"] = "woo_search"
        elif r2 is None or (r and r.get("status") in (403, 451)):
            out["note"] = "سایت به سرور جواب نداد یا مسدود کرد؛ احتمالاً IP خارج از ایران را می‌بندد."
        else:
            out["suggested"] = "url_template"
            out["note"] = "ووکامرس تشخیص داده نشد؛ آدرس جستجو و انتخابگر لینک را دستی بدهید."
    return out


SOURCE_TYPES = ("woo_store_api", "woo_search", "url_template")
SOURCES_HEADER = """# سایت‌های منبع کاروان صنعت — این فایل از صفحه‌ی «منابع» ساخته می‌شود.
# iran_sites: سایت‌های ایرانی. type یکی از: woo_store_api | woo_search | url_template
# level1: اختیاری؛ اگر خالی باشد سایت برای همه‌ی دسته‌ها جستجو می‌شود.
# foreign_sites: سایت‌های خارجی (فعلاً خاموش). web_search: جستجو در کل وب و دامنه‌های کنار گذاشته‌شده.
"""


def sources_text() -> str:
    p = paths()["sources"]
    return p.read_text(encoding="utf-8") if p.exists() else (ROOT / "sources.yaml").read_text(encoding="utf-8")


def validate_sources(txt: str) -> dict:
    data = yaml.safe_load(txt) or {}
    assert isinstance(data, dict), "باید یک نگاشت YAML باشد"
    for s in data.get("iran_sites") or []:
        assert isinstance(s, dict) and s.get("domain"), "هر سایت باید domain داشته باشد"
        assert s.get("type", "woo_search") in SOURCE_TYPES, f"نوع نامعتبر برای {s.get('domain')}"
        if s.get("type") == "url_template":
            assert "{query}" in (s.get("search_url") or ""), f"search_url سایت {s['domain']} باید {{query}} داشته باشد"
            assert s.get("link_selector"), f"link_selector سایت {s['domain']} لازم است"
    return data


def clean_site(s: dict) -> dict:
    dom = re.sub(r"^https?://", "", str(s.get("domain", "")).strip()).strip("/").lower()
    dom = dom[4:] if dom.startswith("www.") else dom
    if not re.match(r"^[a-z0-9.\-]+\.[a-z]{2,}(:\d+)?$", dom):
        raise ValueError("دامنه نامعتبر است؛ مثل example-shop.ir")
    out = {"domain": dom, "type": s.get("type") if s.get("type") in SOURCE_TYPES else "woo_search"}
    cats = [c for c in (s.get("level1") or []) if str(c).strip()]
    if cats:
        out["level1"] = cats
    for k in ("seller_name", "search_url", "link_selector"):
        if str(s.get(k) or "").strip():
            out[k] = str(s[k]).strip()
    phones = [p.strip() for p in re.split(r"[,،\s]+", str(s.get("phones") or "")) if p.strip()]
    if phones:
        out["phones"] = phones
    if out["type"] == "url_template" and ("{query}" not in out.get("search_url", "") or not out.get("link_selector")):
        raise ValueError("برای نوع url_template آدرس جستجو با {query} و انتخابگر لینک لازم است")
    return out


def save_sources(data: dict) -> str:
    data.setdefault("foreign_sites", [])
    if not data.get("web_search"):
        data["web_search"] = (yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8")) or {}).get("web_search")
    txt = SOURCES_HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    validate_sources(txt)
    paths()["sources"].write_text(txt, encoding="utf-8")
    return txt


ENCODINGS = ("utf-8-sig", "cp1256", "utf-16")


def read_any_table(data: bytes, filename: str = "") -> tuple[pd.DataFrame, str]:
    """Read an uploaded table whatever Excel did to it.

    Excel on Persian Windows saves "CSV" as Windows-1256 (byte 0x9D is the
    half-space), "Unicode text" as tab-separated UTF-16, and in some locales
    uses ; instead of , — all of these are accepted, and .xlsx too.
    """
    if filename.lower().endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        df = pd.read_excel(io.BytesIO(data), dtype=str, keep_default_na=False)
        return df, "xlsx"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encs = ("utf-16",)
    else:
        encs = ENCODINGS
    last = None
    for enc in encs:
        try:
            text = data.decode(enc)
        except UnicodeDecodeError as e:
            last = e
            continue
        if enc == "cp1256":
            # Windows-1256 has no Persian ی/ک; Excel either swaps in the Arabic
            # letters (fixed here) or writes "?" (data is lost: refuse it)
            if len(re.findall(r"[\u0600-\u06FF]\?|\?[\u0600-\u06FF]", text)) > 3:
                raise ValueError("Excel این فایل را با رمزگذاری ویندوز ذخیره کرده و حروفی مثل «ی» به «?» تبدیل شده‌اند. "
                                 "خود فایل Excel (xlsx) را بارگذاری کنید، یا در Excel از Save As نوع «CSV UTF-8» را انتخاب کنید.")
            text = text.replace("ي", "ی").replace("ك", "ک")
        first = text.split("\n", 1)[0]
        sep = max((",", ";", "\t"), key=first.count)
        df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)
        return df, f"{enc}, sep={'tab' if sep == chr(9) else sep}"
    raise ValueError(f"encoding not recognised ({last}); save the file from Excel as 'CSV UTF-8'")


def backup_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k in ("taxonomy", "sources", "settings", "site"):
            p = paths()[k]
            if p.exists():
                z.write(p, p.name)
        for f in ("fx_rates.csv", "translations.csv"):
            if (DATA_DIR / f).exists():
                z.write(DATA_DIR / f, f)
        runs = DATA_DIR / "runs"
        if runs.exists():
            for f in runs.rglob("*.csv"):
                z.write(f, "runs/" + str(f.relative_to(runs)))
    return buf.getvalue()


def restore_zip(data: bytes) -> list[str]:
    names = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if n.endswith("/") or ".." in n or n.startswith("/"):
                continue
            ok = n in ("taxonomy.csv", "sources.yaml", "settings.json", "site_export.csv",
                       "fx_rates.csv", "translations.csv") or re.match(r"^runs/[\w\-]+/[\w\-.]+\.csv$", n)
            if not ok:
                continue
            dest = DATA_DIR / n
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(n))
            names.append(n)
    return names


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "CaravanCrawler/0.1"

    def log_message(self, fmt, *args):
        if not self.path.startswith(("/api/job", "/healthz")):
            log.info("%s %s", self.command, self.path)

    # --- auth
    def _authed(self) -> bool:
        if not APP_PASSWORD or self.path == "/healthz":
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
                return hmac.compare_digest(pw, APP_PASSWORD)
            except Exception:
                return False
        return False

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="caravan-crawler"')
        self.end_headers()

    # --- responses
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400):
        self._json({"error": msg}, code)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 60 * 1024 * 1024:
            raise ValueError("file too large")
        return self.rfile.read(n) if n else b""

    def _jbody(self) -> dict:
        b = self._body()
        return json.loads(b.decode("utf-8")) if b else {}

    def _download(self, data: bytes, name: str, ctype: str):
        self._send(200, data, ctype, {"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})

    # --- routing
    def do_GET(self):
        if not self._authed():
            return self._deny()
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
        try:
            if u.path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if parts[:2] == ["api", "state"]:
                return self._json(self.state())
            if parts[:2] == ["api", "job"]:
                return self._json(JOB.status())
            if parts[:2] == ["api", "sources"]:
                txt = sources_text()
                return self._json({"yaml": txt, "data": yaml.safe_load(txt) or {}})
            if parts[:2] == ["api", "backup"]:
                return self._download(backup_zip(), f"caravan-backup-{time.strftime('%Y%m%d-%H%M')}.zip",
                                      "application/zip")
            if parts[:2] == ["api", "runs"] and len(parts) == 2:
                return self._json(list_runs())
            if parts[:2] == ["api", "runs"] and len(parts) >= 3:
                d = run_path(parts[2])
                if len(parts) == 3:
                    return self._json({"name": parts[2], "files": {f.name: file_rows(f) for f in sorted(d.glob("*.csv"))},
                                       "stats": P.stats(build_cfg(), parts[2]) if d.exists() else ""})
                if parts[3] == "zip":
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                        for f in d.glob("*.csv"):
                            z.write(f, f.name)
                    return self._download(buf.getvalue(), f"{parts[2]}.zip", "application/zip")
                if parts[3] in ("file", "download") and len(parts) == 5:
                    f = d / parts[4]
                    if not re.match(r"^[\w\-.]+\.csv$", parts[4]) or not f.exists():
                        return self._err("file not found", 404)
                    if parts[3] == "download":
                        return self._download(f.read_bytes(), f"{parts[2]}-{parts[4]}", "text/csv; charset=utf-8")
                    return self._json(self.preview(f, q))
            return self._err("not found", 404)
        except ValueError as e:
            return self._err(str(e))
        except Exception as e:
            log.error(traceback.format_exc())
            return self._err(str(e), 500)

    def do_POST(self):
        if not self._authed():
            return self._deny()
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
        try:
            if parts[:2] == ["api", "upload"] and len(parts) == 3:
                kind = parts[2]
                if kind not in ("taxonomy", "site"):
                    return self._err("unknown upload")
                data = self._body()
                dest = paths()[kind]
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".tmp")
                try:
                    df, how = read_any_table(data, unquote(self.headers.get("X-Filename", "")))
                except Exception as e:
                    return self._err(f"فایل خوانده نشد: {e}")
                df.columns = [str(c).strip() for c in df.columns]
                df.to_csv(tmp, index=False, encoding="utf-8-sig")   # stored as clean UTF-8 from here on
                if kind == "taxonomy":
                    try:
                        load_taxonomy(tmp)
                    except Exception as e:
                        tmp.unlink(missing_ok=True)
                        return self._err(str(e))
                tmp.replace(dest)
                log.info("upload %s: %d rows (%s)", kind, len(df), how)
                return self._json({"ok": True, "rows": len(df), "read_as": how, "columns": list(df.columns)})
            if parts[:3] == ["api", "sources", "site"]:      # add or replace one site (from the form)
                site = clean_site(self._jbody().get("site") or {})
                data = yaml.safe_load(sources_text()) or {}
                sites = [x for x in (data.get("iran_sites") or []) if x.get("domain") != site["domain"]]
                data["iran_sites"] = sites + [site]
                txt = save_sources(data)
                return self._json({"ok": True, "yaml": txt, "data": data})
            if parts[:3] == ["api", "sources", "remove"]:
                dom = self._jbody().get("domain", "")
                data = yaml.safe_load(sources_text()) or {}
                data["iran_sites"] = [x for x in (data.get("iran_sites") or []) if x.get("domain") != dom]
                txt = save_sources(data)
                return self._json({"ok": True, "yaml": txt, "data": data})
            if parts[:2] == ["api", "sources"]:              # whole file (advanced editor)
                txt = self._jbody().get("yaml", "")
                try:
                    data = validate_sources(txt)
                except Exception as e:
                    return self._err(f"YAML نامعتبر: {e}")
                paths()["sources"].write_text(txt, encoding="utf-8")
                return self._json({"ok": True, "yaml": txt, "data": data, "sites": len(data.get("iran_sites") or [])})
            if parts[:2] == ["api", "settings"]:
                body = self._jbody()
                cur = read_settings()
                for k, v in body.items():
                    if isinstance(v, dict):
                        cur.setdefault(k, {}).update(v)
                paths()["settings"].write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
                return self._json({"ok": True, "settings": cur})
            if parts[:2] == ["api", "restore"]:
                return self._json({"ok": True, "restored": restore_zip(self._body())})
            if parts[:2] == ["api", "test-site"]:
                return self._json(test_site(self._jbody().get("domain", "")))
            if parts[:2] == ["api", "queue-preview"]:
                return self._json(self.queue_preview(self._jbody()))
            if parts[:2] == ["api", "job", "stop"]:
                P.STOP.set()
                return self._json({"ok": True})
            if parts[:2] == ["api", "runs"] and len(parts) == 5 and parts[3] == "step":
                run, step = parts[2], parts[4]
                run_path(run)
                if step not in STEPS:
                    return self._err("unknown step")
                if step != "queue" and not (run_path(run) / FILES["queue"]).exists():
                    return self._err("اول مرحله‌ی «صف» را اجرا کنید.")
                if step == "queue" and taxonomy_df() is None:
                    return self._err("اول فایل تاکسونومی را بارگذاری کنید.")
                err = JOB.start(step, run, self._jbody())
                return self._err(err, 409) if err else self._json({"ok": True})
            return self._err("not found", 404)
        except ValueError as e:
            return self._err(str(e))
        except Exception as e:
            log.error(traceback.format_exc())
            return self._err(str(e), 500)

    def do_DELETE(self):
        if not self._authed():
            return self._deny()
        parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]
        if parts[:2] == ["api", "runs"] and len(parts) == 3:
            if JOB.state["running"] and JOB.state["run"] == parts[2]:
                return self._err("این اجرا در حال کار است", 409)
            shutil.rmtree(run_path(parts[2]), ignore_errors=True)
            return self._json({"ok": True})
        return self._err("not found", 404)

    # --- views
    def state(self) -> dict:
        cfg = build_cfg()
        tax = taxonomy_df()
        tinfo = None
        if tax is not None:
            leaves = tax[tax["is_leaf"]]
            tinfo = {"nodes": len(tax), "leaves": int(len(leaves)),
                     "gap_leaves": int((leaves["site_product_count"] == 0).sum()),
                     "level1": sorted(tax["level1"].dropna().unique().tolist())}
        site = paths()["site"]
        src = cfg.get("sources") or {}
        return {
            "taxonomy": tinfo,
            "site_export": {"rows": file_rows(site)} if site.exists() else None,
            "sources": {"iran_sites": len(src.get("iran_sites") or []),
                        "foreign_sites": len(src.get("foreign_sites") or [])},
            "keys": {k: bool(os.environ.get(k)) for k in ("ANTHROPIC_API_KEY", "SERPER_API_KEY", "FX_API_KEY")},
            "settings": {
                "fx": {k: cfg["fx"].get(k) for k in ("provider", "manual_rate_toman", "rate_type", "url", "json_path", "unit")},
                "search": {"use_web_search": cfg["search"].get("use_web_search", False),
                           "max_hits_per_source": cfg["search"].get("max_hits_per_source", 8)},
                "http": {"delay_seconds": cfg["http"].get("delay_seconds"),
                         "proxy": cfg["http"].get("proxy") or {}},
                "llm": {"model": cfg["llm"].get("model"), "min_confidence": cfg["llm"].get("min_confidence")},
            },
            "runs": list_runs(),
            "job": JOB.status(),
            "ephemeral": bool(os.environ.get("RENDER")),
            "has_settings": paths()["settings"].exists(),
            "has_sources": paths()["sources"].exists(),
        }

    def queue_preview(self, opts: dict) -> dict:
        tax = taxonomy_df()
        if tax is None:
            return {"count": 0, "rows": [], "error": "تاکسونومی بارگذاری نشده"}
        cfg = build_cfg()
        q = build_queue(tax, cfg, mode=opts.get("mode", "gap"), level1=opts.get("level1") or None,
                        path_contains=opts.get("path") or None,
                        ids=[i for i in re.split(r"[\s,،]+", opts.get("ids") or "") if i] or None,
                        include_flagged=bool(opts.get("include_flagged")))
        total = len(q)
        if str(opts.get("limit") or "").isdigit():
            q = q.head(int(opts["limit"]))
        return {"count": len(q), "total": total, "routes": q["route"].value_counts().to_dict(),
                "rows": q.head(30).to_dict("records")}

    def preview(self, f: Path, q: dict) -> dict:
        df = pd.read_csv(f, encoding=ENCODING, dtype=str, keep_default_na=False)
        total = len(df)
        if q.get("q"):
            needle = q["q"].strip().lower()
            mask = df.apply(lambda r: needle in " ".join(r.values).lower(), axis=1)
            df = df[mask]
        off = int(q.get("offset") or 0)
        lim = min(int(q.get("limit") or 100), 500)
        page = df.iloc[off:off + lim]
        return {"columns": list(df.columns), "rows": page.values.tolist(), "total": total,
                "matched": len(df), "offset": off}


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("caravan crawler UI on :%d (data: %s, auth: %s)", port, DATA_DIR, "on" if APP_PASSWORD else "OFF")
    srv.serve_forever()


if __name__ == "__main__":
    main()
