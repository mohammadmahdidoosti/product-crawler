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
If "brand" is already in Persian letters (a foreign brand as Iranians write it, e.g. "مونتاناری"),
return its original Latin name in "en" (Montanari) and keep the Persian spelling as given in "fa".
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


def save(cfg: dict, df: pd.DataFrame, normalized: bool = False) -> Path:
    df = df.reindex(columns=COLS).fillna("")
    if not normalized:
        df = normalize(df)
    df = df.drop_duplicates("source", keep="last").sort_values(["kind", "source"])
    return write_csv(df, path(cfg))


DOMAIN = re.compile(r"(^|[\s(])(www\.)?[\w\-]+(\.[\w\-]+)*\.(com|ir|net|org|co|shop|store|online|info|biz|io|me|site)"
                    r"(\.[a-z]{2})?($|[\s)/])", re.I)


def is_domain(b: str) -> bool:
    """'kalayefani.com', 'www.abzar.ir' or 'فروشگاه X (shop.ir)': a website, never a brand."""
    return bool(DOMAIN.search(str(b or "").strip()))


def kind_of(rec: dict) -> str:
    b = str(rec.get("brand") or "")
    if is_domain(b):
        return "shop"
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
            items = [{"i": i, "brand": r.get("ask") or r["source"], "company": r.get("from_company", False)}
                     for i, r in enumerate(chunk)]
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


# ---------------------------------------------------------------- one name per brand

NONE_PHRASES = {norm_text(x) for x in ("تمامی محصولات", "همه محصولات", "محصولات", "موتور گیربکس آسانسور",
                                        "لوازم آسانسور", "قطعات آسانسور", "متفرقه", "سایر", "عمومی")}
PREFIXES = re.compile(r"^(گروه\s*صنعتی|گروه\s*تولیدی|شرکت|برند|مارک|کارخانه)\s+")


def _fa_key(s: str) -> str:
    return re.sub(r"[\s\u200c\-_.]+", "", key(s))


def _en_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").casefold())


def _latin_case(en: str) -> str:
    en = str(en or "").strip()
    if not en:
        return ""
    if len(re.sub(r"[^A-Za-z]", "", en)) <= 3:
        return en.upper()                          # ABB, LS, AEG
    if en == en.lower():                           # 'ziehl-abegg' -> 'Ziehl-Abegg', 'mato drive' -> 'Mato Drive'
        return " ".join("-".join(p.capitalize() for p in w.split("-")) for w in en.split())
    return en


