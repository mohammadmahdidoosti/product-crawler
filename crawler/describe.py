"""Product descriptions, written fresh (never the source's text).

For each new product (05_new_products.csv) -> 05_descriptions.csv (url, html, short, method, model):
  * with OPENROUTER_API_KEY: a language model on OpenRouter (model in config describe.model) writes a
    Persian description from the product's facts and the source text, under strict rules (no copying,
    no invented specs, no prices / shop names / phone numbers / links); its output is checked and any
    answer that breaks the rules falls back to the template;
  * otherwise: a template built from the facts, with a few sentence variants so pages do not all read
    the same.
The specs table is added by the export step under either text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

import pandas as pd
import requests

from .config import run_dir, secret
from .io import FILES, Appender, read_csv, write_csv

log = logging.getLogger(__name__)
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
DESC_COLS = ["url", "html", "short", "method", "model", "tokens_in", "tokens_out"]
JUNK_KEYS = {"customized", "condition", "certification", "warranty", "after-sales service", "after sales service",
             "transport package", "origin", "hs code", "trademark", "production capacity", "specification",
             "packaging", "supply ability", "port", "payment terms"}


# ------------------------------------------------------------------ facts

def clean_specs(r: dict) -> dict:
    """The product's specs, Persian names first; percent-encoded duplicates and trade boilerplate removed."""
    raw = {}
    for col in ("specs_fa_json", "specs_json"):
        try:
            raw = json.loads(r.get(col) or "{}")
        except ValueError:
            raw = {}
        if raw:
            break
    out: dict[str, str] = {}
    seen_vals = set()
    for k, v in raw.items():
        k, v = re.sub(r"[\u064B-\u0652]", "", str(k)).strip(), str(v).strip()
        if "%" in k:                                       # 'brand' saved twice, once URL-encoded
            k = unquote(k).replace("-", " ").strip()
        if not k or not v or len(v) > 160 or k.lower() in JUNK_KEYS:
            continue
        if (k.lower(), v) in seen_vals or k in out:
            continue
        seen_vals.add((k.lower(), v))
        out[k] = v
    return out


def facts(r: dict) -> dict:
    return {
        "name": r.get("name_fa") or r.get("name", ""),
        "category": str(r.get("breadcrumb") or "").split(">")[-1].strip(),
        "path": str(r.get("breadcrumb") or ""),
        "brand": r.get("brand", ""),
        "model": r.get("model", ""),
        "specs": clean_specs(r),
        "source_text": re.sub(r"\s+", " ", str(r.get("description") or r.get("short_description") or ""))[:1800],
    }


# ------------------------------------------------------------------ template (free)

def _pick(options: list[str], seed: str) -> str:
    return options[int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(options)]


def template(r: dict) -> tuple[str, str]:
    f = facts(r)
    name, cat, brand = f["name"], f["category"], f["brand"]
    by = f" ساخت {brand}" if brand else ""
    intro = _pick([
        f"{name}{by} یکی از گزینه‌های دسته‌ی {cat} در فروشگاه کاروان صنعت است.",
        f"{name}{by} برای کسانی است که به دنبال یک {cat} قابل‌اعتماد هستند.",
        f"در این صفحه مشخصات و قیمت {name}{by} را می‌بینید؛ محصولی از دسته‌ی {cat}.",
    ], name)
    specs = list(f["specs"].items())
    key = [f"{k} {v}" for k, v in specs if k not in ("برند", "مدل") and re.search(r"[\u0600-\u06FF]", k)
           and len(v) < 60][:4]
    lead = _pick(["از ویژگی‌های اصلی آن: ", "مهم‌ترین مشخصات این محصول: ", "مشخصات کلیدی: "], name + "f")
    feat = lead + "، ".join(key) + "." if key else ""
    close = _pick([
        "برای اطمینان از سازگاری با نیاز خود، جدول مشخصات فنی زیر را ببینید و در صورت نیاز با کارشناسان کاروان صنعت مشورت کنید.",
        "مشخصات کامل در جدول زیر آمده است؛ برای انتخاب مدل مناسب می‌توانید با کارشناسان کاروان صنعت تماس بگیرید.",
        "اگر درباره‌ی انتخاب این محصول سؤالی دارید، کارشناسان کاروان صنعت راهنمایی‌تان می‌کنند.",
    ], name + "c")
    html = f"<p>{intro} {feat}</p><p>{close}</p>"
    return html, intro


