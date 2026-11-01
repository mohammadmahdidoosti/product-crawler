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


def merged(attrs: list[dict], rows: list[dict]) -> list[dict]:
    """The product's attributes with the rows added; an attribute it already has keeps its value."""
    out = [dict(a) for a in attrs or []]
    have = {str(a.get("name") or "").strip() for a in out}
    pos = max([int(a.get("position") or 0) for a in out] + [0])
    for r in rows:
        if r["name"] in have:
            continue
        pos += 1
        out.append({"name": r["name"], "options": [r["value"]], "visible": True, "variation": False, "position": pos})
        have.add(r["name"])
    return out
