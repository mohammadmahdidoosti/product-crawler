"""A supplier's price list -> the site's prices (the owner's rule, 3 Nov 2026).

Customers called about wrong prices; the suppliers then sent their price lists (poster pictures,
PDF or Excel). One run ('pl-<name>', folder DATA_DIR/pricelists/<run>) takes one such list:

  read    every file -> rows {brand, model, name, price, unit}: pictures and PDF pages by the vision
          model (one call per picture/page, about a tenth of a cent), Excel/CSV by its columns.
          The same model on several pictures keeps the newest list's price; a disagreement is shown.
  match   the site, read only: products whose name or SKU holds the model code (a code followed by
          another digit is another model), the brand's other products, the site's own toman rate.
          Plan rows (plan.csv):
            update   on the site, the list's price differs  -> the list price, locked
            same     on the site, already the list price     -> only locked
            brand    the same, and its brand term is wrong   -> brand corrected as well
            new      in the list, not on the site            -> a crawl (search) run for it
            spread   the brand's products not in the list whose copies of one model differ a lot
                     in price (one of them is surely wrong) -> 'pending' and first in the slow Torob check
            other    the brand's other products               -> left alone (reported)
  apply   (site lane) the price is written in dollars (toman / the site's rate), with metas
          _caravan_price_lock (the crawler never writes another price there, see wc.price_locked),
          _caravan_price_toman and _caravan_price_date; every product's former values are kept in
          backup.jsonl. A change bigger than `big_ratio` is applied only when asked (a misread price
          or a wrong match is likelier there). Models of the list are kept in DATA_DIR/pricelists/index.json,
          so a product the crawler brings later gets the list price at publish (publish.list_price).
  new     the 'new' rows -> a search run (01_queue.csv); crawled and published like any batch.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from pathlib import Path
from statistics import median

import pandas as pd

from .io import read_csv, write_csv

log = logging.getLogger(__name__)
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
ITEM_COLS = ["brand", "model", "key", "name", "price", "unit", "toman", "list_date", "file", "note"]
PLAN_COLS = ["action", "key", "model", "brand", "list_name", "list_toman", "product_id", "product_name", "status",
             "site_toman", "change_pct", "site_brand", "big", "note"]
MONTHS = {m: i for i, m in enumerate(["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان",
                                      "آذر", "دی", "بهمن", "اسفند"], 1)}
# a product holding the model code but being only a part or a consumable of it
PARTS = ("باتری", "شارژر", "آرمیچر", "آرمیچر", "ذغال", "زغال", "کلید", "قطعه", "تیغه", "سوزن", "کابل", "شلنگ", "شیلنگ",
         "قاب", "دسته", "چرخ", "بلبرینگ", "سیم پیچ", "استاتور", "گیربکس", "پیستون", "واشر", "اورینگ", "لنت", "نازل")
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

VISION_SYSTEM = """You read Iranian supplier price lists (posters, catalog pages, tables).
Return JSON: {"list_date": "<month and year printed, e.g. آبان ۱۴۰۵, or empty>",
 "rows": [{"brand": "<maker as printed, Latin if printed in Latin>", "model": "<model code exactly as printed>",
           "name": "<Persian product name as printed>", "price": "<the price digits exactly as printed>",
           "unit": "rial|toman|unknown"}]}
