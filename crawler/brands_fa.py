"""Brand names for the site: a short trade name with its Persian spelling, 'آنتو (Antu)'.

Customers say a brand on the phone, so every foreign brand gets the way an Iranian salesperson
would pronounce it, with the Latin name next to it. Chinese supplier names that stood in for a
missing brand ('Nanjing Antu Elevator Parts Co., Ltd.') are cut down to their distinctive part
('Antu'); the original company name stays in the table and in the product's meta, so a brand on
the site can always be traced back to the factory that sells it.

DATA_DIR/brands_fa.csv is the shared table (one row per brand string as the crawl wrote it):
  source      brand as in the crawl            kind       foreign | iranian | shop
  en, fa      short Latin name, Persian         display    what the site shows: 'fa (en)', or fa
  suppliers   original company / shop names     products   how many products use it (last build)
  example     one product name                   by         deepseek | rule | manual
  site        matching brand already on the site (filled when the table is built with WC keys)

Persian brands stay as they are; an Iranian shop that only stood in for a missing brand gets no
brand (kind 'shop', display empty) unless someone types one into the table. Rows edited by hand
(by = manual) are never overwritten.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import pandas as pd
import requests

from .config import secret
from .io import read_csv, write_csv
from .normalize import norm_text

log = logging.getLogger(__name__)

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
FILE = "brands_fa.csv"
COLS = ["source", "kind", "en", "fa", "display", "suppliers", "products", "example", "by", "site", "updated"]
FA_CHARS = re.compile(r"[؀-ۿ]")
LAT_CHARS = re.compile(r"[A-Za-z]")

SYSTEM = """You prepare brand names for an Iranian industrial marketplace. Customers phone the shop and
say the brand aloud, so each brand needs a short Latin trade name and its Persian spelling.

Input: JSON {"items":[{"i":0,"brand":"...","company":true|false}]}. company=true means the string is a
supplier's company name that stood in for a missing brand (usually a Chinese factory).

For each item return:
- "en": the short trade name in Latin letters, written the way the company writes it.
  From a company name drop the city/province/country (Nanjing, Dongguan, Chengdu, Ningbo, Wenzhou,
  Zhejiang, Guangdong, Shenzhen, Shanghai, Jiangsu, Hebei, China ...), the legal form (Co., Ltd.,
  Limited, Inc., Group, Corp.) and generic industry words (Elevator Parts, Sensor, Instrument(s),
  Technology/Technologies, Electric(al), Electronics, Machinery, Valve, Pump, Motor, Trading,
  Industrial, Industry, Manufacturing, Equipment, Automation, Import & Export, International).
  Keep only the distinctive name: "Nanjing Antu Elevator Parts" -> "Antu",
  "Mega-Tek Instrument (Chengdu)" -> "Mega-Tek", "Dongguan Dalang Aoer Sensor" -> "Aoer",
  "Bluefin Sensor Technologies" -> "Bluefin". If two distinctive words belong together keep both
  ("Hui Feng" -> "Huifeng" only if the company writes it as one word). Short all-caps brands stay
  all caps ("FUEN", "SKF"). Otherwise use normal capitalisation ("Antu", not "ANTU").
- "fa": the Persian spelling of "en" as an Iranian salesperson would say it: a transliteration of the
  sound, never a translation of the meaning. Use the established Persian spelling of well-known
  brands (Schneider -> اشنایدر, Siemens -> زیمنس, Bosch -> بوش, Makita -> ماکیتا, Delta -> دلتا,
  LS -> ال‌اس). Acronyms read letter by letter are written as the letters are said, joined with a
  half-space (SKF -> اس‌کا‌اف, ABB -> آ‌ب‌ب). Start with آ where the word starts with a long a sound.
  Pinyin: Antu -> آنتو, Aoer -> آئور, Huifeng -> هویفنگ, Xinda -> شین‌دا, Zhongtai -> جونگ‌تای.
