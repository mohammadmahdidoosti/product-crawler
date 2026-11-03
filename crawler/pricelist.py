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
EXTRA_COLS = ["fix_brand", "keep_id", "stock"]
MONTHS = {m: i for i, m in enumerate(["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان",
                                      "آذر", "دی", "بهمن", "اسفند"], 1)}
# a product holding the model code but being only a part or a consumable of it
PARTS = ("باتری", "شارژر", "آرمیچر", "آرمیچر", "ذغال", "زغال", "کلید", "قطعه", "تیغه", "سوزن", "کابل", "شلنگ", "شیلنگ",
         "قاب", "دسته", "چرخ", "بلبرینگ", "سیم پیچ", "استاتور", "گیربکس", "پیستون", "واشر", "اورینگ", "لنت", "نازل")
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

VISION_SYSTEM = """You read Iranian supplier price lists (posters, catalog pages, tables).
Return JSON: {"list_date": "<date or month and year printed, e.g. آبان ۱۴۰۵ or 1405/08/01, or empty>",
 "brand": "<the maker or supplier named on the page, as printed, or empty>",
 "rows": [{"model": "<the product's model code ONLY when one is printed as an identifier (letters and digits such as
            VR2402-BCK, LE-710, P3355); empty when the row has no model code. Never put a size, a size range
            (90-315, 1/2 اینچ) or a row number here>",
           "name": "<the row's full product name exactly as printed, with its sizes and variant words
            (دستی / نیمه هیدرولیک / تمام هیدرولیک, دو فک / چهار فک ...)>",
           "brand": "<maker of this row if it differs from the page's, else empty>",
           "price": "<the price digits exactly as printed>", "unit": "rial|toman|unknown"}]}
One row for EVERY product line that has a price, also rows without a model code. Copy digits exactly.
The unit comes from the words near the prices (ریال = rial, تومان = toman). Leave out cartons, counts, row numbers,
phone numbers and other numbers that are not the price. No prose, only the JSON."""


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


def name_key(name: str) -> str:
    """'N' + a short hash of the normalized name, for a list row with no model code."""
    import hashlib
    from .normalize import norm_text
    n = norm_text(str(name or "").translate(FA_DIGITS))
    return ("N" + hashlib.sha1(n.encode()).hexdigest()[:10].upper()) if len(n) >= 6 else ""


def is_name_key(key: str) -> bool:
    return bool(re.fullmatch(r"N[0-9A-F]{10}", str(key)))


def _words_nums(text: str) -> tuple[set, set]:
    from .normalize import norm_text
    t = norm_text(_clean_html(str(text or "")).translate(FA_DIGITS)).replace("/", " / ")
    nums = set(re.findall(r"\d+(?:\.\d+)?", t))
    words = {w for w in t.split() if not re.search(r"\d", w) and len(w) > 1 and w not in GENERIC_WORDS}
    return words, nums


GENERIC_WORDS = {"مدل", "برند", "سری", "با", "و", "به", "همراه", "عدد", "هرکدام", "سایز", "اینچ", "میلیمتر", "قیمت", "خرید"}


def same_by_name(product_name: str, list_name: str) -> bool:
    """A row with no code is a site product only when the names agree: every number of the row is in the
    product's name, and most words of each are in the other (the variant words decide: 'دستی' is not
    'تمام هیدرولیک')."""
    lw, ln = _words_nums(list_name)
    pw, pn = _words_nums(product_name)
    if not lw or not pw or not ln <= pn:
        return False
    return len(lw & pw) >= 0.8 * len(lw) and len(lw & pw) >= 0.6 * len(pw)


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

def _openrouter(cfg: dict, messages: list, max_tokens: int = 3000, title: str = "Caravan price list") -> tuple[dict | None, float]:
    """One JSON answer of the cheap model (OpenRouter) and its cost in dollars."""
    import requests
    from .config import secret
    key = secret("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("کلید OpenRouter روی سرور نیست")
    body = {"model": settings(cfg)["model"], "max_tokens": max_tokens, "temperature": 0,
            "reasoning": {"enabled": False}, "response_format": {"type": "json_object"},
            "messages": messages, "usage": {"include": True}}
    for attempt in range(3):
        try:
            r = requests.post(OPENROUTER, json=body, timeout=180, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "HTTP-Referer": "https://caravansanat.com", "X-Title": title})
        except requests.RequestException as e:
            log.info("pricelist model: %s", e)
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503):
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code != 200:
            log.warning("pricelist model: HTTP %s %s", r.status_code, r.text[:200])
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