One row per product that has a printed price. Copy digits exactly (Persian digits may be written in Latin).
The unit comes from the words near the price (ریال = rial, تومان = toman). Leave out cartons, counts,
phone numbers, watts and other numbers that are not the price. No prose, only the JSON."""


# ------------------------------------------------------------------ places and small helpers

def pdir(cfg: dict) -> Path:
    d = Path(cfg["_data_dir"]) / "pricelists"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_path(cfg: dict, run: str) -> Path:
    if not re.fullmatch(r"pl-[\w\-]{1,60}", run or ""):
        raise ValueError("نام اجرا باید با pl- شروع شود (حروف، عدد و خط تیره)")
    d = pdir(cfg) / run
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings(cfg: dict) -> dict:
    st = {"big_ratio": 2.5, "spread_ratio": 1.4, "same_pct": 1.0, "max_files": 80,
          "model": cfg.get("images", {}).get("vision_model") or "deepseek/deepseek-v4.1-flash"}
    st.update(cfg.get("pricelist") or {})
    return st


def runs(cfg: dict) -> list[dict]:
    out = []
    for d in sorted(pdir(cfg).glob("pl-*"), key=lambda p: p.stat().st_mtime, reverse=True):
        out.append({"run": d.name, "files": len(list((d / "files").glob("*"))) if (d / "files").exists() else 0,
                    "items": (d / "items.csv").exists(), "plan": (d / "plan.csv").exists(),
                    "applied": (d / "applied.csv").exists()})
    return out


def add_file(cfg: dict, run: str, data: bytes, filename: str) -> dict:
    d = run_path(cfg, run) / "files"
    d.mkdir(exist_ok=True)
    name = re.sub(r"[^\w.\-]+", "_", Path(filename or "file").name)[-80:] or "file"
    if len(list(d.glob("*"))) >= int(settings(cfg)["max_files"]):
        raise ValueError("تعداد فایل‌های این اجرا به سقف رسیده")
    if name.lower().endswith((".zip",)):
        import zipfile
        n = 0
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for i in z.infolist():
                if i.is_dir() or not i.filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".pdf", ".xlsx", ".csv")):
                    continue
                (d / re.sub(r"[^\w.\-]+", "_", Path(i.filename).name)[-80:]).write_bytes(z.read(i))
                n += 1
        return {"files": n}
    (d / name).write_bytes(data)
    return {"files": 1}


def model_key(s) -> str:
    """'VR2402-BCK', 'vr 2402 bck' and 'VR2402BCK' are one model."""
    return re.sub(r"[^A-Z0-9]", "", str(s or "").translate(FA_DIGITS).upper())


def contains_model(text: str, key: str) -> bool:
    """The model code inside a product's name/SKU, its parts maybe split by a space or dash ('VR2010 BLX');
    'VR2010' is not inside 'VR20105' (another model), and 'VR2010-BLX ۲۰ ولت' still holds VR2010BLX."""
    if len(key) < 3 or not re.search(r"\d", key):
        return False
    t = str(text or "").translate(FA_DIGITS).upper()
    pat = r"(?<![A-Z0-9])" + r"[\s\-_/.]?".join(map(re.escape, key)) + r"(?![0-9])"
    return re.search(pat, t) is not None


def to_toman(price, unit: str) -> int | None:
    raw = re.sub(r"\D", "", str(price or "").translate(FA_DIGITS))
    if not raw:
        return None
    v = int(raw)
    if unit == "rial":
        v //= 10
    return v if 10_000 <= v <= 20_000_000_000 else None


def list_date_rank(s: str) -> int:
    """'آبان ۱۴۰۵' -> 140508, for 'the newest list wins'; 0 when unknown."""
    s = str(s or "").translate(FA_DIGITS)
    y = re.search(r"1[34]\d\d", s)
    m = next((v for k, v in MONTHS.items() if k in s), 0)
    return int(y.group(0)) * 100 + m if y else 0


def _clean_html(s: str) -> str:
    import html
    return html.unescape(re.sub(r"<[^>]+>", " ", str(s or ""))).strip()


# ------------------------------------------------------------------ step: read

def _vision(cfg: dict, png: bytes, mime: str = "image/jpeg") -> tuple[dict | None, float]:
    import requests
    from .config import secret
    key = secret("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("کلید OpenRouter روی سرور نیست")
    body = {"model": settings(cfg)["model"], "max_tokens": 3000, "temperature": 0,
            "reasoning": {"enabled": False}, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": VISION_SYSTEM},
                         {"role": "user", "content": [
                             {"type": "text", "text": "Read every priced product of this price list."},
                             {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(png).decode()}}]}],
            "usage": {"include": True}}
    for attempt in range(3):
        try:
            r = requests.post(OPENROUTER, json=body, timeout=180, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "HTTP-Referer": "https://caravansanat.com", "X-Title": "Caravan price list"})
        except requests.RequestException as e:
            log.info("pricelist vision: %s", e)
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503):
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code != 200:
            log.warning("pricelist vision: HTTP %s %s", r.status_code, r.text[:200])
            return None, 0.0
        j = r.json()
        usd = float((j.get("usage") or {}).get("cost") or 0)
        text = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return (json.loads(m.group(0)) if m else None), usd
        except ValueError:
            return None, usd
    return None, 0.0


def _images_of(f: Path) -> list[tuple[str, bytes, str]]:
    """(label, picture bytes, mime) of one file: a picture as it is, a PDF page by page."""
    low = f.name.lower()
    if low.endswith((".jpg", ".jpeg")):
        return [(f.name, f.read_bytes(), "image/jpeg")]
    if low.endswith((".png", ".webp")):
        return [(f.name, f.read_bytes(), "image/png" if low.endswith(".png") else "image/webp")]
    if low.endswith(".pdf"):
        import pdfplumber
        out = []
        with pdfplumber.open(str(f)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                buf = io.BytesIO()
                page.to_image(resolution=130).original.convert("RGB").save(buf, "JPEG", quality=85)
                out.append((f"{f.name} ص{i}", buf.getvalue(), "image/jpeg"))
        return out
    return []


def _table_rows(f: Path) -> list[dict]:
    """Excel/CSV: the model, name and price columns found by their headers."""
    df = pd.read_excel(f) if f.name.lower().endswith((".xlsx", ".xlsm")) else read_csv(f)
    cols = {str(c): str(c).strip().lower() for c in df.columns}

    def pick(*words):
        return next((c for c, l in cols.items() if any(w in l for w in words)), None)
    cm, cn, cp = pick("مدل", "کد", "model", "code"), pick("نام", "شرح", "عنوان", "name", "product"), \
        pick("قیمت", "price", "مبلغ")
    if not cm or not cp:
        raise ValueError(f"{f.name}: ستون مدل یا قیمت پیدا نشد")
    unit = "rial" if "ریال" in cp else "toman" if "تومان" in cp else "unknown"
    return [{"brand": "", "model": str(r[cm]), "name": str(r[cn]) if cn else "", "price": str(r[cp]), "unit": unit}
            for r in df.to_dict("records") if str(r.get(cm) or "").strip() and str(r.get(cp) or "").strip()]


def read(cfg: dict, run: str, default_unit: str = "auto", brand: str = "", progress=None) -> dict:
    """Every file of the run -> items.csv (one row per model)."""
    d = run_path(cfg, run)
    files = sorted((d / "files").glob("*")) if (d / "files").exists() else []
    if not files:
        raise ValueError("فایلی بارگذاری نشده")
    cache_p = d / "read_cache.json"
    cache = json.loads(cache_p.read_text()) if cache_p.exists() else {}
    rows, usd, calls, failed = [], 0.0, 0, []
    for k, f in enumerate(files):
        if progress:
            progress(k, len(files))
        if f.stat().st_size == 0:
            failed.append(f"{f.name}: فایل خالی است")
            continue
        try:
            if f.name.lower().endswith((".xlsx", ".xlsm", ".csv")):
                rows += [{**r, "list_date": "", "file": f.name} for r in _table_rows(f)]
                continue
            for label, pic, mime in _images_of(f):
                import hashlib
                h = hashlib.sha1(pic).hexdigest()
                if h not in cache:
                    j, c = _vision(cfg, pic, mime)
                    usd, calls = usd + c, calls + 1
                    if j is None:
                        failed.append(f"{label}: خوانده نشد")
                        continue
                    cache[h] = j
                    cache_p.write_text(json.dumps(cache, ensure_ascii=False))
                j = cache[h]
                for r in j.get("rows") or []:
                    if isinstance(r, dict):
                        rows.append({"brand": str(r.get("brand") or ""), "model": str(r.get("model") or ""),
                                     "name": str(r.get("name") or ""), "price": str(r.get("price") or ""),
                                     "unit": str(r.get("unit") or "unknown"), "list_date": str(j.get("list_date") or ""),
                                     "file": label})
        except Exception as e:                           # noqa: BLE001 - one file never stops the others
            log.warning("pricelist read %s: %s", f.name, e)
            failed.append(f"{f.name}: {e}"[:200])
    items = merge_rows(rows, default_unit, brand)
    write_csv(items, d / "items.csv", ITEM_COLS)
    (d / "read.json").write_text(json.dumps({"files": len(files), "rows": len(rows), "items": len(items),
                                             "usd": round(usd, 4), "calls": calls, "failed": failed,
                                             "at": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False))
    log.info("PRICELIST read %s: %d files, %d rows, %d models, $%.4f", run, len(files), len(rows), len(items), usd)
    return {"files": len(files), "rows": len(rows), "items": len(items), "usd": round(usd, 4), "failed": failed}


def merge_rows(rows: list[dict], default_unit: str = "auto", brand: str = "") -> pd.DataFrame:
    """One row per model; the newest list's price wins, a different price on the same date is noted."""
    by: dict[str, dict] = {}
    for r in rows:
        k = model_key(r["model"])
        if len(k) < 3 or not re.search(r"\d", k):
            continue
        unit = r["unit"] if r["unit"] in ("rial", "toman") else (default_unit if default_unit in ("rial", "toman") else "")
        t = to_toman(r["price"], unit) if unit else None
        note = "" if unit else "واحد قیمت معلوم نیست"
        if unit and t is None:
            note = "قیمت خوانده‌شده معقول نیست"
        row = {"brand": brand or r.get("brand", ""), "model": r["model"].strip(), "key": k, "name": r.get("name", "").strip(),
               "price": r["price"], "unit": unit, "toman": t or "", "list_date": r.get("list_date", ""),
               "file": r.get("file", ""), "note": note}
        old = by.get(k)
        if old is None:
            by[k] = row
            continue
        a, b = list_date_rank(old["list_date"]), list_date_rank(row["list_date"])
        if (b > a and row["toman"]) or (not old["toman"] and row["toman"]):
            row["note"] = "؛ ".join(x for x in (row["note"], f"در لیست قدیمی‌تر {old['toman']}" if old["toman"] and old["toman"] != row["toman"] else "") if x)
            by[k] = row
        elif b == a and row["toman"] and old["toman"] and row["toman"] != old["toman"]:
            old["note"] = "؛ ".join(x for x in (old["note"], f"تصویر دیگر {row['toman']} خوانده شد؛ بررسی کنید") if x)
    return pd.DataFrame(list(by.values()), columns=ITEM_COLS)


