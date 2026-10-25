"""Second chance: every usable page a run crawled goes to every empty category it fits.

A page is crawled while searching for one category; when it is another model or brand it is
rejected there, but it is still a good product (price, photos, a shop). Here each such page —
and each page that was kept, for more categories — is checked against the empty categories of
the whole site tree (taxonomy nodes with no products on the site), not just the run's queue.
The same filter rules decide (the brand or use a sub-category adds, cordless or corded, number of
modes, country, model numbers), at a higher confidence than the run's own filter. A page that fits
both a category and one of its parents goes to the deeper one only.

The rows added look like filter output, with relevance_reason starting 'شانس دوم:', so the merge
step treats them as any other match: one product per model, best category main, the others extra.
Persian-named pages only (Iranian shops): a Chinese supplier's page stays with its own route.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd

from .normalize import norm_text

log = logging.getLogger(__name__)
FA = re.compile(r"[؀-ۿ]")
PREFIX = "شانس دوم: "


class GapIndex:
    """Empty taxonomy nodes, found by the first word of their name."""

    def __init__(self, cfg: dict, empty_only: bool = True):
        from .rules import STOP
        from .taxonomy import load_taxonomy
        p = Path(cfg.get("taxonomy", {}).get("file") or "")
        self.nodes: list[dict] = []
        self.by_word: dict[str, list[int]] = {}
        if not p.exists():
            return
        t = load_taxonomy(p)
        skip = {norm_text(x) for x in cfg.get("taxonomy", {}).get("exclude_level1", [])}
        t = t[((t["site_product_count"] == 0) | (not empty_only)) & ~t["level1"].map(norm_text).isin(skip) & (t["leaf"] != "")]
        for r in t.to_dict("records"):
            words = [w for w in norm_text(r["leaf"]).split() if w not in STOP and len(w) > 1]
            if not words:
                continue
            i = len(self.nodes)
            self.nodes.append({"taxonomy_id": r["taxonomy_id"], "breadcrumb": r["breadcrumb"], "leaf": r["leaf"],
                               "words": set(words), "head": words[0]})
            self.by_word.setdefault(words[0], []).append(i)

    def candidates(self, name: str) -> list[dict]:
        nm = norm_text(name)
        ws, flat = set(nm.split()), nm.replace(" ", "")
        out = []
        for w in ws:
            for i in self.by_word.get(w, []):
                n = self.nodes[i]
                inside = sum(1 for x in n["words"] if x in ws or x in flat)
                if inside / len(n["words"]) >= 0.5:          # cheap test before the full rules
                    out.append(n)
        return out


LEAD_SKIP = {norm_text(w) for w in ("ست", "پک", "مجموعه", "کیت", "بسته", "جفت", "عدد", "عددی", "دستگاه", "خرید", "قیمت", "و")}


# shop words for the same thing as a category word ('اره زنجیری' is an 'اره موتوری')
SAME = {norm_text(a): {norm_text(x) for x in b} for a, b in (("موتوری", ("زنجیری",)), ("زنجیری", ("موتوری",)))}


def starts_as(name: str, node: dict) -> bool:
    """The product IS the node's thing: its name starts with the node's first word (after 'کیت',
    'ست', counts…), or with the second when the first is implied ('بتن کن' for 'دریل بتن کن').
    'ست خم کن … لوله' is a pipe bender, not a pipe."""
    ws = [w for w in norm_text(name).split() if w not in LEAD_SKIP and not re.fullmatch(r"\d+", w)]
    if not ws:
        return False
    lw = norm_text(node["leaf"]).split()
    if ws[0] == norm_text("اره") and lw[0] != ws[0] and len(ws) > 1:   # 'اره پروفیل بر …' is a 'پروفیل بر'
        ws = ws[1:]
    return ws[0] == lw[0] or (len(lw) > 1 and ws[0] == lw[1])


UNSURE = {norm_text(w) for w in ("قدیمی", "جدید", "اصل", "اصلی", "اورجینال", "کپی", "طرح", "درجه", "ارزان", "باکیفیت")}
POWER = {norm_text(w) for w in ("موتوری", "شارژی", "بنزینی", "برقی", "دیزلی", "گازی", "پنوماتیک", "پنوماتیکی", "بادی", "هیدرولیک", "هیدرولیکی")}


def type_ok(node: dict, name: str) -> bool:
    """The product is what every category above the node is: under '… > اره درخت بر > اره درخت بر
    شارژی' the name must say 'اره درخت بر' (not 'اره عمود بر'). A title with 'و' ('اره شارژی و
    برقی') is met by any of its parts."""
    from .rules import ADJ, DECISIVE, NUM_WORDS, ORIGIN_WORDS, STOP, UNITS
    nm = norm_text(name)
    ws, flat = set(nm.split()), nm.replace(" ", "")
    parts = [p.strip() for p in node["breadcrumb"].split(">")]
    for title in parts[1:-1]:
        segs = re.split(r"\s+و\s+|،|,", norm_text(title))
        ok = False
        for seg in segs:
            need = [w for w in seg.split() if w not in STOP and w not in ADJ and w not in DECISIVE and w not in UNITS
                    and w not in NUM_WORDS and w not in ORIGIN_WORDS and not re.search(r"\d", w) and len(w) > 1]
            if need and all(w in ws or w in flat for w in need):
                ok = True
                break
            if not need:
                ok = True
        if not ok:
            return False
    return True


