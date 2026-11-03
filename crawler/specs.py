"""The «ویژگی‌ها» list of a product's description as rows of its spec table (مشخصات فنی).

The shop asked (1 Nov) that the features list the descriptions carry ("اندازه‌گیری ولتاژ DC تا ۶۰۰ ولت",
"دارای یک سال گارانتی") also show in the spec table as name: value rows ("ولتاژ DC: تا ۶۰۰ ولت",
"گارانتی: ۱ سال"), for old products and new ones. The list stays in the text; the table only gains rows.
For now these are product attributes for the table only (no shop filters: those need the same names
across products, a later job).

  feature_items(html) -> the list items under the «ویژگی‌ها» heading
  to_pairs(...)       -> the cheap text model turns them into short name/value pairs (only facts in the lines)
  merged(attrs, pairs)-> the product's attributes with the new rows added (brand and others kept)
"""
from __future__ import annotations

import json
import logging
import re
from html import unescape

import requests

log = logging.getLogger(__name__)
MAX_ROWS = 16
WORDS = {"یک": "1", "دو": "2", "سه": "3", "چهار": "4", "پنج": "5", "شش": "6", "هفت": "7", "هشت": "8",
         "نه": "9", "ده": "10", "دوازده": "12", "صد": "100"}
_HEAD = re.compile(r"<h[2-4][^>]*>\s*(?:ویژگی|ویژگي|مشخصات کلیدی)[^<]*</h[2-4]>\s*<ul[^>]*>(.*?)</ul>", re.S | re.I)
_LI = re.compile(r"<li[^>]*>(.*?)</li>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")

SYSTEM = """You turn the feature lines of a Persian product page into rows of a spec table.
Each row: {"name": short Persian spec name (1-3 words, e.g. "ولتاژ DC", "توان", "گارانتی", "وزن", "نمایشگر"),
"value": the value with its unit, as short as possible (e.g. "تا ۶۰۰ ولت", "۱ سال", "۲۰۰۰ شمارش")}.
Rules: use only what the lines say; never add a number or fact. Skip a line that has no concrete
spec (praise, "مناسب برای ...", "کیفیت بالا", uses). One row per spec; no two rows with the same name.
Keep model codes and units as written. At most 16 rows.
Return JSON only: {"rows": [{"name": "...", "value": "..."}]}"""


# a shop's WooCommerce attribute slug ('pa_working-pressure') in Persian; a slug with a word not here is not
# shown (its value usually sits under a Persian name too)
SLUG_WORDS = {
    "brand": "برند", "model": "مدل", "size": "سایز", "color": "رنگ", "colour": "رنگ", "rang": "رنگ",
    "weight": "وزن", "vazn": "وزن", "power": "توان", "tavan": "توان", "watt": "توان", "voltage": "ولتاژ",
    "voltaj": "ولتاژ", "volt": "ولتاژ", "garanty": "گارانتی", "garanti": "گارانتی", "guarantee": "گارانتی",
    "warranty": "گارانتی", "made": "کشور سازنده", "country": "کشور سازنده", "keshvar": "کشور سازنده",
    "origin": "کشور سازنده", "material": "جنس", "jens": "جنس", "type": "نوع", "noe": "نوع", "kind": "نوع",
    "capacity": "ظرفیت", "zarfiat": "ظرفیت", "length": "طول", "tool": "طول", "width": "عرض", "arz": "عرض",
    "height": "ارتفاع", "ertefa": "ارتفاع", "dimension": "ابعاد", "dimensions": "ابعاد", "abaad": "ابعاد",
    "speed": "سرعت", "sorat": "سرعت", "rpm": "دور", "pressure": "فشار", "feshar": "فشار", "working": "کاری",
    "work": "کاری", "response": "عملکرد", "temp": "دما", "temperature": "دما", "dama": "دما", "water": "آب",
    "sprinkling": "پاشش", "flow": "دبی", "debi": "دبی", "diameter": "قطر", "ghotr": "قطر", "thickness": "ضخامت",
    "zekhamat": "ضخامت", "phase": "فاز", "faz": "فاز", "frequency": "فرکانس", "current": "جریان", "amper": "آمپر",
    "ampere": "آمپر", "battery": "باتری", "chuck": "سه‌نظام", "torque": "گشتاور", "noise": "صدا", "cable": "کابل",
    "motor": "موتور", "gear": "دنده", "usage": "کاربرد", "application": "کاربرد", "karbord": "کاربرد",
    "connection": "اتصال", "inlet": "ورودی", "outlet": "خروجی", "input": "ورودی", "output": "خروجی",
    "max": "حداکثر", "min": "حداقل", "blade": "تیغه", "disc": "صفحه", "volume": "حجم", "hajm": "حجم",
    "tank": "مخزن", "head": "هد", "suction": "مکش", "accuracy": "دقت", "range": "محدوده", "display": "نمایشگر",
    "package": "بسته‌بندی", "count": "تعداد", "number": "تعداد", "tedad": "تعداد", "unit": "واحد",
    "standard": "استاندارد", "class": "کلاس", "grade": "گرید", "fuel": "سوخت", "engine": "موتور", "stroke": "کورس",
}