def items(cfg: dict, run: str) -> pd.DataFrame:
    p = run_path(cfg, run) / "items.csv"
    return read_csv(p).reindex(columns=ITEM_COLS, fill_value="").fillna("") if p.exists() else pd.DataFrame(columns=ITEM_COLS)


def edit_items(cfg: dict, run: str, rows: list[dict]) -> int:
    """A person corrects a misread row: [{key, toman?, model?, name?, delete?}]."""
    df = items(cfg, run)
    n = 0
    for e in rows:
        m = df["key"] == str(e.get("key"))
        if not m.any():
            continue
        if e.get("delete"):
            df = df[~m]
        else:
            for c in ("toman", "model", "name", "brand"):
                if c in e:
                    df.loc[m, c] = re.sub(r"\D", "", str(e[c]).translate(FA_DIGITS)) if c == "toman" else str(e[c])
            if "model" in e:
                df.loc[m, "key"] = model_key(e["model"])
            df.loc[m, "note"] = "اصلاح دستی"
        n += 1
    write_csv(df, run_path(cfg, run) / "items.csv", ITEM_COLS)
    return n


# ------------------------------------------------------------------ step: match (read-only)

def site_rate(wc, pid: int) -> float | None:
    """The site's own toman-per-dollar figure: one product's shown (view) price over its stored price."""
    a = wc._call(lambda: wc._wc("GET", f"products/{pid}", params={"context": "edit"}), "rate edit")
    b = wc._call(lambda: wc._wc("GET", f"products/{pid}", params={"context": "view"}), "rate view")
    try:
        usd, toman = float(a.get("regular_price") or 0), float(b.get("regular_price") or b.get("price") or 0)
    except (TypeError, ValueError):
        return None
    return toman / usd if usd > 0 and toman > usd * 1000 else None