def strict(node: dict, r: dict) -> str | None:
    """Second-chance checks on top of the filter: what cannot be confirmed from the name is a no."""
    from .rules import _mode, judge
    name = str(r.get("name") or "")
    nm = set(norm_text(name).split())
    lf = set(norm_text(node["leaf"]).split())
    if lf & UNSURE:
        return "دسته‌ای که از روی نام قابل تشخیص نیست (قدیمی/اصل/…)"
    if not starts_as(name, node) or not type_ok(node, name):
        return "نوع محصول با دسته فرق دارد"
    from .rules import ADJ, ORIGIN_WORDS, STOP, UNITS, leaf_brand_word
    nmn = norm_text(name)
    flat = nmn.replace(" ", "")
    lw = [w for w in norm_text(node["leaf"]).split()]
    need = [w for w in lw if w not in STOP and w not in ADJ and w not in UNITS and w not in ORIGIN_WORDS
            and not re.search(r"\d", w) and not leaf_brand_word(w, node["leaf"])]
    implied = lw[:1] if len(lw) > 1 and nmn.split()[:1] == lw[1:2] else []     # 'عمود بر …' is an 'اره عمود بر'
    missing = [w for w in need if w not in nm and w not in flat and w not in implied
               and not (SAME.get(w, set()) & nm)]
    # the first two words name the thing ('اره عمود', 'موتور گیربکس'): both must be there; of the
    # rest one may be left out ('موتور و گیربکس آسانسور' takes 'موتور گیربکس بهران')
    if any(w in missing for w in need[:2]) or len(missing) > 1:   # 'اره افقی بر' is not an 'اره عمود بر'
        return f"«{' '.join(missing)}» در نام نیست"
    missing_power = [w for w in lf & POWER if w not in nm and not (SAME.get(w, set()) & nm)]
    if missing_power:
        return f"«{missing_power[0]}» در نام نیست"
    if _mode(node["leaf"]) and _mode(name) != _mode(node["leaf"]):
        return "تعداد حالت در نام نیست"
    if "دستی" in lf and "دستی" not in nm:
        return "«دستی» در نام نیست"
    parts = [p.strip() for p in node["breadcrumb"].split(">")]
    if len(parts) >= 3:                       # it must also be what the parent category is
        pv = judge(" > ".join(parts[:-1]), parts[-2], r)
        if not pv["relevant"] or float(pv["confidence"]) < 0.75:
            return "با دسته‌ی بالاتر هم نمی‌خواند: " + pv["reason"]
    return None


def _deepest(nodes: list[dict]) -> list[dict]:
    """A page fitting 'اره عمود بر' and 'اره عمود بر > اره عمود بر رونیکس' goes to the second only."""
    crumbs = [n["breadcrumb"] for n in nodes]
    return [n for n in nodes if not any(c != n["breadcrumb"] and c.startswith(n["breadcrumb"] + " >") for c in crumbs)]