def _vision(cfg: dict, png: bytes, mime: str = "image/jpeg") -> tuple[dict | None, float]:
    return _openrouter(cfg, [{"role": "system", "content": VISION_SYSTEM},
                             {"role": "user", "content": [
                                 {"type": "text", "text": "Read every priced product of this price list."},
                                 {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(png).decode()}}]}])


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
                h = hashlib.sha1(pic + VISION_SYSTEM.encode()).hexdigest()    # a new prompt reads the picture again
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
                        rows.append({"brand": str(r.get("brand") or j.get("brand") or ""), "model": str(r.get("model") or ""),
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
            k = name_key(r.get("name", ""))           # a row with no model code is known by its whole name
            if not k:
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
        elif b == a and row["toman"] and old["toman"] and row["toman"] != old["toman"] and not k.startswith("N"):
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


def _of_brand(p: dict, term: dict | None, resolved: dict, brand: str, cfg: dict) -> bool:
    """The product is the list's brand: its brand term, or the brand's name in its name (a list naming a
    brand only)."""
    if not str(brand or "").strip():
        return False
    if term and term["id"] in [b.get("id") for b in p.get("brands") or []]:
        return True
    name = _flat(_clean_html(p.get("name")))
    return any(n and len(n) >= 3 and n in name for n in brand_names(cfg, brand))


# words the lists and the shops write differently for one thing (3 Nov, the P.S.C list)
SYNONYMS = {"هیتر": "صفحه گرمکن", "اسکرابر": "اسکراپر", "رستریگ": "رتنبرگ", "اچار": "آچار", "لقه": "لقمه"}
SAME_SYSTEM = """You compare one row of a supplier's price list with products of an online shop.
Pick every shop product that is the SAME product and the SAME variant as the row: the same size range, the same
number of jaws, the same drive (manual / gearbox / semi-hydraulic / full hydraulic), the same heater (electric / gas),
the same design. Words may differ (هیتر = صفحه گرمکن, رستریگ = رتنبرگ, اهرم دستی = دستی; digits may be Persian or
Latin; a range may be written in either order). A product of another named brand, another size, another variant, a
spare part or an accessory is NOT the same. Usually there is none or one; several only when the shop has copies.
Reply JSON only: {"same": [<candidate numbers>]}"""


def _syn(text: str) -> str:
    t = str(text or "")
    for a, b in SYNONYMS.items():
        t = re.sub(rf"(?<!\S){a}(?!\S)", b, t)
    return t


def _name_cands(wc, name: str, site_name: str = "", n: int = 10) -> list[dict]:
    """Shop products that may be a row with no code: searched by its first words (also in the shop's words and
    by the supplier site's name of it), kept only when they carry the row's sizes (numbers of 2+ digits)."""
    from .normalize import norm_text
    terms = []
    for t in (name, _syn(name), site_name):
        w = [x for x in norm_text(_clean_html(t).translate(FA_DIGITS)).split() if x not in GENERIC_WORDS and x not in GENERIC]
        words = [x for x in w if not re.search(r"\d", x)]
        nums = [x for x in re.findall(r"\d{2,}", " ".join(w))]
        if words:
            terms.append(" ".join(words[:3]))
            if nums:
                terms.append(" ".join(words[:2] + nums[:1]))
    _, ln = _words_nums(name)
    need = {x for x in ln if len(x) >= 2}
    found: dict[int, dict] = {}
    for t in dict.fromkeys(terms):
        for p in _search(wc, t):
            _, pn = _words_nums(p.get("name") or "")
            if need <= pn and p["id"] not in found:
                found[p["id"]] = p
    return list(found.values())[:n]


def _pick_same(cfg: dict, name: str, brand: str, cand_names: list[str]) -> tuple[list[int], float]:
    user = f"row: {name}\nbrand of the list: {brand}\n" + "\n".join(f"{j}: {c}" for j, c in enumerate(cand_names))
    ans, usd = _openrouter(cfg, [{"role": "system", "content": SAME_SYSTEM}, {"role": "user", "content": user}],
                           max_tokens=200, title="Caravan price list match")
    js = []
    for j in (ans or {}).get("same") or []:
        try:
            j = int(j)
        except (TypeError, ValueError):
            continue
        if 0 <= j < len(cand_names) and j not in js:
            js.append(j)
    return js, usd


def match(cfg: dict, run: str, wc=None, progress=None) -> dict:
    from .wc import WC
    wc = wc or WC()
    if not wc.ok():
        raise ValueError("WC_URL / WC_KEY / WC_SECRET روی سرور تنظیم نشده")
    st = settings(cfg)
    it = items(cfg, run)
    if (it["toman"].astype(str).str.strip() == "").all():
        raise ValueError("ردیف قیمت‌داری در لیست نیست؛ اول «خواندن» را اجرا کنید")
    brands = _brand_terms(wc, it, cfg)
    seen: dict[int, dict] = {}
    rows, rate, llm_usd = [], None, 0.0
    sm = run_path(cfg, run) / "site_match.csv"            # the supplier site's name of a row: our old copies may use it
    site_names = {str(x["key"]): x["site_name"] for x in read_csv(sm).fillna("").to_dict("records")} if sm.exists() else {}
    keys = list(it.to_dict("records"))
    for k, r in enumerate(keys):
        if progress:
            progress(k, len(keys) + 1)
        hits = {}
        if is_name_key(r["key"]):
            cands = _name_cands(wc, r["name"], site_names.get(str(r["key"]), ""))
            sure = [p for p in cands if same_by_name(p.get("name"), r["name"])]
            if sure:
                hits = {p["id"]: p for p in sure}
            elif cands:
                js, u = _pick_same(cfg, r["name"], str(r["brand"]), [_clean_html(p.get("name")) for p in cands])
                llm_usd += u
                hits = {cands[j]["id"]: cands[j] for j in js}
        binfo = brands.get(str(r["brand"]).strip()) or {}
        bt, bres = binfo.get("term"), binfo.get("resolved") or {}
        maybe = {}
        for term in ([] if is_name_key(r["key"]) else dict.fromkeys([r["model"], r["model"].replace("-", " "), r["key"]])):
            for p in _search(wc, term):
                if contains_model(f"{p.get('name')} {p.get('sku')}", r["key"]) and not _is_part(p.get("name"), r["name"]):
                    if not weak_key(r["key"]) or kind_in(p.get("name"), r["name"]):
                        hits[p["id"]] = p
                    elif _of_brand(p, bt, bres, r["brand"], cfg):
                        maybe[p["id"]] = p
        # a code of digits only on a product of the list's brand, named in other words ('بلوور' in the list,
        # 'دمنده - مکنده' on the site, both Ronix 1209: 3 Nov): the cheap model says whether it is the same
        maybe = [p for pid, p in maybe.items() if pid not in hits][:8]
        if maybe:
            js, u = _pick_same(cfg, f"{r['name']} {r['model']}", str(r["brand"]), [_clean_html(p.get("name")) for p in maybe])
            llm_usd += u
            hits.update({maybe[j]["id"]: maybe[j] for j in js})
        priced = str(r["toman"]).strip() != ""
        if not priced:                                   # in the catalog without a price: kept in stock, price untouched
            for pid, p in hits.items():
                seen[pid] = p
                rows.append({"action": "listed", "key": r["key"], "model": r["model"], "brand": r["brand"],
                             "list_name": r["name"], "list_toman": "", "product_id": pid,
                             "product_name": _clean_html(p.get("name")), "status": p.get("status", ""),
                             "stock": p.get("stock_status", ""), "note": "در لیست بدون قیمت؛ قیمت دست نمی‌خورد"})
            if not hits:
                rows.append({"action": "skip", "key": r["key"], "model": r["model"], "brand": r["brand"],
                             "list_name": r["name"], "list_toman": "", "note": "بدون قیمت و روی سایت نیست؛ ساخته نمی‌شود"})
            continue
        if not hits:
            rows.append({"action": "new", "key": r["key"], "model": r["model"], "brand": r["brand"],
                         "list_name": r["name"], "list_toman": int(r["toman"]), "note": r["note"]})
            continue
        # one model, several copies on the site: the oldest published one stays (3 Nov), the rest go to it
        order = sorted(hits.values(), key=lambda p: (p.get("status") != "publish", int(p["id"])))
        keep = order[0]["id"]
        for pid, p in hits.items():
            seen[pid] = p
            if pid != keep:
                rows.append({"action": "dup", "key": r["key"], "model": r["model"], "brand": r["brand"],
                             "list_name": r["name"], "list_toman": int(r["toman"]), "product_id": pid,
                             "product_name": _clean_html(p.get("name")), "status": p.get("status", ""),
                             "keep_id": keep, "note": f"نسخه‌ی تکراری؛ به #{keep} ریدایرکت می‌شود"})
                continue
            if rate is None and p.get("regular_price"):
                rate = site_rate(wc, pid)
            pb = [b.get("name", "") for b in p.get("brands") or []]
            usd = float(p.get("regular_price") or 0) if p.get("type") != "variable" else 0
            rows.append({"action": "update", "key": r["key"], "model": r["model"], "brand": r["brand"],
                         "list_name": r["name"], "list_toman": int(r["toman"]), "product_id": pid,
                         "product_name": _clean_html(p.get("name")), "status": p.get("status", ""),
                         "_usd": usd, "site_brand": "، ".join(pb),
                         "_brand_ok": _brand_ok(p, bt, bres), "stock": p.get("stock_status", ""),
                         "_type": p.get("type"), "note": r["note"]})
    if rate is None:                                   # nothing of the list is on the site: any product tells it
        j = wc._call(lambda: wc._wc("GET", "products", params={"per_page": 5, "status": "publish", "context": "edit",
                                                               "type": "simple"}), "rate sample")
        for p in j or []:
            if p.get("regular_price") and (rate := site_rate(wc, p["id"])):
                break
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
    # the brand's other products: not in the list -> out of stock when the list is the brand's whole list
    # (3 Nov); products with the brand only in their name count too and get the brand. Copies far apart in
    # price are still told apart ('spread') for the report.
    from .merge_site import registry
    merged_dups = {str(x.get("product_id")) for x in registry(cfg).values() if x.get("role") == "dup"}
    for name, b in brands.items():
        t = b.get("term")
        prods = []
        if t:
            try:
                prods = _brand_products(wc, int(t["id"]))
            except Exception as e:                       # noqa: BLE001 - the report says so
                log.warning("pricelist brand %s: %s", name, e)
        unbranded = _named_unbranded(wc, cfg, name, {p["id"] for p in prods} | set(seen))
        prods = [p for p in prods + unbranded if p["id"] not in seen and str(p["id"]) not in merged_dups
                 and p.get("status") in ("publish", "pending", "private")]
        for p in prods:
            if p.get("type") == "variable" or not p.get("regular_price"):
                out.append({"action": "other", "key": f"_{p['id']}", "brand": name, "product_id": p["id"],
                            "product_name": _clean_html(p.get("name")), "status": p.get("status", ""),
                            "stock": p.get("stock_status", ""), "_type": p.get("type"),
                            "fix_brand": "yes" if p.get("_unbranded") else "",
                            "note": "محصول متغیر" if p.get("type") == "variable" else "بدون قیمت"})
        others = [p for p in prods if p.get("type") != "variable" and p.get("regular_price")]
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
                            "stock": p.get("stock_status", ""), "fix_brand": "yes" if p.get("_unbranded") else "",
                            "note": f"{len(ps)} نسخه با قیمت‌های {', '.join(f'{round(x * rate / 1e6, 2)}M' for x in sorted(prices))}"
                            if far else ""})
    plan = pd.DataFrame(out)
    plan = plan.drop(columns=[c for c in ("_type",) if c in plan])
    for c in PLAN_COLS + EXTRA_COLS:
        if c not in plan:
            plan[c] = ""
    plan = plan.fillna("")
    d = run_path(cfg, run)
    write_csv(plan, d / "plan.csv", PLAN_COLS + EXTRA_COLS)
    upd = plan[plan["action"].isin(["update", "same", "brand"])]
    ratios = [r["list_toman"] / r["site_toman"] for r in upd.to_dict("records") if r["site_toman"]]
    summ = {"rate": rate, "counts": plan["action"].value_counts().to_dict(), "big": int((plan["big"] == "yes").sum()),
            "median_change_pct": round((median(ratios) - 1) * 100, 1) if ratios else None,
            "brands": {k: (v["term"] or {}).get("name", "") for k, v in brands.items()},
            "usd": round(llm_usd, 4), "at": time.strftime("%Y-%m-%d %H:%M")}
    # what 'the list is the brand's whole list' would do: the share going out of stock warns of a missing page
    oos = plan[plan["action"].isin(["other", "spread"]) & (plan["stock"] != "outofstock")]
    inlist = plan[plan["action"].isin(["update", "same", "brand", "dup", "listed", "skip"]) & (plan["product_id"].astype(str) != "")]
    summ["out_of_stock"] = int(len(oos))
    summ["oos_share"] = round(len(oos) / max(1, len(oos) + len(inlist)), 2)
    (d / "match.json").write_text(json.dumps(summ, ensure_ascii=False))
    log.info("PRICELIST match %s: %s", run, summ)
    return summ