def _search(wc, term: str) -> list[dict]:
    out, page = [], 1
    while page <= 5:
        j = wc._call(lambda: wc._wc("GET", "products", params={"search": term, "status": "any", "per_page": 100,
                                                               "page": page, "context": "edit"}), f"search {term}")
        if not isinstance(j, list) or not j:
            break
        out += j
        if len(j) < 100:
            break
        page += 1
    return out


def _brand_products(wc, brand_id: int) -> list[dict]:
    """The brand's products with their stored (dollar) prices: the default 'view' answer of this site
    carries toman prices."""
    out, page = [], 1
    while page <= 50:
        j = wc._call(lambda: wc._wc("GET", "products", params={"brand": brand_id, "status": "any", "per_page": 100,
                                                               "page": page, "context": "edit"}), "brand products")
        if not isinstance(j, list) or not j:
            break
        out += [p for p in j if brand_id in [b.get("id") for b in p.get("brands") or []]]
        if len(j) < 100:
            break
        page += 1
    return out


def _brand_terms(wc, items_df: pd.DataFrame, cfg: dict) -> dict[str, dict]:
    """Brand of the list (as printed) -> the site's brand term, or {} when none matches."""
    from .catalog_publish import _brand
    from .brands_fa import match_site
    terms = wc.list_brands() or []
    out = {}
    for b in sorted({str(x).strip() for x in items_df["brand"] if str(x).strip()}):
        br = _brand(cfg, b)
        t = match_site(terms, br.get("fa", ""), br.get("en", ""), br.get("display", b))
        out[b] = {"resolved": br, "term": t}
    return out