def harvest(cfg: dict, df: pd.DataFrame, crawled: pd.DataFrame, index: GapIndex | None = None,
            min_conf: float | None = None, progress=None) -> pd.DataFrame:
    """df: the run's matches (filter output after cross_assign). Returns df with the second-chance
    rows added."""
    from .rules import judge
    if crawled is None or crawled.empty:
        return df
    index = index or GapIndex(cfg)
    if not index.nodes:
        log.info("second chance: no taxonomy file, skipped")
        return df
    min_conf = float(min_conf if min_conf is not None else cfg.get("dedupe", {}).get("harvest_confidence", 0.85))
    have = set(df["taxonomy_id"] + "|" + df["url"]) if len(df) else set()
    pages = crawled[(crawled["price_toman"] != "") & (crawled["image_urls"] != "")
                    & crawled["name"].map(lambda n: bool(FA.search(str(n))))
                    & (crawled.get("source_type", "") != "foreign")].drop_duplicates("url")
    add, placed = [], set()
    rows = pages.to_dict("records")
    for k, r in enumerate(rows):
        if progress and k % 50 == 0:
            progress(k, len(rows))
        fits = []
        for n in index.candidates(r.get("name", "")):
            if f"{n['taxonomy_id']}|{r['url']}" in have:
                continue
            v = judge(n["breadcrumb"], n["leaf"], r)
            if v["relevant"] and float(v["confidence"]) >= min_conf and strict(n, r) is None:
                fits.append({**n, "v": v})
        for n in _deepest(fits):
            add.append({**r, "taxonomy_id": n["taxonomy_id"], "breadcrumb": n["breadcrumb"], "query": n["leaf"],
                        "relevant": True, "relevance_confidence": str(n["v"]["confidence"]),
                        "relevance_reason": PREFIX + n["v"]["reason"]})
            have.add(f"{n['taxonomy_id']}|{r['url']}")
            placed.add(r["url"])
    if add:
        log.info("second chance: %d page(s) placed in %d more category match(es) across the site tree",
                 len(placed), len(add))
        df = pd.concat([df, pd.DataFrame(add)], ignore_index=True).fillna("")
    return df


UP = "دسته‌ی بالاتر: "


def place_up(cfg: dict, df: pd.DataFrame, crawled: pd.DataFrame, min_conf: float = 0.75) -> pd.DataFrame:
    """Nothing good is thrown away: a page that fits no category after the filter and the second
    chance (an elevator motor whose country no page states, a corded saw found under a cordless
    one) goes to the nearest category above the one it was found for that it really is, with the
    same strict checks; when nothing above fits (a petrol chainsaw found under 'اره شارژی'), to
    the deepest category anywhere in the site tree that fits it. Never a whole top-level section."""
    from .rules import judge
    from .taxonomy import load_taxonomy
    p = Path(cfg.get("taxonomy", {}).get("file") or "")
    if crawled is None or crawled.empty or not p.exists():
        return df
    t = load_taxonomy(p)
    parent = dict(zip(t["taxonomy_id"], t["parent_id"]))
    node = {r["taxonomy_id"]: r for r in t.to_dict("records")}
    placed = set(df["url"]) if len(df) else set()
    pages = crawled[(crawled["price_toman"] != "") & (crawled["image_urls"] != "")
                    & crawled["name"].map(lambda n: bool(FA.search(str(n))))
                    & (crawled.get("source_type", "") != "foreign") & ~crawled["url"].isin(placed)]
    add, full = [], None
    for url, rows in pages.groupby("url", sort=False):
        r = rows.iloc[0].to_dict()
        best = None
        for tid in dict.fromkeys(rows["taxonomy_id"]):
            pt, depth = parent.get(tid, ""), 0
            while pt and pt in node and depth < 4:
                n = node[pt]
                if n["breadcrumb"].count(">") < 1:
                    break
                nd = {"taxonomy_id": pt, "breadcrumb": n["breadcrumb"], "leaf": n["leaf"]}
                v = judge(n["breadcrumb"], n["leaf"], r)
                if v["relevant"] and float(v["confidence"]) >= min_conf and strict(nd, r) is None:
                    if best is None or n["breadcrumb"].count(">") > best[0]["breadcrumb"].count(">"):
                        best = (nd, v)
                    break
                pt, depth = parent.get(pt, ""), depth + 1
        why = UP
        if best is None:                     # nothing above fits: the best category anywhere in the tree
            full = full or GapIndex(cfg, empty_only=False)
            fits = []
            for n in full.candidates(r.get("name", "")):
                if n["breadcrumb"].count(">") < 1:          # not a whole top-level section
                    continue
                v = judge(n["breadcrumb"], n["leaf"], r)
                # every word of the category is in the name (strict): judge's share may be lower
                # when the shop uses another word for the same thing ('زنجیری' for 'موتوری')
                if v["relevant"] and float(v["confidence"]) >= min_conf and strict(n, r) is None:
                    fits.append((n, v))
            deep = _deepest([n for n, _ in fits])
            if deep:
                n = max(deep, key=lambda x: x["breadcrumb"].count(">"))
                best, why = (n, next(v for m, v in fits if m is n)), "جای دیگر درخت: "
        if best:
            nd, v = best
            add.append({**r, "taxonomy_id": nd["taxonomy_id"], "breadcrumb": nd["breadcrumb"], "query": nd["leaf"],
                        "relevant": True, "relevance_confidence": str(v["confidence"]),
                        "relevance_reason": why + v["reason"]})
    if add:
        log.info("one level up: %d page(s) that fit no category placed in the nearest category above", len(add))
        df = pd.concat([df, pd.DataFrame(add)], ignore_index=True).fillna("")
    return df