def _named_unbranded(wc, cfg: dict, brand: str, skip: set) -> list[dict]:
    """Products with no brand term whose name holds the brand (3 Nov: 'دریل رونیکس 2700' with no brand
    still is Ronix): searched by the brand's names, marked _unbranded (the brand is put on them)."""
    from .normalize import norm_text
    names = []
    try:
        from .catalog_publish import _brand
        br = _brand(cfg, brand)
        names = [x for x in (br.get("fa"), br.get("en")) if x and len(str(x)) >= 3]
    except Exception:                                    # noqa: BLE001
        pass
    out = {}
    for n in dict.fromkeys(names):
        nn = norm_text(str(n)).casefold()
        for p in _search(wc, n):
            if p["id"] in skip or p.get("brands"):
                continue
            if re.search(rf"(?<!\w){re.escape(nn)}(?!\w)", norm_text(_clean_html(p.get("name"))).casefold()):
                out[p["id"]] = {**p, "_unbranded": True}
    return list(out.values())


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
        if str(r.get("toman") or "").strip() and len(r["key"]) >= 4 and not is_name_key(r["key"]):
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


def _locks_path(cfg: dict) -> Path:
    return pdir(cfg) / "brand_locks.json"


def brand_locks(cfg: dict) -> dict:
    """Brands whose whole list was applied (3 Nov): the crawler passes their products by."""
    p = _locks_path(cfg)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def lock_brand(cfg: dict, brand: str, run: str, extra_names: list | None = None) -> dict:
    from .normalize import norm_text
    names = set()
    try:
        from .catalog_publish import _brand
        br = _brand(cfg, brand)
        names |= {br.get("fa", ""), br.get("en", "")}
    except Exception:                                    # noqa: BLE001
        pass
    names |= set(extra_names or [])
    names = sorted({norm_text(str(n)).casefold().strip() for n in names if len(str(n or "").strip()) >= 3})
    locks = brand_locks(cfg)
    locks[brand] = {"names": names, "run": run, "at": time.strftime("%Y-%m-%d %H:%M")}
    _locks_path(cfg).write_text(json.dumps(locks, ensure_ascii=False, indent=1), encoding="utf-8")
    return locks[brand]