GENERIC = {"دستگاه", "مدل", "سری", "کیت", "ست", "جدید", "حرفه‌ای", "صنعتی", "برقی", "پرتابل"}


def weak_key(key: str) -> bool:
    """A code of digits only ('50-160', '90-315': a size range in a list of welding machines) is also a size
    of pipes, jacks and gauges: it alone never makes a match."""
    return key.isdigit()


def kind_in(product_name: str, list_name: str) -> bool:
    """The list row's kind of thing (its first real word: 'جوش' of 'دستگاه جوش پلی اتیلن …') is in the
    product's name too."""
    from .normalize import norm_text
    words = [w for w in norm_text(list_name).split() if w not in GENERIC and len(w) > 1 and not re.search(r"\d", w)]
    name = norm_text(_clean_html(product_name))
    return bool(words) and all(w in name.split() for w in words[:2])


def _is_part(product_name: str, list_name: str) -> bool:
    n = _clean_html(product_name)
    return any(w in n and w not in list_name for w in PARTS) and not n.startswith(("کیت", "ست"))


def _brand_ok(p: dict, term: dict | None, resolved: dict) -> bool:
    """The product carries the list's brand (its site term, or a term named like it)."""
    from .brands_fa import key
    have = p.get("brands") or []
    if term:
        return term["id"] in [b.get("id") for b in have]
    want = {key(x) for x in (resolved.get("fa"), resolved.get("en"), resolved.get("display")) if key(x)}
    if not want:
        return True                                  # the list names no brand: nothing to judge
    return any(key(b.get("name", "")) in want or any(w in key(b.get("name", "")) for w in want) for b in have)