def fa_name(name: str) -> str | None:
    """A spec name as the table shows it: Persian names as they are, a Latin slug ('pa_working-pressure')
    in Persian ('فشار کاری'), None for a slug it cannot read."""
    from urllib.parse import unquote
    n = unquote(str(name or "")).strip()
    n = re.sub(r"^(pa|attribute)_", "", n, flags=re.I)
    if not n:
        return None
    if re.search(r"[\u0600-\u06FF]", n):
        return n.replace("-", " ").replace("_", " ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\- ]+", n):
        return n
    words = [w for w in re.split(r"[\-_ ]+", n.lower()) if w and w not in ("in", "of", "the", "and")]
    if not words or any(w not in SLUG_WORDS for w in words):
        return None
    fa = list(dict.fromkeys(SLUG_WORDS[w] for w in words))[::-1]   # 'working pressure' -> فشار کاری
    fa = [w + "ی" if i < len(fa) - 1 and w.endswith(("ا", "و")) else w for i, w in enumerate(fa)]   # دمای عملکرد
    return " ".join(fa)


_TABLE = re.compile(r"<h[2-4][^>]*>\s*مشخصات\s*فنی\s*</h[2-4]>\s*<table[^>]*>(.*?)</table>", re.S | re.I)
_TR = re.compile(r"<tr[^>]*>\s*<t[hd][^>]*>(.*?)</t[hd]>\s*<td[^>]*>(.*?)</td>\s*</tr>", re.S | re.I)


def table_rows(html: str) -> list[dict]:
    """The rows of the «مشخصات فنی» table the crawler used to put at the end of descriptions."""
    m = _TABLE.search(html or "")
    if not m:
        return []
    out = []
    for k, v in _TR.findall(m.group(1)):
        k, v = _text(k), _text(v)
        n = fa_name(k)
        if n and v:
            out.append({"name": n, "value": v})
    return out


def without_table(html: str) -> str:
    m = _TABLE.search(html or "")
    return (html[:m.start()] + html[m.end():]).strip() if m else html


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", s))).strip()


def feature_items(html: str) -> list[str]:
    m = _HEAD.search(html or "")
    if not m:
        return []
    return [t for t in (_text(x) for x in _LI.findall(m.group(1))) if t][:20]


def without_features(html: str, items: list[str], rows: list[dict], min_share: float = 0.6) -> str:
    """The description without its «ویژگی‌ها» list once the table carries it (the shop, 1 Nov: the list
    and the table say the same thing twice). Kept when too few of its lines became rows."""
    m = _HEAD.search(html or "")
    if not m or not items or len(rows) < min_share * len(items):
        return html
    return (html[:m.start()] + html[m.end():]).strip()


def _nums(s: str) -> list[str]:
    """The numbers of a text, Persian digits and separators ('۳٫۰۰۰', '۱۲٬۵۰۰') read as Latin, '3.000' == '3'."""
    from .normalize import fa_digits_to_en
    t = fa_digits_to_en(s).replace("٫", ".").replace("٬", "").replace("،", " ")
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)
    out = []
    for x in re.findall(r"\d+(?:\.\d+)?", t):
        out.append(x.rstrip("0").rstrip(".") if "." in x else x)
    return out


