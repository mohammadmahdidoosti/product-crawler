#!/usr/bin/env python3
"""Web UI for the Caravan Sanat crawler (standard-library HTTP server).

Environment variables:
  PORT               port (Render sets it)                        default 8000
  APP_PASSWORD       password for the whole UI (HTTP Basic, any username)
  DATA_DIR           where taxonomy, settings, cache and runs live default ./data
  LLM_API_KEY        key of an OpenAI-compatible model service (optional; filter, translation)
  SERPER_API_KEY     web-search API key (optional)
  WC_URL, WC_KEY, WC_SECRET, WP_USER, WP_APP_PASSWORD
                     direct publishing to the WooCommerce site (crawler/publish.py)
  FX_API_KEY         used inside the dollar-API URL as {FX_API_KEY} (optional)

On Render's free plan the disk is wiped on every restart or deploy: download
the run files (and a backup of settings) after each run.
"""
from __future__ import annotations

import base64
import hmac
import csv
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
from crawler.io import ENCODING, FILES, read_csv
from crawler.taxonomy import build_queue, load_taxonomy

DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
STATIC = ROOT / "static"
RUN_NAME = re.compile(r"^[\w\-]{1,60}$", re.UNICODE)
STEPS = ["queue", "search", "crawl", "filter", "topup", "dedupe", "images", "describe", "prices", "export"]

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
    """One lane of work. 'crawl' runs the crawl steps (and owns the pipeline's global STOP and
    PROGRESS); 'site' runs the steps that talk to the shop (publish, status, brands) with its own,
    so batch N can be published while batch N+1 is crawled. A lane's job is written to
    DATA_DIR/jobs/<lane>.json while it runs: if the server restarts (a deploy, a crash) the job is
    started again on boot and continues where it was (every step skips the rows it finished)."""

    def __init__(self, lane: str = "crawl"):
        self.lane = lane
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.state = {"running": False, "step": "", "run": "", "started": 0, "ended": 0,
                      "ok": None, "error": "", "lane": lane}
        self.logs: deque[str] = deque(maxlen=400)
        if lane == "crawl":
            self.stop_ev, self.prog = P.STOP, P.PROGRESS      # the pipeline's own: its steps read them
        else:
            self.stop_ev, self.prog = threading.Event(), {"step": "", "done": 0, "total": 0}
        h = logging.Handler()
        h.emit = lambda r: self.logs.append(f"{time.strftime('%H:%M:%S')} {r.levelname} {r.getMessage()}")
        h.setLevel(logging.INFO)
        site = ("crawler.publish", "crawler.brands_fa", "crawler.wc")
        h.addFilter(lambda r: r.name.startswith(site) == (lane == "site"))
        logging.getLogger("crawler").addHandler(h)

    def _saved(self) -> Path:
        return DATA_DIR / "jobs" / f"{self.lane}.json"

    def _persist(self, step: str, run: str, opts: dict, tries: int) -> None:
        try:
            self._saved().parent.mkdir(parents=True, exist_ok=True)
            self._saved().write_text(json.dumps({"step": step, "run": run, "opts": opts, "tries": tries,
                                                 "current": self.state.get("current", step)},
                                                ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def start(self, step: str, run: str, opts: dict, tries: int = 0) -> str | None:
        other = JOBS["site" if self.lane == "crawl" else "crawl"] if "JOBS" in globals() else None
        with self.lock:
            if self.state["running"]:
                return "یک مرحله در حال اجراست؛ صبر کنید یا متوقفش کنید."
            if other is not None and other.state["running"] and other.state["run"] == run and run != "routing":
                return f"اجرای «{run}» الان در خط دیگر در حال کار است؛ بعد از تمام شدنش دوباره بزنید."
            self.stop_ev.clear()
            self.prog.update(step=step, done=0, total=0)
            self.logs.clear()
            self.state = {"running": True, "step": step, "current": step, "run": run, "started": time.time(),
                          "ended": 0, "ok": None, "error": "", "lane": self.lane}
            self._persist(step, run, opts, tries)
            self.thread = threading.Thread(target=self._run, args=(step, run, opts), daemon=True)
            self.thread.start()
        return None

    def resume(self) -> None:
        """After a restart: start the job that was running when the server stopped."""
        p = self._saved()
        if not p.exists():
            return
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            p.unlink(missing_ok=True)
            return
        if int(j.get("tries", 0)) >= 2:                     # failed to finish twice: leave it to a person
            log.warning("job %s/%s not resumed (already resumed twice)", j.get("step"), j.get("run"))
            p.unlink(missing_ok=True)
            return
        opts = dict(j.get("opts") or {})
        if j.get("step") == "all":
            # continue from the step that was running; never clear files again
            opts.pop("from_step", None)
            opts.pop("steps", None)
            if j.get("current") in STEPS:
                opts["resume_from"] = j["current"]
        log.info("resuming %s job %s on %s (from %s)", self.lane, j.get("step"), j.get("run"), j.get("current"))
        self.start(j["step"], j["run"], opts, tries=int(j.get("tries", 0)) + 1)
        self.logs.append(f"ادامه‌ی خودکار بعد از ری‌استارت سرور (از «{j.get('current')}»)")

    def _one(self, cfg: dict, step: str, run: str, opts: dict) -> None:
        self.state["current"] = step
        if self._saved().exists():
            try:
                j = json.loads(self._saved().read_text(encoding="utf-8"))
                j["current"] = step
                self._saved().write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
            except (OSError, ValueError):
                pass
        self.prog.update(step=step, done=0, total=0)
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
        elif step == "topup":
            P.step_topup(cfg, run, round_=int(opts.get("round") or 1))
        elif step == "dedupe":
            P.step_dedupe(cfg, run, None, skip_filter=bool(opts.get("skip_filter")))
        elif step == "images":
            from crawler.images import step_images
            step_images(cfg, run)
        elif step == "describe":
            from crawler.describe import step_describe
            step_describe(cfg, run)
        elif step == "prices":                       # doubtful prices settled from evidence (detective)
            from crawler.detective import run_detective
            res = run_detective(cfg, run, apply=opts.get("apply", True) is not False,
                                progress=lambda d_, t_: self.prog.update(step="prices", done=d_, total=t_))
            self.logs.append(f"کارآگاه قیمت: {res['doubtful']} قیمت مشکوک؛ {res['settled']} حل شد، "
                             f"{res['left']} ماند؛ {res['searches']} جستجو")
        elif step == "enhance":
            from crawler.enhance import enhance
            urls = opts.get("urls") or []
            if not urls:
                from crawler import review as RV
                urls = [x["url"] for x in RV.items(cfg, run, "images")["items"]][:int(opts.get("limit") or 5)]
            ic = cfg.get("images", {})
            models = opts.get("models") or [ic.get("ai_model")] + list(ic.get("ai_fallback_models") or [])
            if opts.get("compare") and not opts.get("urls"):     # products whose photo needed an edit
                im = read_csv(P.run_dir(cfg, run) / "05_images.csv")
                urls = [r["url"] for r in im.to_dict("records") if "ai_fixed" in str(r.get("notes") or "")
                        or "text_overlay" in str(r.get("notes") or "")][:int(opts.get("limit") or 15)]
            enhance(cfg, run, urls, models, int(opts.get("n") or 1), first_ok=not opts.get("models"),
                    compare=bool(opts.get("compare")))
        elif step == "photo_fix":
            from crawler.aiphotos import fix_photos
            fix_photos(cfg, run)
        elif step == "vision_compare":
            from crawler.vision import compare as vcompare
            vcompare(cfg, run, opts.get("models") or [], int(opts.get("n") or 25), int(opts.get("n") or 25))
        elif step == "describe_compare":
            from crawler.describe import compare
            compare(cfg, run, opts.get("models") or [cfg.get("describe", {}).get("model")], int(opts.get("n") or 10))
        elif step == "export":
            P.step_export(cfg, run)
        elif step == "resync":
            from crawler.publish import resync
            res = resync(cfg, run, dry_run=bool(opts.get("dry_run", True)),
                         progress=lambda d, t: self.prog.update(step="resync", done=d, total=t), stop=self.stop_ev.is_set)
            self.logs.append(f"هماهنگی دسته‌ها: {res['to_change']} محصول تغییر دارد؛ {res['lose_a_category']} دسته‌ی نادرست، "
                             f"{res['gain_a_category']} دسته‌ی تازه، {res['out_of_run']} بیرون از اجرا ({res.get('to_draft', 0)} به پیش‌نویس)"
                             + ("" if opts.get("dry_run", True) else f"؛ اعمال شد روی {res['changed']}"))
        elif step == "status":
            from crawler.publish import set_status
            skus = [x for x in re.split(r"[\s,،]+", str(opts.get("skus") or "")) if x]
            res = set_status(cfg, run, opts.get("status") or "publish", skus=skus or None,
                             progress=lambda d, t: self.prog.update(step="status", done=d, total=t), stop=self.stop_ev.is_set)
            self.logs.append(f"وضعیت «{opts.get('status') or 'publish'}»: {res['changed']} از {res['requested']} محصول"
                             + (f"، ناموفق: {len(res['failed'])}" if res["failed"] else ""))
        elif step == "brands":
            from crawler import brands_fa as BF
            from crawler.wc import WC
            recs = [r for r, _ in P.export_rows(cfg, run)]
            res = BF.build(cfg, recs, redo=bool(opts.get("redo")), wc=WC(timeout=40),
                           progress=lambda d, t: self.prog.update(step="brands", done=d, total=t), stop=self.stop_ev.is_set)
            self.logs.append(f"برندها: {res['brands']} برند در این اجرا، {res['named']} نام‌گذاری با DeepSeek"
                             + (f"، ناموفق: {', '.join(res['failed'])}" if res["failed"] else ""))
        elif step == "publish":
            from crawler.publish import publish
            skus = [x for x in re.split(r"[\s,،]+", str(opts.get("skus") or "")) if x]
            res = publish(cfg, run, limit=int(opts.get("limit") or 0) if not skus else 0, skus=skus or None,
                          dry_run=bool(opts.get("dry_run")), republish=bool(opts.get("republish")),
                          progress=lambda d, t: self.prog.update(step="publish", done=d, total=t),
                          stop=self.stop_ev.is_set)
            self.logs.append("نتیجه: " + "، ".join(f"{k} {v}" for k, v in res["counts"].items() if v))
        elif step == "probe":
            from crawler import routing_state as RS
            res = RS.probe_batch(cfg, int(opts["batch"]),
                                 progress=lambda d, t: self.prog.update(step="probe", done=d, total=t),
                                 stop=self.stop_ev.is_set)
            log.info("probe batch %s: %s", opts["batch"], res)
        if not self.stop_ev.is_set() and self.prog.get("total"):
            self.prog["done"] = self.prog["total"]

    def _run(self, step: str, run: str, opts: dict) -> None:
        try:
            cfg = build_cfg()
            # "all" runs every step in order and stops at the first failure or a stop request
            steps = STEPS if step == "all" else [step]
            if step == "all" and opts.get("keep_queue"):
                steps = [x for x in STEPS if x != "queue"]
            if step == "all" and opts.get("resume_from") in steps:
                steps = steps[steps.index(opts["resume_from"]):]
            only = [x for x in (opts.get("steps") or []) if x in STEPS]
            if step == "all" and (opts.get("from_step") in STEPS or only):
                # re-run from a step (or only the listed steps, e.g. filter+dedupe+export without a new
                # top-up): their outputs are cleared, the others kept
                steps = only or STEPS[STEPS.index(opts["from_step"]):]
                prefixes = {"queue": "01_", "search": "02_", "crawl": "03_", "filter": "04_", "topup": "02_topup",
                            "dedupe": "05_", "images": "05_images", "describe": "05_descriptions", "prices": "05_price_detective",
                            "export": "06_"}
                d = run_path(run)
                for st in steps:
                    for f in d.glob(prefixes[st] + "*.csv"):
                        f.unlink()
                self.logs.append(f"re-run of «{'، '.join(steps)}»: other files kept")
            for st in steps:
                if self.stop_ev.is_set():
                    break
                if step == "all":
                    self.logs.append(f"—— {st} ——")
                t0 = time.time()
                self._one(cfg, st, run, opts)
                took = time.time() - t0
                self.logs.append(f"⏱ {st}: {int(took // 60)}:{int(took % 60):02d}")
                self.state.setdefault("timings", {})[st] = round(took)
            if step not in ("probe", "publish", "brands", "status", "resync"):
                for line in P.stats(cfg, run).splitlines():
                    self.logs.append(line)
            self.state["ok"] = not self.stop_ev.is_set()
        except Exception as e:
            self.state["ok"] = False
            self.state["error"] = str(e)
            self.logs.append("ERROR " + str(e))
            log.error(traceback.format_exc())
        finally:
            self.state["running"] = False
            self.state["ended"] = time.time()
            self._saved().unlink(missing_ok=True)

    def status(self) -> dict:
        return {**self.state, "progress": dict(self.prog), "logs": list(self.logs)[-200:]}


SITE_STEPS = {"publish", "status", "brands", "resync"}
JOBS = {"crawl": Job("crawl"), "site": Job("site")}
JOB = JOBS["crawl"]


def start_job(step: str, run: str, opts: dict) -> str | None:
    return JOBS["site" if step in SITE_STEPS else "crawl"].start(step, run, opts)
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


_ROWS_CACHE: dict = {}


def file_rows(p: Path) -> int:
    """Row count of a CSV, cached by size and time: the status page asks often and the
    crawl file grows large, so it is never parsed with pandas just to be counted."""
    try:
        st = p.stat()
    except OSError:
        return 0
    key = (str(p), st.st_size, st.st_mtime)
    hit = _ROWS_CACHE.get(str(p))
    if hit and hit[0] == key:
        return hit[1]
    n = 0
    try:
        with p.open(encoding="utf-8-sig", newline="") as fh:
            n = max(sum(1 for _ in csv.reader(fh)) - 1, 0)
    except Exception:
        n = 0
    _ROWS_CACHE[str(p)] = (key, n)
    return n


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


def _read(d: Path, name: str) -> pd.DataFrame:
    p = d / name
    if not p.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(p, encoding=ENCODING, dtype=str, keep_default_na=False)
    except Exception:
        return pd.DataFrame()


def run_kpi(d: Path) -> dict:
    c = _read(d, FILES["crawled"])
    priced = int((c["price_toman"] != "").sum()) if "price_toman" in c else 0
    return {"queue": len(_read(d, FILES["queue"])), "hits": len(_read(d, FILES["hits"])),
            "crawled": len(c), "priced": priced,
            "sellers": int(c["source_domain"].nunique()) if "source_domain" in c else 0,
            "filtered": len(_read(d, FILES["filtered"])), "rejected": len(_read(d, FILES["rejected"])),
            "new": len(_read(d, FILES["new"])), "updates": len(_read(d, FILES["updates"])),
            "review": len(_read(d, FILES["review"])), "woo": len(_read(d, FILES["woo"]))}


def sellers_of(d: Path) -> list[dict]:
    """Suppliers found in a run: the list for the 'invite suppliers' phase."""
    c = _read(d, FILES["crawled"])
    if c.empty:
        return []
    out = []
    for dom, g in c.groupby("source_domain"):
        phones = sorted({p for v in g["seller_phones"] for p in str(v).split(" | ") if p})
        prices = pd.to_numeric(g["price_toman"], errors="coerce")
        out.append({"seller": next((n for n in g["seller_name"] if n and n != dom), dom), "domain": dom,
                    "phones": " | ".join(phones), "products": len(g), "priced": int(prices.notna().sum()),
                    "categories": " | ".join(sorted({b.split(">")[-1].strip() for b in g["breadcrumb"] if b}))[:300],
                    "claims_representative": "yes" if (g["claims_representative"] == "yes").any() else "",
                    "brands": " | ".join(sorted({b for b in g["brand"] if b}))[:200],
                    "sample_url": g["url"].iloc[0]})
    return sorted(out, key=lambda r: -r["products"])


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


PEEK_DOMAINS = ("made-in-china.com", "torob.com")


def peek(url: str, find: list | None = None) -> dict:
    """Developer aid: fetch one page through the server and summarise its structure.
    Limited to Made-in-China and the configured or discovered source domains."""
    from crawler.extract import extract_page
    from crawler.fetch import domain_of
    cfg = build_cfg()
    dom = domain_of(url)
    allowed = set(PEEK_DOMAINS) | {s["domain"] for s in (cfg["sources"].get("iran_sites") or [])}
    disc = DATA_DIR / "discovered.csv"
    if disc.exists():
        allowed |= set(pd.read_csv(disc, encoding=ENCODING, dtype=str)["domain"])
    if not any(dom == d or dom.endswith("." + d) for d in allowed):
        return {"error": f"domain {dom} not allowed for peek"}
    f = Fetcher(cfg, "foreign")
    r = f.get(url, use_cache=False)
    if not r or r.get("status") != 200:
        return {"status": r.get("status") if r else None}
    html = r["text"]
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True)[:4000]:
        h = a["href"]
        if "/product" in h or "/prod/" in h:
            links.append([h[:200], (a.get("title") or a.get_text(" ", strip=True))[:80]])
    rec = extract_page(html, url)
    if rec:
        rec.pop("page_text", None)
    tables = []
    for t in soup.find_all("table")[:12]:
        rows = [[c.get_text(" ", strip=True)[:60] for c in tr.find_all(["th", "td"])][:4] for tr in t.find_all("tr")[:6]]
        tables.append({"class": " ".join(t.get("class") or []), "parent": " ".join(t.parent.get("class") or []),
                       "rows": rows})
    snippets = {}
    for word in (find or [])[:5]:
        i = html.find(word)
        if i >= 0:
            snippets[word] = html[max(0, i - 300):i + 1200]
    return {"status": 200, "length": len(html), "title": soup.title.get_text()[:150] if soup.title else "",
            "jsonld": html.count("application/ld+json"), "product_links": links[:40],
            "extracted": rec, "tables": tables, "snippets": snippets, "head": html[:3000]}


def backup_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k in ("taxonomy", "sources", "settings", "site"):
            p = paths()[k]
            if p.exists():
                z.write(p, p.name)
        for f in ("fx_rates.csv", "translations.csv", "routing.csv", "probes.csv", "discovered.csv",
                  "serper_usage.csv", "untranslated.csv"):
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
                       "fx_rates.csv", "translations.csv", "routing.csv", "probes.csv", "discovered.csv",
                       "serper_usage.csv", "untranslated.csv") or re.match(r"^runs/[\w\-]+/[\w\-.]+\.csv$", n)
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
        # product photos are public: the WooCommerce importer on the site fetches them
        if not APP_PASSWORD or self.path == "/healthz" or self.path.startswith("/img/"):
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
            if parts[:1] == ["img"] and len(parts) == 3 and re.fullmatch(r"[0-9a-f]{14}", parts[1]) \
                    and re.fullmatch(r"(ai\d_)?\d{1,2}\.webp", parts[2]):
                f = DATA_DIR / "images" / parts[1] / parts[2]
                if f.exists():
                    return self._send(200, f.read_bytes(), "image/webp")
                return self._send(404, b"not found", "text/plain")
            if parts[:2] == ["img", "_compare"] and len(parts) == 4 and re.fullmatch(r"[0-9a-f]{14}", parts[2]) \
                    and re.fullmatch(r"[a-z0-9\-]{3,60}_\d{1,2}\.webp", parts[3]):     # model comparison pictures
                f = DATA_DIR / "images" / "_compare" / parts[2] / parts[3]
                if f.exists():
                    return self._send(200, f.read_bytes(), "image/webp")
                return self._send(404, b"not found", "text/plain")
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if parts[:2] == ["api", "state"]:
                return self._json(self.state())
            if parts[:2] == ["api", "job"]:
                if q.get("lane") in JOBS:
                    return self._json(JOBS[q["lane"]].status())
                return self._json({**JOB.status(), "site": JOBS["site"].status()})
            if parts[:2] == ["api", "sources"]:
                txt = sources_text()
                return self._json({"yaml": txt, "data": yaml.safe_load(txt) or {}})
            if parts[:2] == ["api", "routing"]:
                from crawler import routing_state as RS
                from crawler.probe import total_usage
                cfg = build_cfg()
                if len(parts) == 2:
                    return self._json({**RS.summary(cfg), "serper_used": total_usage(cfg),
                                       "serper_key": bool(os.environ.get("SERPER_API_KEY"))})
                if parts[2] == "batch" and len(parts) == 4:
                    rows = RS.batch_rows(cfg, int(parts[3]))
                    if q.get("format") == "csv":
                        buf = io.StringIO()
                        rows.to_csv(buf, index=False)
                        return self._download(("\ufeff" + buf.getvalue()).encode("utf-8"),
                                              f"routing-batch-{parts[3]}.csv", "text/csv; charset=utf-8")
                    from crawler.probe import load_probes
                    pr = load_probes(cfg)
                    out = rows.to_dict("records")
                    for r in out:
                        p_ = pr.get(r["group"])
                        r["evidence"] = json.loads(p_["domains_json"])[:5] if p_ else []
                    return self._json({"rows": out})
            if parts[:3] == ["api", "wc", "product"] and len(parts) == 4:    # check what the site holds for one SKU
                from crawler.wc import WC
                w = WC(timeout=40)
                p_ = w.find_by_sku(parts[3]) if w.ok() else None
                if not p_:
                    return self._err("not found", 404)
                return self._json({k: p_.get(k) for k in ("id", "name", "status", "sku", "regular_price", "price",
                                                          "categories", "brands", "attributes", "images", "meta_data")})
            if parts[:3] == ["api", "wc", "brands"]:
                from crawler.wc import WC
                w = WC(timeout=40)
                if not w.ok():
                    return self._err("WC_URL / WC_KEY / WC_SECRET not set")
                terms = w.list_brands()
                return self._json({"brands": None if terms is None else
                                   [{"id": t.get("id"), "name": t.get("name"), "slug": t.get("slug"),
                                     "count": t.get("count")} for t in terms]})
            if parts[:2] == ["api", "no-category"] and parts[2:3] == ["download"]:   # the shared waiting list
                f = build_cfg()["_data_dir"] / "no_category.csv"
                if not f.exists():
                    return self._err("هنوز فهرستی ساخته نشده", 404)
                return self._download(f.read_bytes(), "caravan-no-category.csv", "text/csv; charset=utf-8")
            if parts[:2] == ["api", "brands"] and parts[2:3] == ["download"]:
                from crawler import brands_fa as BF
                p_ = BF.path(build_cfg())
                if not p_.exists():
                    return self._err("جدول برندها هنوز ساخته نشده", 404)
                return self._download(p_.read_bytes(), "caravan-brands.csv", "text/csv; charset=utf-8")
            if parts[:2] == ["api", "backup"]:
                return self._download(backup_zip(), f"caravan-backup-{time.strftime('%Y%m%d-%H%M')}.zip",
                                      "application/zip")
            if parts[:2] == ["api", "runs"] and len(parts) == 2:
                return self._json(list_runs())
            if parts[:2] == ["api", "runs"] and len(parts) >= 3:
                d = run_path(parts[2])
                if len(parts) == 3:
                    return self._json({"name": parts[2], "files": {f.name: file_rows(f) for f in sorted(d.glob("*.csv"))},
                                       "kpi": run_kpi(d), "stats": P.stats(build_cfg(), parts[2]) if d.exists() else ""})
                if parts[3] == "brands":
                    from crawler import brands_fa as BF
                    cfg = build_cfg()
                    return self._json({"rows": BF.for_run(cfg, [r for r, _ in P.export_rows(cfg, parts[2])])})
                if parts[3] == "review":
                    from crawler import review as RV
                    return self._json(RV.items(build_cfg(), parts[2], q.get("filter", "issues")))
                if parts[3] == "sellers":
                    rows = sellers_of(d)
                    if q.get("format") == "csv":
                        buf = io.StringIO()
                        pd.DataFrame(rows).to_csv(buf, index=False)
                        return self._download(("\ufeff" + buf.getvalue()).encode("utf-8"),
                                              f"{parts[2]}-sellers.csv", "text/csv; charset=utf-8")
                    return self._json({"rows": rows})
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
            if parts[:2] == ["api", "routing"] and len(parts) >= 3:
                from crawler import routing_state as RS
                cfg = build_cfg()
                body = self._jbody()
                if parts[2] == "build":
                    if taxonomy_df() is None:
                        return self._err("اول فایل تاکسونومی را بارگذاری کنید.")
                    return self._json(RS.build(cfg, mode=body.get("mode", "gap"), level1=body.get("level1") or None,
                                               path_contains=body.get("path") or None,
                                               include_flagged=bool(body.get("include_flagged")),
                                               batch_size=int(cfg.get("routing", {}).get("batch_size", 100))))
                if parts[2] == "probe" and len(parts) == 4:
                    if not os.environ.get("SERPER_API_KEY"):
                        return self._err("کلید SERPER_API_KEY روی سرور تنظیم نشده")
                    err = start_job("probe", f"routing", {"batch": int(parts[3])})
                    return self._err(err, 409) if err else self._json({"ok": True})
                if parts[2] == "review" and len(parts) == 4:
                    return self._json(RS.review_batch(cfg, int(parts[3]), body.get("rows") or []))
                if parts[2] == "queue" and len(parts) == 4:
                    run = f"batch-{int(parts[3])}"
                    res = RS.make_queue(cfg, int(parts[3]), run)
                    if body.get("start"):
                        err = start_job("all", run, {"keep_queue": True})
                        res["started"] = not err
                    return self._json(res)
                return self._err("not found", 404)
            if parts[:2] == ["api", "debug"] and parts[2:3] == ["peek"]:
                b = self._jbody(); return self._json(peek(b.get("url", ""), b.get("find")))
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
            if parts[:3] == ["api", "job", "stop"]:
                lane = self._jbody().get("lane")
                for k, j in JOBS.items():
                    if lane in (None, "", k):
                        j.stop_ev.set()
                return self._json({"ok": True})
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "resync":       # {"dry_run": true}
                run_path(parts[2])
                err = start_job("resync", parts[2], {"dry_run": self._jbody().get("dry_run", True) is not False})
                return self._err(err, 409) if err else self._json({"ok": True})
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "recategorize":
                # {"drops": [{url, taxonomy_id}], "fallback": {taxonomy_id: wp_id}, "dry_run": true}
                from crawler.publish import recategorize
                b = self._jbody()
                return self._json(recategorize(build_cfg(), parts[2], b.get("drops") or [], b.get("fallback") or {},
                                               dry_run=b.get("dry_run", True) is not False))
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "remove":   # {"urls": [...]}
                from crawler.publish import remove
                urls = [u for u in (self._jbody().get("urls") or []) if u]
                if not urls:
                    return self._err("urls missing")
                return self._json(remove(build_cfg(), parts[2], urls))
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "status":   # {"status": "publish"}
                run_path(parts[2])
                b = self._jbody()
                err = start_job("status", parts[2], {"status": b.get("status") or "publish", "skus": b.get("skus")})
                return self._err(err, 409) if err else self._json({"ok": True})
            if parts[:2] == ["api", "runs"] and len(parts) == 5 and parts[3:] == ["brands", "build"]:
                run_path(parts[2])
                err = start_job("brands", parts[2], {"redo": bool(self._jbody().get("redo"))})
                return self._err(err, 409) if err else self._json({"ok": True})
            if parts[:4] == ["api", "wc", "brands", "merge"]:     # {"from": id, "to": id, "dry_run": true}
                from crawler.wc import WC
                b = self._jbody()
                return self._json(WC(timeout=60).merge_brands(int(b["from"]), int(b["to"]),
                                                              dry_run=b.get("dry_run", True) is not False))
            if parts[:2] == ["api", "brands"] and len(parts) == 2:
                from crawler import brands_fa as BF
                return self._json({"ok": True, "saved": BF.edit(build_cfg(), self._jbody().get("rows") or [])})
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "publish":
                run_path(parts[2])
                b = self._jbody()
                opts = {k: b.get(k) for k in ("limit", "dry_run", "skus", "republish")}
                if not opts.get("skus") and not str(opts.get("limit") or "").isdigit():
                    return self._err("تعداد محصول را مشخص کنید")
                err = start_job("publish", parts[2], opts)
                return self._err(err, 409) if err else self._json({"ok": True})
            if parts[:3] == ["api", "wc", "test"]:
                from crawler.wc import WC
                w = WC(timeout=40)
                return self._json(w.test() if w.ok() else {"error": "WC_URL / WC_KEY / WC_SECRET not set"})
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "photo-fix":
                run_path(parts[2])
                err = start_job("photo_fix", parts[2], {})
                return self._err(err) if err else self._json({"ok": True})
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "vision-compare":
                run_path(parts[2])
                b = self._jbody()
                err = start_job("vision_compare", parts[2], {"models": b.get("models") or [], "n": b.get("n") or 25})
                return self._err(err) if err else self._json({"ok": True})
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "enhance":
                run_path(parts[2])
                b = self._jbody()
                err = start_job("enhance", parts[2], {k: b.get(k) for k in ("urls", "models", "n", "limit", "compare")})
                return self._err(err) if err else self._json({"ok": True})
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "describe-compare":
                run_path(parts[2])
                b = self._jbody()
                err = start_job("describe_compare", parts[2], {"models": b.get("models") or [], "n": b.get("n") or 10})
                return self._err(err) if err else self._json({"ok": True})
            if parts[:2] == ["api", "runs"] and len(parts) == 4 and parts[3] == "review":
                from crawler import review as RV
                run_path(parts[2])
                return self._json(RV.save_decision(build_cfg(), parts[2], self._jbody()))
            if parts[:2] == ["api", "runs"] and len(parts) == 5 and parts[3] == "step":
                run, step = parts[2], parts[4]
                run_path(run)
                if step not in STEPS and step != "all":
                    return self._err("unknown step")
                if step not in ("queue", "all") and not (run_path(run) / FILES["queue"]).exists():
                    return self._err("اول مرحله‌ی «صف» را اجرا کنید.")
                if step in ("queue", "all") and taxonomy_df() is None:
                    return self._err("اول فایل تاکسونومی را بارگذاری کنید.")
                err = start_job(step, run, self._jbody())
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
            if any(j.state["running"] and j.state["run"] == parts[2] for j in JOBS.values()):
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
            "keys": {k: bool(os.environ.get(k)) for k in ("SERPER_API_KEY", "LLM_API_KEY", "FX_API_KEY", "OPENROUTER_API_KEY",
                                                          "WC_URL", "WC_KEY", "WC_SECRET", "WP_APP_PASSWORD")},
            "settings": {
                "fx": {k: cfg["fx"].get(k) for k in ("provider", "manual_rate_toman", "rate_type", "url", "json_path", "unit")},
                "search": {"use_web_search": cfg["search"].get("use_web_search", False),
                           "max_hits_per_source": cfg["search"].get("max_hits_per_source", 8)},
                "http": {"delay_seconds": cfg["http"].get("delay_seconds"),
                         "proxy": cfg["http"].get("proxy") or {}},
                "llm": {k: cfg["llm"].get(k) for k in ("provider", "base_url", "model", "min_confidence")},
            },
            "runs": list_runs(),
            "job": {**JOB.status(), "site": JOBS["site"].status()},
            # data on Render is lost on restart unless DATA_DIR is on a persistent disk
            "ephemeral": bool(os.environ.get("RENDER")) and not os.path.ismount(DATA_DIR),
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
    for j in JOBS.values():                         # jobs cut short by a restart continue
        try:
            j.resume()
        except Exception:                           # noqa: BLE001 - the UI must come up regardless
            log.error(traceback.format_exc())
    srv.serve_forever()


if __name__ == "__main__":
    main()