def match(cfg: dict, run: str, wc=None, progress=None) -> dict:
    from .wc import WC
    wc = wc or WC()
    if not wc.ok():
        raise ValueError("WC_URL / WC_KEY / WC_SECRET روی سرور تنظیم نشده")
    st = settings(cfg)
    it = items(cfg, run)
    it = it[it["toman"].astype(str).str.strip() != ""]
    if it.empty:
        raise ValueError("ردیف قیمت‌داری در لیست نیست؛ اول «خواندن» را اجرا کنید")
    brands = _brand_terms(wc, it, cfg)
    seen: dict[int, dict] = {}
    rows, rate = [], None
    keys = list(it.to_dict("records"))
    for k, r in enumerate(keys):
        if progress:
            progress(k, len(keys) + 1)
        hits = {}
        for term in dict.fromkeys([r["model"], r["model"].replace("-", " "), r["key"]]):
            for p in _search(wc, term):
                if contains_model(f"{p.get('name')} {p.get('sku')}", r["key"]) and not _is_part(p.get("name"), r["name"]) \
                        and (not weak_key(r["key"]) or kind_in(p.get("name"), r["name"])):
                    hits[p["id"]] = p
        binfo = brands.get(str(r["brand"]).strip()) or {}
        bt, bres = binfo.get("term"), binfo.get("resolved") or {}
        if not hits:
            rows.append({"action": "new", "key": r["key"], "model": r["model"], "brand": r["brand"],
                         "list_name": r["name"], "list_toman": int(r["toman"]), "note": r["note"]})
            continue
        for pid, p in hits.items():
            seen[pid] = p
            if rate is None and p.get("regular_price"):
                rate = site_rate(wc, pid)
            pb = [b.get("name", "") for b in p.get("brands") or []]
            usd = float(p.get("regular_price") or 0) if p.get("type") != "variable" else 0
            rows.append({"action": "update", "key": r["key"], "model": r["model"], "brand": r["brand"],
                         "list_name": r["name"], "list_toman": int(r["toman"]), "product_id": pid,
                         "product_name": _clean_html(p.get("name")), "status": p.get("status", ""),
                         "_usd": usd, "site_brand": "، ".join(pb),
                         "_brand_ok": _brand_ok(p, bt, bres),
                         "_type": p.get("type"), "note": r["note"]})
    if rate is None:
        raise ValueError("نرخ تبدیل سایت خوانده نشد")
    out = []
    for x in rows:
        if x["action"] == "update":
            if x.pop("_type") == "variable":
                x.update(action="skip", note="محصول متغیر است؛ قیمت دستی")
            site_t = round(x.pop("_usd") * rate, -3)
            x["site_toman"] = int(site_t)
            x["change_pct"] = round((x["list_toman"] / site_t - 1) * 100, 1) if site_t else ""
            ratio = x["list_toman"] / site_t if site_t else 99
            x["big"] = "yes" if ratio > st["big_ratio"] or ratio < 1 / st["big_ratio"] else ""
            if x["action"] == "update" and site_t and abs(ratio - 1) * 100 < st["same_pct"]:
                x["action"] = "same"
            if not x.pop("_brand_ok") and x["action"] != "skip":
                x["note"] = "؛ ".join(v for v in (x["note"], "برند روی سایت اشتباه است") if v)
                x["action"] = "brand" if x["action"] == "same" else x["action"]
                x["fix_brand"] = "yes"
        out.append(x)
    # the brands' other products: copies of one model far apart in price
    for name, b in brands.items():
        t = b.get("term")
        if not t:
            continue
        try:
            prods = _brand_products(wc, int(t["id"]))
        except Exception as e:                           # noqa: BLE001 - the report says so
            log.warning("pricelist brand %s: %s", name, e)
            continue
        others = [p for p in prods if p["id"] not in seen and p.get("type") != "variable" and p.get("regular_price")]
        groups: dict[str, list] = {}
        for p in others:
            m = re.search(r"[A-Z]{1,4}[\- ]?\d{2,5}[A-Z0-9\-]*", _clean_html(p.get("name")).upper())
            groups.setdefault(model_key(m.group(0)) if m else f"_{p['id']}", []).append(p)
        for g, ps in groups.items():
            prices = [float(p["regular_price"]) for p in ps]
            med = median(prices)
            for p in ps:
                v = float(p["regular_price"])
                far = len(ps) > 1 and not g.startswith("_") and (v / med > st["spread_ratio"] or med / v > st["spread_ratio"]
                                                                 or (len(ps) == 2 and max(prices) / min(prices) > st["spread_ratio"]))
                out.append({"action": "spread" if far else "other", "key": g, "model": g if not g.startswith("_") else "",
                            "brand": name, "product_id": p["id"], "product_name": _clean_html(p.get("name")),
                            "status": p.get("status", ""), "site_toman": int(round(v * rate, -3)),
                            "note": f"{len(ps)} نسخه با قیمت‌های {', '.join(f'{round(x * rate / 1e6, 2)}M' for x in sorted(prices))}"
                            if far else ""})
    plan = pd.DataFrame(out)
    for c in PLAN_COLS + ["fix_brand"]:
        if c not in plan:
            plan[c] = ""
    plan = plan.fillna("")
    d = run_path(cfg, run)
    write_csv(plan, d / "plan.csv", PLAN_COLS + ["fix_brand"])
    upd = plan[plan["action"].isin(["update", "same", "brand"])]
    ratios = [r["list_toman"] / r["site_toman"] for r in upd.to_dict("records") if r["site_toman"]]
    summ = {"rate": rate, "counts": plan["action"].value_counts().to_dict(), "big": int((plan["big"] == "yes").sum()),
            "median_change_pct": round((median(ratios) - 1) * 100, 1) if ratios else None,
            "brands": {k: (v["term"] or {}).get("name", "") for k, v in brands.items()},
            "at": time.strftime("%Y-%m-%d %H:%M")}
    (d / "match.json").write_text(json.dumps(summ, ensure_ascii=False))
    log.info("PRICELIST match %s: %s", run, summ)
    return summ


def plan(cfg: dict, run: str) -> pd.DataFrame:
    p = run_path(cfg, run) / "plan.csv"
    return read_csv(p).fillna("") if p.exists() else pd.DataFrame(columns=PLAN_COLS)


# ------------------------------------------------------------------ step: apply (site lane)

def _index_path(cfg: dict) -> Path:
    return pdir(cfg) / "index.json"


def load_index(cfg: dict) -> dict:
    p = _index_path(cfg)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def brand_names(cfg: dict, brand: str) -> list[str]:
    """The brand as it may be written in a product name (Persian and Latin)."""
    if not str(brand or "").strip():
        return []
    try:
        from .catalog_publish import _brand
        br = _brand(cfg, brand)
        names = [brand, br.get("fa", ""), br.get("en", ""), br.get("display", "")]
    except Exception:                                    # noqa: BLE001 - the printed name is enough
        names = [brand]
    return sorted({_flat(x) for x in names if _flat(x)})


def _flat(s) -> str:
    return re.sub(r"[\s\u200c\-]+", "", str(s or "")).lower()