def unlock_brand(cfg: dict, brand: str) -> bool:
    locks = brand_locks(cfg)
    if brand not in locks:
        return False
    locks.pop(brand)
    _locks_path(cfg).write_text(json.dumps(locks, ensure_ascii=False, indent=1), encoding="utf-8")
    return True


def locked_brand_of(locks: dict, name: str, brand: str = "") -> str:
    """The locked brand a crawled product belongs to (its brand field or a whole word of its name), or ''."""
    if not locks:
        return ""
    from .normalize import norm_text
    n = norm_text(f"{name} {brand}").casefold()
    for b, v in locks.items():
        for w in v.get("names") or []:
            if w and re.search(rf"(?<!\w){re.escape(w)}(?!\w)", n):
                return b
    return ""


def lock_meta(run: str, toman: int, day: str = "") -> list[dict]:
    day = day or time.strftime("%Y-%m-%d")
    return [{"key": "_caravan_price_lock", "value": f"{run} {day}"},
            {"key": "_caravan_price_toman", "value": str(int(toman))},
            {"key": "_caravan_price_date", "value": day},
            {"key": "_caravan_price_estimated", "value": ""}]


def apply(cfg: dict, run: str, wc=None, include_big: bool = False, spread: bool = True, progress=None, stop=None,
          complete: bool = True) -> dict:
    """The plan on the site. complete (3 Nov, the list is the brand's whole list): the brand's products not in
    it go out of stock, products back in it come back in stock, and the brand is locked for the crawler.
    Copies of a listed model go (301) to the copy kept. Every product's state before is kept in backup.jsonl."""
    from .merge_site import redirect_copy
    from .publish import SiteBrands
    from .wc import WC
    wc = wc or WC()
    if not wc.ok():
        raise ValueError("WC_URL / WC_KEY / WC_SECRET روی سرور تنظیم نشده")
    d = run_path(cfg, run)
    pl = plan(cfg, run)
    if pl.empty:
        raise ValueError("اول «تطبیق با سایت» را اجرا کنید")
    for c in EXTRA_COLS:
        if c not in pl:
            pl[c] = ""
    rate = float(json.loads((d / "match.json").read_text())["rate"])
    priced = pl[pl["action"].isin(["update", "same", "brand"]) & ((pl["big"] != "yes") | include_big)]
    brands = SiteBrands(wc)
    terms = _brand_terms(wc, pl[pl["brand"].astype(str).str.strip() != ""], cfg) if len(pl) else {}

    def brand_of(r) -> dict | None:
        br = (terms.get(str(r["brand"]).strip()) or {}).get("resolved")
        bid = brands.id_for(br)[0] if br and br.get("status") == "ok" else None
        return {"brands": [{"id": int(bid)}]} if bid else None

    items_ = []
    for r in priced.to_dict("records"):
        usd = round(float(r["list_toman"]) / rate, 4)
        x = {"id": int(float(r["product_id"])), "regular_price": f"{usd:.4f}", "sale_price": "",
             "meta_data": lock_meta(run, int(float(r["list_toman"])))}
        if r.get("stock") == "outofstock":
            x["stock_status"] = "instock"
        if r.get("fix_brand") == "yes" and (b := brand_of(r)):
            x.update(b)
        items_.append((r, x))
    for r in pl[pl["action"] == "listed"].to_dict("records"):
        if r.get("stock") == "outofstock":
            items_.append((r, {"id": int(float(r["product_id"])), "stock_status": "instock"}))
    oos_rows = pl[pl["action"].isin(["other", "spread"])] if complete else pl.iloc[0:0]
    for r in oos_rows.to_dict("records"):
        x = {"id": int(float(r["product_id"]))}
        if r.get("stock") != "outofstock" and "متغیر" not in str(r.get("note")):
            x["stock_status"] = "outofstock"
            x["meta_data"] = [{"key": "_caravan_oos_list", "value": f"{run} {time.strftime('%Y-%m-%d')}"}]
        if r.get("fix_brand") == "yes" and (b := brand_of(r)):
            x.update(b)
        if len(x) > 1:
            items_.append((r, x))
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
                                         "stock_status": p.get("stock_status"), "brands": p.get("brands"),
                                         "at": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False) + "\n")
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
                             "price_toman": r.get("list_toman", "") if "regular_price" in x else "",
                             "status": x.get("stock_status", ""), "brand_fixed": "yes" if "brands" in x else ""})
    redirected = 0
    for r in pl[pl["action"] == "dup"].to_dict("records"):
        if stop and stop():
            break
        try:
            how = redirect_copy(cfg, wc, int(float(r["keep_id"])), int(float(r["product_id"])), run)
            redirected += 1
            done.append({"product_id": int(float(r["product_id"])), "action": "dup", "model": r["model"],
                         "name": r.get("product_name", ""), "status": how, "site_toman": "", "price_toman": "",
                         "brand_fixed": ""})
        except Exception as e:                           # noqa: BLE001
            failed.append({"product_id": int(float(r["product_id"])), "error": str(e)[:200]})
    it = items(cfg, run)
    indexed = save_index(cfg, run, it)
    locked = []
    if complete and not (stop and stop()):
        for b in sorted({str(x).strip() for x in it["brand"] if str(x).strip()}):
            lock_brand(cfg, b, run)
            locked.append(b)
    write_csv(pd.DataFrame(done + [{**f, "action": "error"} for f in failed]), d / "applied.csv",
              ["product_id", "action", "model", "name", "site_toman", "price_toman", "status", "brand_fixed", "error"])
    res = {"priced": sum(1 for x in done if x["action"] in ("update", "same", "brand") and x["price_toman"] != ""),
           "out_of_stock": sum(1 for x in done if x["status"] == "outofstock"),
           "back_in_stock": sum(1 for x in done if x["status"] == "instock"),
           "redirected": redirected, "brand_locked": locked,
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
        rows.append({"taxonomy_id": m[0]["taxonomy_id"],
                     "query": (f"{r['model']} {fa or en or r['brand']}" if r["model"] else str(r["list_name"])).strip(),
                     "breadcrumb": t["breadcrumb"], "leaf": t["leaf"], "level1": t["level1"],
                     "route": resolve_route(t, cfg.get("routes") or {}), "site_product_count": int(t["site_product_count"]),
                     "priority": len(rows), "extra_queries": f"{r['list_name']} {r['model']}",
                     "from_searches": f"price list {run}", "searches": 0})
    if not rows:
        raise ValueError("برای هیچ‌کدام دسته‌ای پیدا نشد")
    name = f"{run}-new"
    write_csv(pd.DataFrame(rows), run_dir(cfg, name) / "01_queue.csv", QUEUE_COLS + ["extra_queries", "from_searches", "searches"])
    return {"run": name, "queued": len(rows), "no_category": skipped}


def _leaf_for(idx, name: str) -> dict | None:
    """The taxonomy leaf for a new row: every word of the leaf must be in the row's name (no other maker's
    leaf, no part's leaf)."""
    from .normalize import norm_text
    from .routing import leaf_brand
    words = set(norm_text(str(name)).split())
    for x in idx.match(str(name), n=25):
        if not leaf_brand(x["leaf"]) and all(w in words or re.fullmatch(r"[\d.]+", w) for w in norm_text(x["leaf"]).split()):
            return x
    return None


# ------------------------------------------------------------------ the supplier's own site (3 Nov)
# A list without photos: when the supplier has a shop site, its products give the new rows their photos,
# text and specs. The price always stays the list's.

SITE_MATCH_COLS = ["key", "list_name", "site_name", "site_url", "images", "how"]
PICK_SYSTEM = """You match rows of a supplier's price list to the products of the same supplier's own website.
For each row choose the candidate that is the SAME product and the SAME variant: the same size range, the same
number of jaws, the same drive (manual / gearbox / semi-hydraulic / full hydraulic), the same heater (electric /
gas), the same design. Words may differ (هیتر = صفحه گرمکن, رستریگ = رتنبرگ, اهرم دستی = دستی, digits may be
Persian or Latin, a range may be written in either order). If no candidate is that same product, answer null;
a near size or another variant is null.
Reply JSON only: {"picks": [{"i": <row number>, "j": <candidate number or null>}]}"""


def norm_site(url: str) -> str:
    from urllib.parse import urlparse
    u = str(url or "").strip()
    if not u:
        return ""
    if not re.match(r"https?://", u, re.I):
        u = "https://" + u
    host = urlparse(u).netloc.lower()
    if "." not in host or " " in host:
        raise ValueError("آدرس سایت درست نیست (مثل pooya-sanat.com)")
    return f"https://{host}/"


def get_site(cfg: dict, run: str) -> str:
    p = run_path(cfg, run) / "site.json"
    return json.loads(p.read_text()).get("url", "") if p.exists() else ""


def set_site(cfg: dict, run: str, url: str) -> str:
    d = run_path(cfg, run)
    u = norm_site(url)
    old = get_site(cfg, run)
    (d / "site.json").write_text(json.dumps({"url": u}, ensure_ascii=False))
    if u != old:                                          # another site: its products and matches are not this one's
        (d / "site_match.csv").unlink(missing_ok=True)
    return u


def site_products(cfg: dict, run: str, stop=None, fresh_days: float = 3) -> list[dict]:
    """Every product of the supplier's site (WooCommerce Store API, else the sitemap, else links), kept in
    the run's folder for a few days so a second press does not crawl the site again."""
    import argparse
    import threading
    from . import wpshop
    url = get_site(cfg, run)
    if not url:
        return []
    d = run_path(cfg, run) / "site"
    d.mkdir(exist_ok=True)
    out = d / "products.jsonl"
    meta = d / "crawl.json"
    m = json.loads(meta.read_text()) if meta.exists() else {}
    if out.exists() and m.get("url") == url and time.time() - m.get("at", 0) < fresh_days * 86400:
        return wpshop.read_jsonl(str(out))
    a = argparse.Namespace(out=str(d), mode="auto", delay=1.0, workers=2, parallel_sites=1, max_pages=1500,
                           timeout=30, proxy=None, user_agent=wpshop.DEFAULT_USER_AGENT, images="none", enrich=False,
                           only_available=False, resume=False, ignore_robots=False, insecure=False, verbose=False)
    ev = threading.Event()
    c = wpshop.SiteCrawler(url, a, stop_event=ev, out_path=str(out))
    if stop:
        def watch():
            while not ev.is_set():
                if stop():
                    ev.set()
                time.sleep(1)
        threading.Thread(target=watch, daemon=True).start()
    try:
        n = c.run()
    finally:
        ev.set()                                          # the watcher ends with the crawl
    meta.write_text(json.dumps({"url": url, "at": time.time(), "products": n, "robots_blocked": c.blocked_by_robots}))
    return wpshop.read_jsonl(str(out)) if out.exists() else []


def _cands(name: str, recs: list[dict], n: int = 8) -> list[int]:
    """The site products most like a row: shared numbers (sizes) first, then shared words."""
    lw, ln = _words_nums(name)
    scored = []
    for j, r in enumerate(recs):
        pw, pn = _words_nums(r.get("name") or "")
        num = len(ln & pn) / len(ln) if ln else 0.0
        wrd = len(lw & pw) / len(lw) if lw else 0.0
        if num or wrd >= 0.34:
            scored.append((num * 2 + wrd, j))
    return [j for _, j in sorted(scored, reverse=True)[:n]]


def site_match(cfg: dict, run: str, progress=None, stop=None) -> dict:
    """The list's new rows -> the supplier site's products (site_match.csv). Nothing on caravansanat changes."""
    if not get_site(cfg, run):
        raise ValueError("برای این لیست سایتی ثبت نشده")
    d = run_path(cfg, run)
    pl = plan(cfg, run)
    rows = pl[pl["action"] == "new"].to_dict("records") if not pl.empty else items(cfg, run).rename(
        columns={"name": "list_name"}).to_dict("records")
    if progress:
        progress(0, len(rows))
    recs = [r for r in site_products(cfg, run, stop=stop) if (r.get("name") or "").strip()]
    if not recs:
        raise ValueError("از سایت تأمین‌کننده محصولی خوانده نشد (سایت باز نشد یا robots.txt اجازه نداد)")
    picks, usd, todo = {}, 0.0, []
    for i, r in enumerate(rows):
        c = _cands(r["list_name"], recs)
        sure = [j for j in c if same_by_name(recs[j]["name"], r["list_name"])]
        if len(sure) == 1:
            picks[i] = (sure[0], "نام یکسان")
        elif c:
            todo.append((i, c))
    for b in range(0, len(todo), 8):
        if stop and stop():
            break
        if progress:
            progress(b, len(todo))
        part = todo[b:b + 8]
        user = "\n\n".join(f"row {i}: {rows[i]['list_name']}\n" + "\n".join(f"  {j}: {recs[j]['name']}" for j in c)
                           for i, c in part)
        ans, u = _openrouter(cfg, [{"role": "system", "content": PICK_SYSTEM}, {"role": "user", "content": user}],
                             max_tokens=800, title="Caravan price list site")
        usd += u
        ok = {i: set(c) for i, c in part}
        for x in (ans or {}).get("picks") or []:
            try:
                i, j = int(x.get("i")), x.get("j")
            except (TypeError, ValueError):
                continue
            if i in ok and j is not None and int(j) in ok[i]:
                picks[i] = (int(j), "مدل زبانی")
    out = []
    for i, r in enumerate(rows):
        j, how = picks.get(i, (None, ""))
        s = recs[j] if j is not None else {}
        out.append({"key": r["key"], "list_name": r["list_name"], "site_name": s.get("name", ""),
                    "site_url": s.get("url", ""), "images": int(s.get("image_count") or 0) if s else 0, "how": how})
    write_csv(pd.DataFrame(out, columns=SITE_MATCH_COLS), d / "site_match.csv", SITE_MATCH_COLS)
    res = {"site": get_site(cfg, run), "site_products": len(recs), "rows": len(rows),
           "found": sum(1 for x in out if x["site_url"]), "with_photos": sum(1 for x in out if x["images"]),
           "usd": round(usd, 4), "at": time.strftime("%Y-%m-%d %H:%M")}
    (d / "site_match.json").write_text(json.dumps(res, ensure_ascii=False))
    log.info("PRICELIST site match %s: %s", run, res)
    return res


def site_rows(cfg: dict, run: str) -> dict:
    """key -> the site product chosen for that row (from site_match.csv)."""
    d = run_path(cfg, run)
    p, jl = d / "site_match.csv", d / "site" / "products.jsonl"
    if not p.exists() or not jl.exists():
        return {}
    from . import wpshop
    by_url = {r.get("url"): r for r in wpshop.read_jsonl(str(jl))}
    out = {}
    for r in read_csv(p).fillna("").to_dict("records"):
        if r["site_url"] and r["site_url"] in by_url:
            out[str(r["key"])] = by_url[r["site_url"]]
    return out


def _photos(cfg: dict, run: str, rec: dict, n: int = 4) -> list[Path]:
    """The site product's photos, downloaded once and saved as webp (small ones and icons are left out)."""
    import hashlib
    import requests
    from PIL import Image
    d = run_path(cfg, run) / "site" / "img"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for u in [x.strip() for x in str(rec.get("images") or "").split("|") if x.strip()]:
        if len(out) >= n:
            break
        f = d / (hashlib.sha1(u.encode()).hexdigest()[:16] + ".webp")
        if not f.exists():
            try:
                r = requests.get(u, timeout=40, headers={"User-Agent": "Mozilla/5.0 (CaravanSanat price list)"})
                r.raise_for_status()
                im = Image.open(io.BytesIO(r.content))
                if min(im.size) < 300:
                    continue
                im = im.convert("RGB")
                im.thumbnail((1600, 1600))
                im.save(f, "WEBP", quality=85)
            except Exception as e:                       # noqa: BLE001 - one photo's failure leaves the others
                log.info("pricelist photo %s: %s", u, e)
                continue
        out.append(f)
    return out


def _similar_photos(cfg: dict, name: str, brand: str, n: int = 2) -> list[Path]:
    """No photo of the product itself (3 Nov): a photo of the same kind and size from another page, found by
    the name WITHOUT the brand, used only when the strict check finds it clean (no other shop's or maker's
    logo, no added text, white background, the whole product) and the product it shows matches the name."""
    import hashlib
    from PIL import Image
    from .config import secret
    from .images import download, search_images
    from .normalize import norm_text
    from .pipeline import _fetchers
    from .vision import ASK_FOUND, check, found_ok
    key = secret("OPENROUTER_API_KEY")
    if not key:
        return []
    bw = {norm_text(w).casefold() for b in brand_names(cfg, brand) for w in [b]} | \
         {norm_text(w).casefold() for w in str(brand or "").replace("(", " ").replace(")", " ").split() if len(w) > 2}
    words = [w for w in str(name).split() if norm_text(w).casefold() not in bw and _flat(w) not in bw]
    q = " ".join(words)
    f = _fetchers(cfg)["iran"]
    model = cfg.get("images", {}).get("vision_model", "deepseek/deepseek-v4.1-flash")
    d = pdir(cfg) / "similar"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for u in search_images(cfg, f, {"name": q, "image_query": q}, n=8):
        if len(out) >= n:
            break
        v = check(model, u, q, key, ask=ASK_FOUND)
        if not found_ok(v):
            continue
        data = download(f, u, min_side=400)
        if not data:
            continue
        fp = d / (hashlib.sha1(u.encode()).hexdigest()[:16] + ".webp")
        try:
            im = Image.open(io.BytesIO(data)).convert("RGB")
            im.thumbnail((1600, 1600))
            im.save(fp, "WEBP", quality=85)
        except Exception:                                # noqa: BLE001
            continue
        out.append(fp)
    return out


def create_new(cfg: dict, run: str, wc=None, progress=None, stop=None) -> dict:
    """(3 Nov) The list's rows not on the site -> products made from the list itself, as 'pending' for a
    person: the name (with the brand), the list price (locked), a category, a description by the text model.
    With the supplier's own site set (site.json) a row found there brings its photos, text and specs; else no
    photo yet. A row made before (its SKU) is not made again."""
    import hashlib
    from .catalog_publish import _brand
    from .brands_fa import name_with_brand
    from .describe import Writer, template
    from .publish import SiteBrands
    from .searches import TaxIndex, load_tax
    from .wc import WC, category_map
    wc = wc or WC()
    if not wc.ok():
        raise ValueError("WC_URL / WC_KEY / WC_SECRET روی سرور تنظیم نشده")
    d = run_path(cfg, run)
    pl = plan(cfg, run)
    new = pl[pl["action"] == "new"]
    if new.empty:
        raise ValueError("ردیف تازه‌ای در این لیست نیست")
    rate = float(json.loads((d / "match.json").read_text())["rate"])
    idx, cats = TaxIndex(load_tax(cfg), cfg), category_map()
    brands, writer = SiteBrands(wc), Writer(cfg)
    if get_site(cfg, run) and not (d / "site_match.csv").exists():
        site_match(cfg, run, stop=stop)                  # the supplier's own site gives photos and text
    found = site_rows(cfg, run)
    out = []
    for i, r in enumerate(new.to_dict("records")):
        if stop and stop():
            break
        if progress:
            progress(i, len(new))
        sku = "PL-" + hashlib.sha1(f"{r['brand']}|{r['key']}".encode()).hexdigest()[:10].upper()
        res = {"key": r["key"], "name": r["list_name"], "sku": sku, "product_id": "", "status": "", "category": "",
               "photos": 0, "photo_from": "site" if str(r["key"]) in found else "", "site_url": "", "error": ""}
        try:
            if wc.find_by_sku(sku):
                res["error"] = "قبلاً ساخته شده"
                out.append(res)
                continue
            leaf = _leaf_for(idx, r["list_name"])
            wp = cats.get(str(leaf["taxonomy_id"])) if leaf else None
            if not wp:
                res["error"] = "دسته‌ی مناسب پیدا نشد"
                out.append(res)
                continue
            br = _brand(cfg, r["brand"]) if str(r["brand"]).strip() else {"status": "none"}
            name = name_with_brand(f"{r['list_name']} {r['model']}".strip() if r["model"] and r["model"] not in r["list_name"]
                                   else r["list_name"], br)
            site = found.get(str(r["key"])) or {}
            rec = {"url": sku, "name": name, "breadcrumb": leaf["breadcrumb"], "brand": br.get("display", ""),
                   "model": r["model"], "specs_json": site.get("specs") or "{}",
                   "description": site.get("description") or "", "seller_name": ""}
            txt = writer.write(rec) if writer.ok() else None
            html, short = (txt["html"], txt["short"]) if txt else template(rec)
            data = {"name": name, "type": "simple", "status": "pending", "sku": sku,
                    "regular_price": f"{float(r['list_toman']) / rate:.4f}", "description": html, "short_description": short,
                    "categories": [{"id": int(wp)}], "meta_data": lock_meta(run, int(float(r["list_toman"])))}
            bid = brands.id_for(br)[0] if br.get("status") == "ok" else None
            if bid:
                data["brands"] = [{"id": int(bid)}]
            files = []
            if site:
                res["site_url"] = site.get("url", "")
                data["meta_data"] = data["meta_data"] + [{"key": "_caravan_source_url", "value": site.get("url", "")}]
                files = _photos(cfg, run, site)
                if not files and site.get("images") and not wc.can_upload():
                    data["images"] = [{"src": u.strip()} for u in str(site["images"]).split("|") if u.strip()][:4]
            if not files and not data.get("images"):
                files = _similar_photos(cfg, r["list_name"], str(r["brand"]))
                res["photo_from"] = "similar" if files else ""
            if files and wc.can_upload():
                data["images"] = [{"id": wc.upload_media(f, f"{sku.lower()}-{n}.webp", alt=name)["id"]}
                                  for n, f in enumerate(files, 1)]
            res["photos"] = len(data.get("images") or [])
            if res["photos"]:                            # a photo and a category: on the shop at once (3 Nov)
                data["status"] = "publish"
            prod = wc.create_product(data)
            res.update(product_id=prod["id"], status=data["status"], category=leaf["breadcrumb"])
        except Exception as e:                           # noqa: BLE001 - one row's failure is recorded
            res["error"] = str(e)[:200]
        out.append(res)
    write_csv(pd.DataFrame(out), d / "created.csv", ["key", "name", "sku", "product_id", "status", "category", "photos",
                                                      "photo_from", "site_url", "error"])
    made = sum(1 for x in out if x["product_id"])
    log.info("PRICELIST create %s: %d made of %d", run, made, len(out))
    return {"made": made, "rows": len(out), "with_photos": sum(1 for x in out if x["photos"]),
            "published": sum(1 for x in out if x["status"] == "publish"),
            "similar_photos": sum(1 for x in out if x.get("photo_from") == "similar"), "no_category": sum(1 for x in out if x["error"] == "دسته‌ی مناسب پیدا نشد"),
            "failed": sum(1 for x in out if x["error"] and x["error"] not in ("قبلاً ساخته شده", "دسته‌ی مناسب پیدا نشد"))}


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
    p = d / "created.csv"
    out["created"] = read_csv(p).fillna("").to_dict("records") if p.exists() else []
    out["site"] = get_site(cfg, run)
    p = d / "site_match.json"
    out["site_info"] = json.loads(p.read_text()) if p.exists() else None
    p = d / "site_match.csv"
    out["site_match"] = read_csv(p).fillna("").to_dict("records") if p.exists() else []
    return out