# ------------------------------------------------------------------ language model (OpenRouter)

SYSTEM = """You write product descriptions in Persian (Farsi) for Caravan Sanat (کاروان صنعت), an Iranian online store of
industrial equipment and tools. Write fresh text; never copy sentences from the source text.
Rules:
- Persian only (model codes, units and brand names in Latin are fine). Natural, clear, professional; no hype.
- Use only facts given in the input (name, brand, model, specs, source text). Never invent a number, spec,
  certificate, warranty or country of origin. If a fact is uncertain, leave it out.
- No prices, no shop or seller names (other than the brand), no phone numbers, no links, no emojis.
- Leave out trade terms of the source: minimum order (MOQ), packing, shipping, payment, sample, factory
  capacity or the factory's own certificates and customers.
- Translate technical terms into Persian (keep the Latin term in brackets only when it helps); numbers
  with their units.
- 180 to 300 words: a fuller introduction, the features as a list, and real uses of the product.
- Open the introduction with what this product does or what sets it apart, in your own words. Do not
  open with stock phrases such as "یکی از اتصالات پرکاربرد", "یکی از اجزای پرکاربرد/کلیدی/کاربردی" or
  "از ... پرکاربرد در"; thousands of the shop's pages would then start the same way.
Return JSON only: {"short": "<one or two sentences, max 220 characters>",
 "html": "<p>intro</p><h3>ویژگی‌ها</h3><ul><li>…</li></ul><h3>کاربردها</h3><p>…</p>"}
Use only the tags p, h3, ul, li, strong."""


FIX_NUMBERS = ("در متنت عدد {n} آمده که در اسم و مشخصات این محصول نیست. همان متن را دوباره بنویس و هیچ عددی "
               "ننویس مگر عددهایی که عیناً در اسم یا مشخصات محصول آمده‌اند؛ اندازه، توان، زاویه یا ظرفیتی را که "
               "در اسم نیست حدس نزن و به‌جایش کلی بنویس. همان قالب JSON.")


