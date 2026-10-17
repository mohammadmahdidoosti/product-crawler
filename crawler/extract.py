"""Step 3 (part): pull product data out of a page or an API response.

Order, cheapest and most reliable first:
  1. WooCommerce Store API JSON (many Iranian shops expose it)
  2. schema.org Product JSON-LD
  3. WooCommerce product page HTML (price block + attributes table)
  4. Open Graph meta tags
An LLM fallback for pages none of these understand is in llm.py.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from typing import Any

from bs4 import BeautifulSoup

from .normalize import (detect_unit, dominant_unit_in_page, extract_models,
                        extract_phones, fa_digits_to_en, norm_text, parse_number)

REP_WORDS = ("نماینده رسمی", "نمایندگی رسمی", "نماینده انحصاری", "عاملیت فروش",
             "وارد کننده رسمی", "واردکننده رسمی", "official distributor", "authorized distributor")
SALE_UNITS = {"متری": "متر", "متر ": "متر", "رول": "رول", "شاخه": "شاخه", "بسته": "بسته",
              "کارتن": "کارتن", "کیلویی": "کیلوگرم", "کیلوگرم": "کیلوگرم", "جفت": "جفت",
              "عددی": "عدد", "ست ": "ست"}
BRAND_KEYS = ("برند", "brand", "سازنده", "شرکت سازنده", "مارک")
MODEL_KEYS = ("مدل", "model", "کد", "شماره مدل", "پارت نامبر", "part number")


def _clean(s: Any) -> str:
    if s is None:
        return ""
    s = htmllib.unescape(str(s))
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _first(x: Any) -> Any:
    return x[0] if isinstance(x, list) and x else x


def empty_record() -> dict:
    return {"name": "", "brand": "", "model": "", "specs": {}, "short_description": "",
            "price_amount": None, "price_code": "", "price_unit_detected": "",
            "in_stock": "", "image_urls": [], "seller_name": "", "seller_phones": [],
            "claims_representative": "", "sale_unit": "", "extracted_by": "", "page_text": "", "sku": ""}


# ------------------------------------------------------------------ JSON-LD

def _jsonld_blocks(soup: BeautifulSoup) -> list[dict]:
    out: list[dict] = []
    for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            try:  # some themes emit trailing commas / control chars
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.strip()), strict=False)
            except json.JSONDecodeError:
                continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                out.append(d)
                if "@graph" in d:
                    stack.extend(d["@graph"] if isinstance(d["@graph"], list) else [d["@graph"]])
    return out


def _is_product(d: dict) -> bool:
    t = d.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(str(x).lower() in ("product", "productgroup") for x in types)


def from_jsonld(soup: BeautifulSoup) -> dict | None:
    prods = [d for d in _jsonld_blocks(soup) if _is_product(d)]
    if not prods:
        return None
    p = prods[0]
    rec = empty_record()
    rec["extracted_by"] = "jsonld"
    rec["name"] = _clean(p.get("name"))
    b = p.get("brand")
    b = _first(b)
    rec["brand"] = _clean(b.get("name") if isinstance(b, dict) else b)
    rec["model"] = _clean(p.get("mpn") or p.get("model") or "")
    if isinstance(p.get("model"), dict):
        rec["model"] = _clean(p["model"].get("name"))
    rec["short_description"] = _clean(p.get("description"))[:600]
    imgs = p.get("image")
    imgs = imgs if isinstance(imgs, list) else [imgs]
    rec["image_urls"] = [i.get("url") if isinstance(i, dict) else i for i in imgs if i]
    for prop in p.get("additionalProperty") or []:
        if isinstance(prop, dict) and prop.get("name"):
            rec["specs"][_clean(prop["name"])] = _clean(prop.get("value"))
    offers = p.get("offers")
    offers = offers if isinstance(offers, list) else [offers] if offers else []
    for o in offers:
        if not isinstance(o, dict):
            continue
        amt = o.get("price") or o.get("lowPrice")
        if amt is None and isinstance(o.get("priceSpecification"), dict):
            amt = o["priceSpecification"].get("price")
        amt = parse_number(amt)
        if amt:
            rec["price_amount"] = amt
            rec["price_code"] = str(o.get("priceCurrency") or "")
            av = str(o.get("availability") or "").lower()
            rec["in_stock"] = "yes" if "instock" in av else "no" if "outofstock" in av else ""
            break
    rec["sku"] = _clean(p.get("sku"))
    return rec


# ------------------------------------------------------------------ Woo HTML

def from_woo_html(soup: BeautifulSoup) -> dict | None:
    title = soup.select_one("h1.product_title, h1.product-title, .product h1, h1")
    price_el = soup.select_one(".summary .price ins .woocommerce-Price-amount, "
                               ".summary .price .woocommerce-Price-amount, "
                               "p.price .woocommerce-Price-amount, .price .amount")
    if not title or not price_el:
        return None
    rec = empty_record()
    rec["extracted_by"] = "woo_html"
    rec["name"] = _clean(title.get_text())
    rec["price_amount"] = parse_number(price_el.get_text())
    rec["price_unit_detected"] = detect_unit(price_el.get_text()) or ""
    desc = soup.select_one(".woocommerce-product-details__short-description")
    if desc:
        rec["short_description"] = _clean(desc.get_text())[:600]
    return rec


def woo_attributes(soup: BeautifulSoup) -> dict:
    specs = {}
    for tr in soup.select("table.woocommerce-product-attributes tr, table.shop_attributes tr"):
        th, td = tr.find("th"), tr.find("td")
        if th and td:
            specs[_clean(th.get_text())] = _clean(td.get_text())
    return specs


# ------------------------------------------------------------------ OG meta

def from_meta(soup: BeautifulSoup) -> dict | None:
    def meta(*names: str) -> str:
        for n in names:
            m = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if m and m.get("content"):
                return m["content"]
        return ""
    amt = parse_number(meta("product:price:amount", "og:price:amount"))
    name = meta("og:title")
    if not (amt and name):
        return None
    rec = empty_record()
    rec["extracted_by"] = "og_meta"
    rec["name"] = _clean(name)
    rec["price_amount"] = amt
    rec["price_code"] = meta("product:price:currency", "og:price:currency")
    img = meta("og:image")
    rec["image_urls"] = [img] if img else []
    return rec


# ------------------------------------------------------------------ page

def site_name(soup: BeautifulSoup) -> str:
    m = soup.find("meta", attrs={"property": "og:site_name"})
    return _clean(m["content"]) if m and m.get("content") else ""


def extract_page(html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    rec = from_jsonld(soup)
    woo = from_woo_html(soup)
    if rec is None:
        rec = woo or from_meta(soup)
    elif woo and not rec["price_amount"]:
        rec["price_amount"] = woo["price_amount"]
        rec["price_unit_detected"] = woo["price_unit_detected"]
    if rec is None:
        return None
    if woo and woo["price_unit_detected"] and not rec["price_unit_detected"]:
        rec["price_unit_detected"] = woo["price_unit_detected"]
    rec["specs"] = {**woo_attributes(soup), **rec["specs"]}
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    rec["page_text"] = text
    rec["seller_name"] = site_name(soup)
    finalize(rec, text)
    if "made-in-china.com" in html[:30000]:
        made_in_china(rec, BeautifulSoup(html, "lxml"))
    return rec


SKIP_MIC_SPECS = {"shipping cost", "customization", "trademark", "transport package", "origin", "hs code",
                  "production capacity"}


def made_in_china(rec: dict, soup: BeautifulSoup) -> None:
    """Made-in-China pages: the JSON-LD 'brand' is the supplier company and the
    'model' an internal MIC- number. Specs sit in 'Basic Info' blocks."""
    specs = {}
    for item in soup.select(".bsc-item"):
        k, v = item.select_one(".bac-item-label"), item.select_one(".bac-item-value")
        if k and v:
            specs[_clean(k.get_text()).rstrip(":.")] = _clean(v.get_text())[:200]
    for tr in soup.select(".sr-proMainInfo-baseInfo-propertyAttr tr"):
        cells = [_clean(c.get_text()) for c in tr.find_all(["th", "td"])]
        if len(cells) >= 2 and cells[0]:
            specs.setdefault(cells[0].rstrip(":"), cells[1][:200])
    moq = soup.find(string=re.compile(r"\(MOQ\)"))
    if moq:
        node, txt = moq, ""
        for _ in range(3):                      # "(MOQ)" may sit in its own tag next to the number
            node = node.parent
            if node is None:
                break
            txt = _clean(node.get_text(" ")).replace("(MOQ)", "").strip()
            if txt:
                break
        if txt and len(txt) < 40:
            specs["MOQ"] = txt
    company = rec["brand"]
    model = next((v for k, v in specs.items() if k.lower().startswith("model")), "")
    rec["model"] = model if model and model.lower() not in ("customized", "oem", "n/a") else \
        ("" if str(rec["model"]).upper().startswith("MIC-") else rec["model"])
    rec["brand"] = specs.get("Trademark", "") if specs.get("Trademark", "").lower() not in ("oem", "odm", "customized") else ""
    rec["specs"] = {k: v for k, v in specs.items()
                    if k.lower() not in SKIP_MIC_SPECS and not k.lower().startswith("model")}
    rec["seller_name"] = company if company and "made-in-china" not in company.lower() else rec["seller_name"]
    rec["seller_phones"] = []            # numbers in the text are not the supplier's; contact goes via the site
    rec["claims_representative"] = ""
    rec["sku"] = ""


# ------------------------------------------------------------------ Woo Store API

def from_store_api(item: dict) -> dict:
    rec = empty_record()
    rec["extracted_by"] = "woo_store_api"
    rec["name"] = _clean(item.get("name"))
    rec["short_description"] = _clean(item.get("short_description") or item.get("description"))[:600]
    pr = item.get("prices") or {}
    amt = parse_number(pr.get("price"))
    minor = int(pr.get("currency_minor_unit") or 0)
    if amt:
        rec["price_amount"] = amt / (10 ** minor)
    rec["price_code"] = pr.get("currency_code") or ""
    rec["price_unit_detected"] = detect_unit(f"{pr.get('currency_suffix', '')} {pr.get('currency_prefix', '')} "
                                             f"{pr.get('currency_symbol', '')}") or ""
    rec["in_stock"] = "yes" if item.get("is_in_stock") else "no" if item.get("is_in_stock") is False else ""
    rec["image_urls"] = [i.get("src") for i in item.get("images") or [] if i.get("src")]
    for a in item.get("attributes") or []:
        vals = ", ".join(t.get("name", "") for t in a.get("terms") or [])
        if a.get("name") and vals:
            rec["specs"][_clean(a["name"])] = _clean(vals)
    rec["sku"] = _clean(item.get("sku"))
    finalize(rec, rec["short_description"])
    return rec


# ------------------------------------------------------------------ shared

def finalize(rec: dict, text: str) -> None:
    """Fill brand/model/unit/phones/representative from specs and text."""
    specs_norm = {norm_text(k): v for k, v in rec["specs"].items()}
    if not rec["brand"]:
        rec["brand"] = next((v for k, v in specs_norm.items() if any(b in k for b in BRAND_KEYS)), "")
    if not rec["model"]:
        rec["model"] = next((v for k, v in specs_norm.items() if any(m == k or k.startswith(m) for m in MODEL_KEYS)), "")
    if not rec["model"]:
        models = extract_models(rec["name"])
        rec["model"] = models[0] if models else ""
    # a shop's SKU is often its own internal number ("56040"), not the maker's
    # model; use it only if it looks like a model code
    if not rec["model"] and extract_models(rec.get("sku")):
        rec["model"] = rec["sku"]
    name_n = norm_text(rec["name"]) + " "
    rec["sale_unit"] = next((u for w, u in SALE_UNITS.items() if norm_text(w) + " " in name_n + " "), "")
    rec["seller_phones"] = extract_phones(text)
    t = norm_text(text)
    rec["claims_representative"] = "yes" if any(norm_text(w) in t for w in REP_WORDS) else ""


def resolve_price_unit(rec: dict, default_unit: str = "toman") -> tuple[str, list[str]]:
    """Decide rial / toman / usd for rec['price_amount']; return (unit, flags).

    Schema currency codes are unreliable on Iranian shops (IRR is often used
    for toman prices), so the visible page text wins when it is clear.
    """
    flags: list[str] = []
    amt = rec.get("price_amount")
    code = str(rec.get("price_code") or "").upper()
    if code == "USD" or code == "$":
        return "usd", flags
    text = fa_digits_to_en(rec.get("page_text") or "").replace(",", "").replace("٬", "")
    if amt and text:
        a = int(amt)
        if re.search(rf"(?<!\d){a}\s*(تومان|تومن)", text):
            return "toman", flags
        if re.search(rf"(?<!\d){a}\s*ریال", text):
            return "rial", flags
        if a % 10 == 0 and re.search(rf"(?<!\d){a // 10}\s*(تومان|تومن)", text):
            return "rial", flags
        if re.search(rf"(?<!\d){a * 10}\s*ریال", text):
            return "toman", flags
    if rec.get("price_unit_detected"):
        return rec["price_unit_detected"], flags
    by_code = {"IRT": "toman", "IRR": "rial", "TOMAN": "toman", "RIAL": "rial"}.get(code)
    page_unit = dominant_unit_in_page(rec.get("page_text") or "")
    if by_code and page_unit and by_code != page_unit:
        flags.append("currency_code_vs_page")
        return page_unit, flags
    if by_code:
        return by_code, flags
    if page_unit:
        return page_unit, flags
    flags.append("unit_assumed")
    return default_unit, flags