def save_index(cfg: dict, run: str, it: pd.DataFrame) -> int:
    idx = load_index(cfg)
    day = time.strftime("%Y-%m-%d")
    n, names = 0, {}
    for r in it.to_dict("records"):
        if str(r.get("toman") or "").strip() and len(r["key"]) >= 4:
            b = str(r.get("brand") or "")
            if b not in names:
                names[b] = brand_names(cfg, b)
            idx[r["key"]] = {"toman": int(float(r["toman"])), "model": r["model"], "name": r.get("name", ""),
                             "brand": b, "brand_names": names[b],
                             "run": run, "date": day, "list_date": r.get("list_date", "")}
            n += 1
    _index_path(cfg).write_text(json.dumps(idx, ensure_ascii=False))
    return n


def list_price(cfg: dict, name: str, sku: str = "", idx: dict | None = None) -> dict | None:
    """A model of an applied price list inside a product's name (for publish); the longest code wins."""
    idx = load_index(cfg) if idx is None else idx
    best, flat = None, _flat(name)
    for k, v in idx.items():
        if not contains_model(f"{name} {sku}", k):
            continue
        bn = v.get("brand_names") or []
        if bn and not any(b in flat for b in bn):
            continue                                     # '3151' of Ronix is not a number in another maker's name
        if weak_key(k) and not (bn or kind_in(name, v.get("name", ""))):
            continue                                     # digits alone, no brand to check: not enough
        if best is None or len(k) > len(best[0]):
            best = (k, v)
    return best[1] if best else None


def lock_meta(run: str, toman: int, day: str = "") -> list[dict]:
    day = day or time.strftime("%Y-%m-%d")
    return [{"key": "_caravan_price_lock", "value": f"{run} {day}"},
            {"key": "_caravan_price_toman", "value": str(int(toman))},
            {"key": "_caravan_price_date", "value": day},
            {"key": "_caravan_price_estimated", "value": ""}]


def apply(cfg: dict, run: str, wc=None, include_big: bool = False, spread: bool = True, progress=None, stop=None) -> dict:
    from .publish import SiteBrands
    from .wc import WC
    wc = wc or WC()
    if not wc.ok():
        raise ValueError("WC_URL / WC_KEY / WC_SECRET روی سرور تنظیم نشده")
    d = run_path(cfg, run)
    pl = plan(cfg, run)
    if pl.empty:
        raise ValueError("اول «تطبیق با سایت» را اجرا کنید")
    rate = float(json.loads((d / "match.json").read_text())["rate"])
    todo = pl[pl["action"].isin(["update", "same", "brand"]) & ((pl["big"] != "yes") | include_big)]
    sp = pl[pl["action"] == "spread"] if spread else pl.iloc[0:0]
    brands = SiteBrands(wc)
    terms = _brand_terms(wc, todo, cfg) if len(todo) else {}
    items_ = []
    for r in todo.to_dict("records"):
        usd = round(float(r["list_toman"]) / rate, 4)
        x = {"id": int(float(r["product_id"])), "regular_price": f"{usd:.4f}", "sale_price": "",
             "meta_data": lock_meta(run, int(float(r["list_toman"])))}
        if r.get("fix_brand") == "yes":
            br = (terms.get(str(r["brand"]).strip()) or {}).get("resolved")
            bid = brands.id_for(br)[0] if br else None
            if bid:
                x["brands"] = [{"id": int(bid)}]
        items_.append((r, x))
    for r in sp.to_dict("records"):
        if r["status"] == "publish":
            items_.append((r, {"id": int(float(r["product_id"])), "status": "pending"}))
    done, failed = [], []
    bk = d / "backup.jsonl"
    for i in range(0, len(items_), 50):
        if stop and stop():
            break
        chunk = items_[i:i + 50]
        if progress:
            progress(i, len(items_))
        ids = [x["id"] for _, x in chunk]
        try:
            cur = wc._call(lambda: wc._wc("GET", "products", params={"include": ",".join(map(str, ids)), "per_page": 100,
                                                                     "status": "any", "context": "edit"}), "backup")
            with bk.open("a", encoding="utf-8") as fh:
                for p in cur:
                    fh.write(json.dumps({"id": p["id"], "regular_price": p.get("regular_price"),
                                         "sale_price": p.get("sale_price"), "status": p.get("status"),
                                         "brands": p.get("brands"), "at": time.strftime("%Y-%m-%d %H:%M")},
                                        ensure_ascii=False) + "\n")
            j = wc.batch_update([x for _, x in chunk])
        except Exception as e:                           # noqa: BLE001 - recorded, the rest go on
            failed += [{"product_id": x["id"], "error": str(e)[:200]} for _, x in chunk]
            continue
        bad = {p.get("id"): p["error"] for p in j.get("update") or [] if p.get("error")}
        for r, x in chunk:
            if x["id"] in bad:
                failed.append({"product_id": x["id"], "error": str(bad[x["id"]])[:200]})
            else:
                done.append({"product_id": x["id"], "action": r["action"], "model": r["model"],
                             "name": r.get("product_name", ""), "site_toman": r.get("site_toman", ""),
                             "price_toman": r.get("list_toman", ""), "status": x.get("status", ""),
                             "brand_fixed": "yes" if "brands" in x else ""})
    it = items(cfg, run)
    indexed = save_index(cfg, run, it)
    write_csv(pd.DataFrame(done + [{**f, "action": "error"} for f in failed]), d / "applied.csv",
              ["product_id", "action", "model", "name", "site_toman", "price_toman", "status", "brand_fixed", "error"])
    res = {"priced": sum(1 for x in done if x["action"] in ("update", "same", "brand")),
           "pending": sum(1 for x in done if x["status"] == "pending"),
           "brand_fixed": sum(1 for x in done if x["brand_fixed"]), "failed": len(failed),
           "held_big": int(((pl["big"] == "yes") & pl["action"].isin(["update", "same", "brand"])).sum()) if not include_big else 0,
           "indexed_models": indexed}
    log.info("PRICELIST apply %s: %s", run, res)
    return res