class Writer:
    def __init__(self, cfg: dict, model: str | None = None):
        dc = cfg.get("describe", {})
        self.key = secret("OPENROUTER_API_KEY")
        self.model = model or dc.get("model", "deepseek/deepseek-v4.1-flash")
        self.max_tokens = int(dc.get("max_tokens", 2500))
        self.s = requests.Session()
        self.usage = {"in": 0, "out": 0, "calls": 0, "failed": 0}
        self.spent = False
        import threading
        self._tl = threading.local()           # .last = 'rejected' when the model answered but broke a rule

    def ok(self) -> bool:
        return bool(self.key)

    def write(self, r: dict) -> dict | None:
        if self.spent:                           # the key or the account has no money left: no more calls
            return None
        f = facts(r)
        user = json.dumps({k: f[k] for k in ("name", "path", "brand", "model", "specs", "source_text")},
                          ensure_ascii=False)
        body = {"model": self.model, "max_tokens": self.max_tokens, "temperature": 0.6,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"},
                "reasoning": {"enabled": False}}                 # no hidden 'thinking' tokens: plain writing
        retried = False
        for attempt in range(4):
            try:
                res = self.s.post(OPENROUTER, json=body, timeout=120, headers={
                    "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                    "HTTP-Referer": "https://caravansanat.com", "X-Title": "Caravan crawler"})
            except requests.RequestException as e:
                log.info("openrouter: %s", e)
                time.sleep(3 * (attempt + 1))
                continue
            if res.status_code in (429, 500, 502, 503):
                time.sleep(5 * (attempt + 1))
                continue
            if res.status_code != 200:
                if res.status_code in (402, 403) and ("limit" in res.text.lower() or "credit" in res.text.lower()):
                    # the key's spending limit or the account's credit is spent: every other call would
                    # fail the same way, so stop asking and say it plainly
                    if not self.spent:
                        log.error("OpenRouter: سقف کلید یا اعتبار حساب تمام شده (%s) — سقف کلید را در "
                                  "openrouter.ai/settings/keys بالا ببرید یا حساب را شارژ کنید", res.text[:160])
                    self.spent = True
                self.usage["failed"] += 1
                return None
            data = res.json()
            u = data.get("usage") or {}
            self.usage["in"] += int(u.get("prompt_tokens") or 0)
            self.usage["out"] += int(u.get("completion_tokens") or 0)
            self.usage["calls"] += 1
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            out = parse_answer(text)
            why = "unparsable" if not out else check_why(out, r)
            if out and not why:
                out["tokens_in"], out["tokens_out"] = u.get("prompt_tokens", ""), u.get("completion_tokens", "")
                return out
            log.info("description rejected (%s, finish=%s) for %s", why,
                     ((data.get("choices") or [{}])[0]).get("finish_reason"), r.get("url"))
            m = re.match(r"number (\S+) not in the facts", why or "")
            if m and not retried:
                # once more, told what was wrong (29 Oct: 146 of 155 rejections were a number the model
                # knew for such products, e.g. '90' for an elbow, but the name does not say)
                retried = True
                body = {**body, "messages": body["messages"] + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": FIX_NUMBERS.format(n=m.group(1))}]}
                continue
            self._tl.last = "rejected"
            self.usage["failed"] += 1
            return None
        self.usage["failed"] += 1
        return None


def parse_answer(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except ValueError:
        return None
    html, short = str(j.get("html") or ""), str(j.get("short") or "")
    html = re.sub(r"<(?!/?(p|h3|ul|li|strong)\b)[^>]*>", "", html)       # only the allowed tags
    return {"html": html.strip(), "short": short.strip()[:300]} if html else None


def check(out: dict, r: dict) -> bool:
    return not check_why(out, r)


def check_why(out: dict, r: dict) -> str:
    """'' when the model's answer keeps the rules, else the broken rule."""
    text = re.sub(r"<[^>]+>", " ", out["html"])
    fa = len(re.findall(r"[؀-ۿ]", text))
    if fa < 0.5 * max(1, len(re.sub(r"\s", "", text))):
        return "not Persian"
    if re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text + " " + str(out.get("short") or "")):
        return "Chinese characters"            # DeepSeek slips a Chinese word in now and then (14 of 25,000)
    words = len(text.split())
    if words < 100 or words > 450:
        return f"{words} words"
    if re.search(r"https?://|www\.|\.ir\b|\.com\b|09\d{9}|۰۹[۰-۹]{9}|تومان|ریال|حداقل سفارش|\bMOQ\b", text):
        return "link/phone/price"
    shop = str(r.get("seller_name") or r.get("source_name") or "").strip()
    if shop and len(shop) > 3 and shop in text and shop != str(r.get("brand") or ""):
        return "shop name"
    # every number the model wrote (two digits or more, or a decimal) must be in the product's own facts
    from .normalize import fa_digits_to_en
    f = facts(r)
    known = fa_digits_to_en(" ".join([f["name"], f["model"], json.dumps(f["specs"], ensure_ascii=False),
                                      str(r.get("description") or ""), str(r.get("short_description") or ""),
                                      str(r.get("specs_json") or ""), f["category"]]))
    known_nums = set(re.findall(r"\d+(?:[.,/]\d+)?", known)) | set(re.findall(r"\d+", known))
    for n in re.findall(r"\d+(?:[.,/]\d+)?", fa_digits_to_en(text)):
        if (len(n) >= 2 or not n.isdigit()) and n not in known_nums and n.replace(",", ".") not in known_nums:
            return f"number {n} not in the facts"
    src = re.sub(r"\s+", " ", str(r.get("description") or ""))
    for sent in re.split(r"[.!؟?\n]", text):               # no sentence copied from the source
        sent = sent.strip()
        if len(sent) > 60 and sent in src:
            return "copied sentence"
    return ""


# ------------------------------------------------------------------ step

def step_describe(cfg: dict, run: str, limit: int | None = None) -> Path | None:
    from .pipeline import _progress
    dc = cfg.get("describe", {})
    d = run_dir(cfg, run)
    if not (d / FILES["new"]).exists():
        return None
    new = read_csv(d / FILES["new"])
    out_p = d / "05_descriptions.csv"
    w = Writer(cfg)
    use_llm = dc.get("provider", "auto") in ("auto", "openrouter") and w.ok()
    have = set()
    if out_p.exists():
        prev = read_csv(out_p)
        # products that only got the template are tried again when a model is available
        have = set(prev[prev["method"] != "template"]["url"]) if use_llm else set(prev["url"])
    tries_p = d / "05_describe_tries.csv"                 # template-only products: how often the models failed
    tries = {}
    if tries_p.exists():
        t = read_csv(tries_p)
        tries = {u: int(n or 0) for u, n in zip(t["url"], t["tries"])}
    if use_llm:                                         # after 2 failed rounds the template stays: no more spend
        have |= {u for u, n in tries.items() if n >= int(dc.get("max_tries", 2))}
    rows = [r for r in new.to_dict("records") if r["url"] not in have][:limit]
    backups = [Writer(cfg, m) for m in (dc.get("fallback_models") or [])] if use_llm else []
    log.info("descriptions: %d products, %s", len(rows), f"OpenRouter {w.model}" if use_llm else "template (free)")

    def one(r):
        tried = False
        if use_llm:
            for x in [w] + backups:                    # the main model, then the backups, then the template
                x._tl.last = ""
                res = x.write(r)
                if res:
                    return {"url": r["url"], **res, "method": "ai", "model": x.model}
                tried |= getattr(x._tl, "last", "") == "rejected"
        html, short = template(r)
        # only a model's rejected answer counts as a try; an outage or a spent key does not
        return {"url": r["url"], "html": html, "short": short, "method": "template", "model": "",
                "tokens_in": "", "tokens_out": "", "_tried": tried}

    with Appender(out_p, DESC_COLS) as a, ThreadPoolExecutor(int(dc.get("workers", 4)) if use_llm else 1) as ex:
        for i, res in enumerate(ex.map(one, rows), 1):
            tried = res.pop("_tried", False)
            a.write(res)
            if use_llm and res["method"] == "template" and tried:
                tries[res["url"]] = tries.get(res["url"], 0) + 1
            if _progress("describe", i, len(rows)):
                break
    if tries:
        write_csv(pd.DataFrame({"url": list(tries), "tries": [str(n) for n in tries.values()]}), tries_p)
    if use_llm:
        u = w.usage
        log.info("descriptions: %s calls, %d failed, tokens in %d / out %d%s",
                 u["calls"], u["failed"], u["in"], u["out"],
                 "".join(f"; backup {b.model}: {b.usage['calls']} calls, {b.usage['failed']} failed" for b in backups))
    return out_p


def compare(cfg: dict, run: str, models: list[str], n: int = 10) -> Path:
    """The same n products written by each model, side by side (06_describe_compare.csv)."""
    d = run_dir(cfg, run)
    new = read_csv(d / FILES["new"])
    has = new[new["description"].fillna("") != ""]
    fa = has[has["source_type"] != "foreign"].head((n + 1) // 2)
    fo = has[has["source_type"] == "foreign"].head(n - len(fa))
    sample = pd.concat([fa, fo]).to_dict("records") or new.head(n).to_dict("records")
    rows = []
    for m in models:
        w = Writer(cfg, m)
        t0 = time.time()
        with ThreadPoolExecutor(4) as ex:
            res = list(ex.map(w.write, sample))
        for r, x in zip(sample, res):
            rows.append({"model": m, "name": r.get("name_fa") or r.get("name"), "url": r["url"],
                         "short": (x or {}).get("short", ""), "html": (x or {}).get("html", ""),
                         "ok": bool(x), "tokens_in": (x or {}).get("tokens_in", ""),
                         "tokens_out": (x or {}).get("tokens_out", "")})
        log.info("compare %s: %d/%d ok, %.0fs, tokens %s", m, sum(1 for x in res if x), len(res),
                 time.time() - t0, w.usage)
    for r in sample:
        html, short = template(r)
        rows.append({"model": "template", "name": r.get("name_fa") or r.get("name"), "url": r["url"],
                     "short": short, "html": html, "ok": True, "tokens_in": "", "tokens_out": ""})
    p = d / "06_describe_compare.csv"
    pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8-sig")
    return p