Answer with JSON only: {"items":[{"i":0,"en":"Antu","fa":"آنتو"}]} with one entry per input item."""


def path(cfg: dict) -> Path:
    return Path(cfg.get("_data_dir") or os.environ.get("DATA_DIR") or "data") / FILE


def key(s: str) -> str:
    return norm_text(re.sub(r"\s+", " ", str(s or "")).strip()).casefold()


def display(fa: str, en: str) -> str:
    fa, en = str(fa or "").strip(), str(en or "").strip()
    if fa and en and key(fa) != key(en):
        return f"{fa} ({en})"
    return fa or en


def load(cfg: dict) -> pd.DataFrame:
    p = path(cfg)
    df = read_csv(p) if p.exists() else pd.DataFrame(columns=COLS)
    return df.reindex(columns=COLS).fillna("")


def table(cfg: dict) -> dict[str, dict]:
    return {key(r["source"]): r for r in load(cfg).to_dict("records")}


def save(cfg: dict, df: pd.DataFrame) -> Path:
    df = df.reindex(columns=COLS).fillna("")
    df["display"] = [d if k == "shop" and not (e or f) else display(f, e)
                     for d, k, f, e in zip(df["display"], df["kind"], df["fa"], df["en"])]
    df = df.drop_duplicates("source", keep="last").sort_values(["kind", "source"])
    return write_csv(df, path(cfg))


def kind_of(rec: dict) -> str:
    b = str(rec.get("brand") or "")
    if rec.get("brand_source") in ("supplier", "domain") and rec.get("source_type") != "foreign":
        return "shop"                                   # an Iranian shop's name, not a brand
    if FA_CHARS.search(b) and not LAT_CHARS.search(b):
        return "iranian"
    return "foreign"


def collect(recs: list[dict]) -> pd.DataFrame:
    """One row per brand string of a run's products, with the companies behind it."""
    rows: dict[str, dict] = {}
    for r in recs:
        b = re.sub(r"\s+", " ", str(r.get("brand") or "")).strip()
        if not b:
            continue
        k = key(b)
        x = rows.setdefault(k, {"source": b, "kind": kind_of(r), "suppliers": [], "products": 0,
                                "example": r.get("name_fa") or r.get("name") or ""})
        x["products"] += 1
        for s in str(r.get("sellers") or r.get("seller_name") or "").split(" | "):
            s = s.strip()
            if s and s not in x["suppliers"]:
                x["suppliers"].append(s)
    out = pd.DataFrame(rows.values(), columns=["source", "kind", "suppliers", "products", "example"])
    if len(out):
        out["suppliers"] = out["suppliers"].map(lambda v: " | ".join(v[:5]))
    return out


def split_display(name: str) -> tuple[str, str]:
    """'سناکوه (SKH)' -> ('سناکوه', 'SKH'); 'Makita' -> ('', 'Makita'); 'رونیکس' -> ('رونیکس', '')."""
    n = str(name or "").strip()
    m = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", n)
    if m:                                  # only a Persian part with a Latin part is 'fa (en)';
        a, b = m.group(1).strip(), m.group(2).strip()   # 'Mega-Tek Instrument (Chengdu)' is not
        if FA_CHARS.search(a) and LAT_CHARS.search(b) and not FA_CHARS.search(b):
            return a, b
        if FA_CHARS.search(b) and LAT_CHARS.search(a) and not FA_CHARS.search(a):
            return b, a
    return (n, "") if FA_CHARS.search(n) and not LAT_CHARS.search(n) else ("", n)