def known(text: str) -> tuple[str, str, str] | None:
    """(Persian name, Latin name, origin) of a brand from brands.csv found in the text."""
    from .routing import LATIN, find_brand
    t = re.sub(r"[\-_]+", " ", str(text or ""))
    t = re.sub(r"\d+", " ", t)
    f = find_brand(t)
    if not f:
        return None
    fa, origin = f
    en = LATIN.get(fa) or LATIN.get(norm_text(fa)) or ""
    return fa, _latin_case(en), origin or "unknown"


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Every spelling of one brand gets one name on the site.

    'RONIX', 'RONIX – رونیکس', 'رونیکس RONIX', 'رونیکس' and 'اره رونیکس' are one brand: rows are
    grouped by their Persian and Latin names (brands.csv recognises a brand inside a longer text)
    and the group shows one name: the brand's existing name on the site if it has one, else
    Persian only for an Iranian brand and 'فارسی (English)' for a foreign one. A hand-edited row
    sets the name of its whole group."""
    df = df.reindex(columns=COLS).fillna("").reset_index(drop=True)
    origin = [""] * len(df)
    for i, r in df.iterrows():
        if r["by"] == "manual":
            continue
        if r["kind"] == "iranian" or (r["kind"] == "foreign" and not r["en"] and FA_CHARS.search(r["source"])):
            raw = PREFIXES.sub("", re.sub(r"[\-_]+", " ", r["source"]).strip()).strip()
            if key(raw) in NONE_PHRASES:
                df.loc[i, ["kind", "fa", "en", "display"]] = ["shop", "", "", ""]
                continue
            k = known(raw)
            if k:
                df.loc[i, ["fa", "en"]] = [k[0], k[1] or r["en"]]
                origin[i] = k[2]
            else:
                fa_, en_ = split_display(raw)
                df.loc[i, ["fa", "en"]] = [fa_ or raw, en_ or r["en"]]
        elif r["fa"]:
            k = known(r["fa"])
            if k and _fa_key(k[0]) == _fa_key(r["fa"]):
                origin[i] = k[2]
        if origin[i] == "foreign" or (r["kind"] == "foreign" and origin[i] != "iranian"):
            origin[i] = origin[i] or "foreign"
    # group rows that share a Persian or a Latin name (union-find)
    parent = list(range(len(df)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    seen: dict[str, int] = {}
    for i, r in df.iterrows():
        if r["kind"] == "shop" and not (r["fa"] or r["en"]):
            continue
        for k in (("fa:" + _fa_key(r["fa"])) if _fa_key(r["fa"]) else "", ("en:" + _en_key(r["en"])) if _en_key(r["en"]) else ""):
            if not k:
                continue
            if k in seen:
                parent[find(i)] = find(seen[k])
            else:
                seen[k] = i
    groups: dict[int, list[int]] = {}
    for i in range(len(df)):
        if df.at[i, "kind"] == "shop" and not (df.at[i, "fa"] or df.at[i, "en"]):
            df.at[i, "display"] = ""
            continue
        groups.setdefault(find(i), []).append(i)
    w = pd.to_numeric(df["products"], errors="coerce").fillna(0)
    for members in groups.values():
        manual = [i for i in members if df.at[i, "by"] == "manual"]
        site = [df.at[i, "site"] for i in members if df.at[i, "site"]]
        if manual:
            m = max(manual, key=lambda i: df.at[i, "updated"])
            fa_, en_ = df.at[m, "fa"], df.at[m, "en"]
            disp = display(fa_, en_)
        else:
            def best(col):
                votes: dict[str, float] = {}
                for i in members:
                    v = str(df.at[i, col]).strip()
                    if v:
                        votes[v] = votes.get(v, 0) + w[i] + (0.5 if col == "en" and v != v.upper() else 0)
                return max(votes, key=votes.get) if votes else ""
            fa_, en_ = best("fa"), _latin_case(best("en"))
            iranian = any(origin[i] == "iranian" for i in members)
            disp = site[0] if site else (fa_ if iranian else display(fa_, en_))
            if site:
                fa_, en_ = split_display(site[0])[0] or fa_, split_display(site[0])[1] or en_
        for i in members:
            df.at[i, "display"] = disp
            if df.at[i, "by"] != "manual":
                df.at[i, "fa"], df.at[i, "en"] = fa_, en_
                if df.at[i, "kind"] == "shop":
                    df.at[i, "kind"] = "iranian" if FA_CHARS.search(fa_) and not en_ else "foreign"
    return df


def build(cfg: dict, recs: list[dict], redo: bool = False, wc=None, progress=None, stop=None) -> dict:
    """Add the run's brands to the table: Persian ones as they are, shops without a brand, the
    rest named by DeepSeek. Existing rows are kept (all of them unless redo; manual ones always)."""
    have = load(cfg)
    prev = {key(s): r for s, r in zip(have["source"], have.to_dict("records"))}
    found = collect(recs)
    now = time.strftime("%Y-%m-%d %H:%M")
    todo, out = [], dict(prev)
    for r in found.to_dict("records"):
        k = key(r["source"])
        old = prev.get(k)
        row = {**(old or {}), "source": r["source"], "kind": (old or {}).get("kind") or r["kind"],
               "suppliers": r["suppliers"] or (old or {}).get("suppliers", ""), "products": r["products"],
               "example": r["example"]}
        if old and old.get("by") != "manual" and r["kind"] == "shop" and old.get("kind") != "shop":
            old = None                                   # e.g. a website name the first build took for a brand
            row["kind"] = "shop"
        if old and (old.get("by") == "manual" or (not redo and (old.get("display") or old.get("kind") == "shop"))):
            out[k] = row
            continue
        if row["kind"] == "iranian":
            fa, en = split_display(r["source"])
            kn = known(r["source"])
            if kn and kn[2] == "foreign" and not (kn[1] or en):
                # a foreign brand written in Persian with no Latin name on file: DeepSeek gives it
                todo.append({**row, "source": r["source"], "fa": kn[0], "from_company": False, "ask": kn[0]})
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
            row = {kk: v for kk, v in t.items() if kk not in ("from_company", "ask")}
            if g and t.get("ask"):                        # Persian foreign brand: only its Latin name was asked
                row.update(en=g["en"], fa=t["fa"], display=display(t["fa"], g["en"]), by="deepseek", updated=now)
            elif g:
                row.update(en=g["en"], fa=g["fa"], display=display(g["fa"], g["en"]), by="deepseek", updated=now)
            elif t.get("ask"):
                row.update(fa=t["fa"], en=row.get("en", ""), by="rule", updated=now)
            else:
                row.update(en=row.get("en") or t["source"], fa=row.get("fa", ""), by=row.get("by") or "",
                           display=row.get("display") or "")
            out[k] = row
    df = pd.DataFrame(out.values()).reindex(columns=COLS).fillna("")
    terms = site_brands(wc) if wc is not None and wc.ok() else None
    if terms is not None:
        df["site"] = [((match_site(terms, f, e, d) or {}).get("name", "") if (d or f or e) else "")
                      for f, e, d in zip(df["fa"], df["en"], df["display"])]
    df = normalize(df)
    save(cfg, df, normalized=True)
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
    if not b or is_domain(b):                       # a website name is never sent as a brand
        return {"status": "none", "display": "", "fa": "", "en": "", "original": original, "source": b}
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