def clean_rows(rows, items: list[str]) -> list[dict]:
    """Rows a model gave, kept only when sane: short name, a value, no number the lines do not have."""
    src = " ".join(items)
    nums = set(_nums(src)) | {d for w, d in WORDS.items() if re.search(rf"(?<!\S){w}(?!\S)", src)}
    out, seen = [], set()
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        n, v = str(r.get("name") or "").strip(" :"), str(r.get("value") or "").strip()
        if not n or not v or len(n) > 30 or len(v) > 80 or n in seen:
            continue
        if any(x not in nums for x in _nums(n + " " + v)):
            continue                                         # a number the features do not have
        seen.add(n)
        out.append({"name": n, "value": v})
    return out[:MAX_ROWS]


def to_pairs(items: list[str], name: str, key: str, model: str = "deepseek/deepseek-v4.1-flash",
             timeout: int = 60) -> tuple[list[dict], float]:
    """(rows, usd) for one product's feature lines."""
    if not items:
        return [], 0.0
    user = f"Product: {name}\nFeature lines:\n" + "\n".join(f"- {x}" for x in items)
    body = {"model": model, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}, "max_tokens": 1500, "temperature": 0.1,
            "reasoning": {"enabled": False},                  # no hidden thinking: it used up the tokens
            "usage": {"include": True}}
    r = requests.post("https://openrouter.ai/api/v1/chat/completions", json=body, timeout=timeout,
                      headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    j = r.json()
    usd = float((j.get("usage") or {}).get("cost") or 0)
    txt = j["choices"][0]["message"]["content"] or ""
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        rows = json.loads(m.group(0)).get("rows") if m else []
    except ValueError:
        rows = []
    return clean_rows(rows, items), usd


def _v(s) -> str:
    from .normalize import fa_digits_to_en
    return re.sub(r"\s+", "", fa_digits_to_en(str(s)).replace("\u200c", "")).lower()


def merged(attrs: list[dict], rows: list[dict]) -> list[dict]:
    """The product's attributes with the rows added; an attribute it already has keeps its value, and a
    row whose value the table already shows under another name is not shown twice."""
    out = [dict(a) for a in attrs or []]
    have = {str(a.get("name") or "").strip() for a in out}
    vals = {_v(o) for a in out for o in (a.get("options") or [a.get("option")] if a.get("option") else a.get("options") or [])}
    pos = max([int(a.get("position") or 0) for a in out] + [0])
    for r in rows:
        if r["name"] in have or _v(r["value"]) in vals:
            continue
        vals.add(_v(r["value"]))
        pos += 1
        out.append({"name": r["name"], "options": [r["value"]], "visible": True, "variation": False, "position": pos})
        have.add(r["name"])
    return out


# ------------------------------------------------------------------ at publish (new products, re-sends)

CACHE = "specs_cache.json"


def _key(items: list[str]) -> str:
    import hashlib
    return hashlib.sha1("\n".join(items).encode()).hexdigest()[:16]


def load_cache(data_dir) -> dict:
    from pathlib import Path
    try:
        return json.loads((Path(data_dir) / CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(data_dir, cache: dict) -> None:
    from pathlib import Path
    p = Path(data_dir) / CACHE
    p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def prefetch(data_dir, texts: list[tuple[str, str]], key: str, workers: int = 8,
             model: str = "deepseek/deepseek-v4.1-flash") -> tuple[dict, float, int]:
    """(cache, usd, calls): rows for every (name, description) whose features are not cached yet, side by
    side. A product sent again (re-publish, price change) costs nothing: its list was turned once."""
    from concurrent.futures import ThreadPoolExecutor
    cache = load_cache(data_dir)
    todo = {}
    for name, html in texts:
        items = feature_items(html)
        if items and _key(items) not in cache:
            todo[_key(items)] = (name, items)
    usd = 0.0

    def one(k):
        name, items = todo[k]
        try:
            return k, *to_pairs(items, name, key, model)
        except Exception as e:                             # noqa: BLE001 - this product keeps its list
            log.warning("spec rows not made for %s: %s", name[:40], e)
            return k, None, 0.0
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for k, rows, c in ex.map(one, list(todo)):
                usd += c
                if rows is not None:
                    cache[k] = rows
        save_cache(data_dir, cache)
    return cache, usd, len(todo)


def apply(data: dict, cache: dict) -> dict:
    """The WooCommerce payload with the spec rows in its attributes, and out of its text both the
    «ویژگی‌ها» list and the «مشخصات فنی» table (the shop, 3 Nov: everything of the table belongs in the
    product's spec table, not as a second, untidy table in the text)."""
    html = data.get("description") or ""
    trows = table_rows(html)
    if trows or _TABLE.search(html):
        data["attributes"] = merged(data.get("attributes") or [], trows)
        html = data["description"] = without_table(html)
    items = feature_items(html)
    rows = cache.get(_key(items)) if items else None
    if not rows:
        return data
    data["attributes"] = merged(data.get("attributes") or [], rows)
    data["description"] = without_features(html, items, rows)
    return data


# ------------------------------------------------------------------ every product on the site, once

def site_run(cfg: dict, wc=None, workers: int = 8, batch: int = 50, start_page: int = 1, max_pages: int | None = None,
             dry_run: bool = False, progress=None, stop=None) -> dict:
    """Every product of the site: its list into its spec table, the list out of its text. Products with no
    list (most old ones) are only read. The text and attributes a product had are kept in
    DATA_DIR/specs_backup.jsonl before it is changed. Pages of 100 are read; the model works on 8 lists
    side by side; changes go to the site 50 at a time (products/batch). A second run changes nothing:
    a product whose list was taken out has none left."""
    import time
    from pathlib import Path
    from .config import secret
    if wc is None:
        from .wc import WC
        wc = WC(timeout=90)
    key = secret("OPENROUTER_API_KEY")
    data_dir = Path(cfg["_data_dir"])
    stats = {"read": 0, "with_list": 0, "changed": 0, "failed": 0, "usd": 0.0, "pages": 0, "last_page": start_page - 1}
    page, total_pages = start_page, None
    with open(data_dir / "specs_backup.jsonl", "a", encoding="utf-8") as bk:
        while True:
            if stop and stop():
                break
            prods, total, total_pages = wc.products_page(page, 100, "id,name,description,attributes", "any")
            if not prods:
                break
            stats["read"] += len(prods)
            have = [p for p in prods if feature_items(p.get("description") or "") or _TABLE.search(p.get("description") or "")]
            stats["with_list"] += len(have)
            cache, usd, _ = prefetch(data_dir, [(p["name"], without_table(p["description"])) for p in have], key, workers)
            stats["usd"] += usd
            ups = []
            for p in have:
                d = apply({"description": p["description"], "attributes": p.get("attributes") or []}, cache)
                if d["description"] == p["description"] and len(d["attributes"]) == len(p.get("attributes") or []):
                    continue
                bk.write(json.dumps({"id": p["id"], "t": time.time(), "description": p["description"],
                                     "attributes": p.get("attributes")}, ensure_ascii=False) + "\n")
                ups.append({"id": p["id"], **d})
            bk.flush()
            for i in range(0, len(ups), batch):
                part = ups[i:i + batch]
                if dry_run:
                    stats["changed"] += len(part)
                    continue
                try:
                    res = wc.batch_update(part)
                    bad = [x for x in (res.get("update") or []) if x.get("error")]
                    stats["changed"] += len(part) - len(bad)
                    stats["failed"] += len(bad)
                except Exception as e:                     # noqa: BLE001 - next batch; a re-run picks these up
                    log.warning("spec table: batch of %d not saved: %s", len(part), e)
                    stats["failed"] += len(part)
            stats["pages"] += 1
            stats["last_page"] = page
            if progress:
                progress(page, total_pages or page)
            log.info("spec table page %d/%s: %d read, %d with a list, %d changed so far ($%.3f)",
                     page, total_pages, len(prods), len(have), stats["changed"], stats["usd"])
            if (max_pages and stats["pages"] >= max_pages) or (total_pages and page >= total_pages):
                break
            page += 1
    stats["usd"] = round(stats["usd"], 4)
    return stats