class Namer:
    """DeepSeek (through OpenRouter) in batches: short Latin name + Persian spelling."""

    def __init__(self, cfg: dict):
        bc = cfg.get("brands", {})
        self.key = secret("OPENROUTER_API_KEY")
        self.model = bc.get("model") or cfg.get("describe", {}).get("model") or "deepseek/deepseek-v4.1-flash"
        self.batch = int(bc.get("batch_size", 40))
        self.s = requests.Session()

    def ok(self) -> bool:
        return bool(self.key)

    def ask(self, items: list[dict]) -> dict[int, dict]:
        body = {"model": self.model, "temperature": 0.2, "max_tokens": 4000,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)}],
                "response_format": {"type": "json_object"}, "reasoning": {"enabled": False}}
        for attempt in range(3):
            try:
                res = self.s.post(OPENROUTER, json=body, timeout=120, headers={
                    "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                    "HTTP-Referer": "https://caravansanat.com", "X-Title": "Caravan crawler"})
            except requests.RequestException as e:
                log.info("brands openrouter: %s", e)
                time.sleep(3 * (attempt + 1))
                continue
            if res.status_code in (429, 500, 502, 503):
                time.sleep(5 * (attempt + 1))
                continue
            if res.status_code != 200:
                log.warning("brands openrouter HTTP %s: %s", res.status_code, res.text[:300])
                return {}
            text = ((res.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            try:
                got = json.loads(text).get("items") or []
            except (ValueError, AttributeError):
                log.info("brands: unparsable answer %s", text[:200])
                continue
            out = {}
            for g in got:
                try:
                    i = int(g.get("i"))
                except (TypeError, ValueError):
                    continue
                en, fa = str(g.get("en") or "").strip(), str(g.get("fa") or "").strip()
                if en and fa and FA_CHARS.search(fa) and not LAT_CHARS.search(fa) and len(en) <= 40:
                    out[i] = {"en": en, "fa": fa}
            return out
        return {}

    def name_all(self, rows: list[dict], progress=None, stop=None) -> dict[str, dict]:
        res: dict[str, dict] = {}
        for start in range(0, len(rows), self.batch):
            if stop and stop():
                break
            chunk = rows[start:start + self.batch]
            items = [{"i": i, "brand": r["source"], "company": r.get("from_company", False)} for i, r in enumerate(chunk)]
            got = self.ask(items)
            for i, r in enumerate(chunk):
                if i in got:
                    res[key(r["source"])] = got[i]
            if progress:
                progress(min(start + self.batch, len(rows)), len(rows))
        return res


def site_brands(wc) -> list[dict] | None:
    """Every brand term on the site, or None when the site has no brands endpoint."""
    try:
        return wc.list_brands()
    except Exception as e:                         # noqa: BLE001 - optional enrichment of the table
        log.warning("site brands not read: %s", e)
        return None


def match_site(terms: list[dict], fa: str, en: str, disp: str) -> dict | None:
    idx: dict[str, dict] = {}
    for t in terms or []:
        a, b = split_display(t.get("name", ""))
        for k in (t.get("name", ""), a, b):
            if key(k):
                idx.setdefault(key(k), t)
    for k in (en, disp, fa):
        if key(k) and key(k) in idx:
            return idx[key(k)]
    return None


def build(cfg: dict, recs: list[dict], redo: bool = False, wc=None, progress=None, stop=None) -> dict:
    """Add the run's brands to the table: Persian ones as they are, shops without a brand, the
    rest named by DeepSeek. Existing rows are kept (all of them unless redo; manual ones always)."""
    have = load(cfg)
    known = {key(s): r for s, r in zip(have["source"], have.to_dict("records"))}
    found = collect(recs)
    now = time.strftime("%Y-%m-%d %H:%M")
    todo, out = [], dict(known)
    for r in found.to_dict("records"):
        k = key(r["source"])
        old = known.get(k)
        row = {**(old or {}), "source": r["source"], "kind": (old or {}).get("kind") or r["kind"],
               "suppliers": r["suppliers"] or (old or {}).get("suppliers", ""), "products": r["products"],
               "example": r["example"]}
        if old and (old.get("by") == "manual" or (not redo and (old.get("display") or old.get("kind") == "shop"))):
            out[k] = row
            continue
        if row["kind"] == "iranian":
            fa, en = split_display(r["source"])
            row.update(fa=fa, en=en, display=display(fa, en), by="rule", updated=now)
        elif row["kind"] == "shop":
            row.update(fa="", en="", display="", by="rule", updated=now)
        else:
            fa, en = split_display(r["source"])
            if fa and en:                                  # already 'سناکوه (SKH)'
                row.update(fa=fa, en=en, display=display(fa, en), by="rule", updated=now)
            else:
                todo.append({**row, "from_company": (r.get("suppliers") or "").startswith(r["source"])
                             or bool(re.search(r"\b(co|ltd|limited|inc|group|technolog|elevator|sensor|"
                                               r"instrument|machinery|electric|industr|trading)\w*", r["source"], re.I))})
        out[k] = row
    named = {}
    if todo:
        nm = Namer(cfg)
        if not nm.ok():
            raise ValueError("OPENROUTER_API_KEY روی سرور تنظیم نشده (برای نام فارسی برندها)")
        log.info("brands: %d foreign brand(s) to DeepSeek (%s)", len(todo), nm.model)
        named = nm.name_all(todo, progress=progress, stop=stop)
        for t in todo:
            k = key(t["source"])
            g = named.get(k)
            row = {kk: v for kk, v in t.items() if kk != "from_company"}
            if g:
                row.update(en=g["en"], fa=g["fa"], display=display(g["fa"], g["en"]), by="deepseek", updated=now)
            else:
                row.update(en=row.get("en") or t["source"], fa=row.get("fa", ""), by=row.get("by") or "",
                           display=row.get("display") or "")
            out[k] = row
    df = pd.DataFrame(out.values()).reindex(columns=COLS).fillna("")
    terms = site_brands(wc) if wc is not None and wc.ok() else None
    if terms is not None:
        df["site"] = [((match_site(terms, f, e, d) or {}).get("name", "") if d else "")
                      for f, e, d in zip(df["fa"], df["en"], df["display"])]
    save(cfg, df)
    missing = [t["source"] for t in todo if key(t["source"]) not in named]
    res = {"brands": len(found), "named": len(named), "failed": missing[:20], "site_brands": None if terms is None else len(terms)}
    log.info("brands: %s", res)
    return res


def for_run(cfg: dict, recs: list[dict]) -> list[dict]:
    """Table rows of the brands used by these products, most used first."""
    t = table(cfg)
    found = collect(recs)
    rows = []
    for r in found.to_dict("records"):
        x = t.get(key(r["source"]))
        rows.append({**(x or {"source": r["source"], "kind": r["kind"], "en": "", "fa": "", "display": "",
                             "suppliers": r["suppliers"], "by": "", "site": ""}),
                     "products": r["products"], "example": r["example"], "in_table": bool(x)})
    return sorted(rows, key=lambda x: (x["in_table"], -int(x["products"] or 0)))


def edit(cfg: dict, rows: list[dict]) -> int:
    """Hand edits from the review table: en / fa (or an empty pair = no brand)."""
    df = load(cfg)
    pos = {key(s): i for i, s in enumerate(df["source"])}
    now = time.strftime("%Y-%m-%d %H:%M")
    n = 0
    for r in rows:
        k = key(r.get("source"))
        if not k:
            continue
        en, fa = str(r.get("en") or "").strip(), str(r.get("fa") or "").strip()
        new = {"en": en, "fa": fa, "display": display(fa, en), "by": "manual", "updated": now}
        if k in pos:
            for c, v in new.items():
                df.at[pos[k], c] = v
        else:
            df = pd.concat([df, pd.DataFrame([{"source": r["source"], "kind": r.get("kind") or "foreign", **new}])],
                           ignore_index=True)
        n += 1
    save(cfg, df)
    return n


# ---------------------------------------------------------------- applying to a product

def resolve(rec: dict, t: dict[str, dict]) -> dict:
    """{'status': ok | none | missing, 'display', 'fa', 'en', 'original'} for one product."""
    b = re.sub(r"\s+", " ", str(rec.get("brand") or "")).strip()
    original = (str(rec.get("sellers") or rec.get("seller_name") or "").split(" | ")[0]).strip()
    if not b:
        return {"status": "none", "display": "", "fa": "", "en": "", "original": original}
    x = t.get(key(b))
    if not x:
        return {"status": "missing", "display": "", "fa": "", "en": "", "original": original, "source": b}
    if not x.get("display"):
        return {"status": "none", "display": "", "fa": "", "en": "", "original": original, "source": b}
    return {"status": "ok", "display": x["display"], "fa": x.get("fa", ""), "en": x.get("en", ""),
            "original": original, "source": b}


def has_brand(name: str, br: dict) -> bool:
    n = key(name)
    for w in (br.get("fa"), br.get("en"), br.get("source")):
        w = key(w)
        if not w:
            continue
        if LAT_CHARS.search(w):
            if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", n):
                return True
        elif w in n:
            return True
    return False


def name_with_brand(name: str, br: dict) -> str:
    """'فلوتر آسانسوری یک اینچ' -> 'فلوتر آسانسوری یک اینچ برند آنتو (Antu)', unless the name already says it."""
    name = str(name or "").strip()
    if br.get("status") != "ok" or has_brand(name, br):
        return name
    return f"{name} برند {br['display']}"