# ------------------------------------------------------------------ step: new products -> a crawl run

def queue_new(cfg: dict, run: str) -> dict:
    """The list's models not on the site -> DATA_DIR/runs/<pl-run>-new/01_queue.csv (not started here)."""
    from .config import run_dir
    from .io import QUEUE_COLS
    from .searches import TaxIndex, load_tax
    from .taxonomy import resolve_route
    tax = load_tax(cfg)
    if tax is None:
        raise ValueError("اول فایل تاکسونومی را بارگذاری کنید")
    pl = plan(cfg, run)
    new = pl[pl["action"] == "new"]
    if new.empty:
        raise ValueError("محصول تازه‌ای در این لیست نیست")
    idx, by_id = TaxIndex(tax, cfg), tax.set_index("taxonomy_id")
    rows, skipped = [], []
    from .brands_fa import split_display
    from .routing import leaf_brand
    from .normalize import norm_text
    for r in new.to_dict("records"):
        words = set(norm_text(str(r["list_name"])).split())
        # a leaf of another maker ('دستگاه جوش co2 صبا', 'پروفیل بر aeg') or of a part ('کلید دریل') would
        # turn the crawl to other products: the leaf's own first word must be in the list's name
        # every word of the leaf (numbers aside) must be in the list's name: 'دریل شارژی' is not a 'دریل چکشی'
        m = [x for x in idx.match(str(r["list_name"]), n=25)
             if not leaf_brand(x["leaf"]) and all(w in words or re.fullmatch(r"[\d.]+", w)
                                                  for w in norm_text(x["leaf"]).split())]
        if not m:
            skipped.append(r["model"])
            continue
        t = by_id.loc[m[0]["taxonomy_id"]].copy()
        t["taxonomy_id"] = m[0]["taxonomy_id"]
        fa, en = split_display(str(r["brand"]))
        rows.append({"taxonomy_id": m[0]["taxonomy_id"], "query": f"{r['model']} {fa or en or r['brand']}".strip(),
                     "breadcrumb": t["breadcrumb"], "leaf": t["leaf"], "level1": t["level1"],
                     "route": resolve_route(t, cfg.get("routes") or {}), "site_product_count": int(t["site_product_count"]),
                     "priority": len(rows), "extra_queries": f"{r['list_name']} {r['model']}",
                     "from_searches": f"price list {run}", "searches": 0})
    if not rows:
        raise ValueError("برای هیچ‌کدام دسته‌ای پیدا نشد")
    name = f"{run}-new"
    write_csv(pd.DataFrame(rows), run_dir(cfg, name) / "01_queue.csv", QUEUE_COLS + ["extra_queries", "from_searches", "searches"])
    return {"run": name, "queued": len(rows), "no_category": skipped}


def summary(cfg: dict, run: str) -> dict:
    d = run_path(cfg, run)
    out = {"run": run, "files": sorted(p.name for p in (d / "files").glob("*")) if (d / "files").exists() else []}
    for k in ("read", "match"):
        p = d / f"{k}.json"
        out[k] = json.loads(p.read_text()) if p.exists() else None
    out["items"] = items(cfg, run).to_dict("records")
    out["plan"] = plan(cfg, run).to_dict("records")
    p = d / "applied.csv"
    out["applied"] = read_csv(p).fillna("").to_dict("records") if p.exists() else []
    return out
