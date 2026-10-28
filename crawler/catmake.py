"""New categories from the waiting list (no_category.csv), made once on the site and in the crawler.

Input: a decisions table, one row per product page: group, path ('ابزار برقی و شارژی > میخ کوب >
میخ نامریی'), url. For every level of every path that the taxonomy does not have yet:

  * a WooCommerce product category is made on the site under its parent (or the existing one with
    the same name under that parent is reused, so running it twice makes nothing twice)
  * a row is added to DATA_DIR/taxonomy.csv (new ID, levels, breadcrumb, Parent_ID)
  * the ID -> WordPress term is added to DATA_DIR/wp_categories.csv

Each page then gets a placement (DATA_DIR/category_placements.csv: url -> taxonomy ID). The dedupe
step puts placed pages in their category before the waiting list is written, so the next
dedupe → export → publish of their batch sends them like any other product.

Category names follow the shop's rules (clean_name / bad_name): no 'قیمت'/'خرید', no emoji or ⚡,
no other shop's name, no date, no English word glued to a Persian one ('باغبانیgardening'), and
nothing outside what the shop sells (game consoles, kitchen appliances).
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

import pandas as pd

from .io import read_csv, write_csv

log = logging.getLogger(__name__)

PLACEMENTS = "category_placements.csv"
PLACE_COLS = ["url", "taxonomy_id", "breadcrumb", "group"]
MAX_LEVELS = 4

SHOP_NAMES = ("نید صنعت", "نیدصنعت", "بیشه کالا", "ایران ابزار", "کالا عمران", "کالاعمران", "ابزارت")
OUT_OF_SCOPE = ("playstation", "پلی استیشن", "آب مرکبات", "اب مرکبات", "هدست واقعیت", "متا کویست",
                "اسپیکر بلوتوث", "دسته بازی", "گوشتکوب", "گوشت کوب", "زودپز", "سالاد ساز", "شیور صورت",
                "ماساژور", "جاروبرقی سامسونگ", "فر کننده", "چراغ کمپی")
NOT_A_NAME = ("تاریخ", "ارسال فوری", "بهترین دستگاه", "همه محصولات", "مشخصات فنی", "لیست", "نکات طلایی")
GARDEN = "ابزار و ماشین‌آلات کشاورزی و باغبانی"
_EMOJI = re.compile(r"[☀-➿\U0001F000-\U0001FAFF️]")
_MARKS = re.compile(r"[‎‏‪-‮]")
_GLUED = re.compile(r"(?<=[؀-ۿ])[A-Za-z]+|[A-Za-z]+(?=[؀-ۿ])")
_DATE = re.compile(r"\d{4}[-/]\d{1,2}|\b1[34]\d\d\b")


def clean_name(name: str) -> str:
    """The shop's rules applied to one category name; '' when nothing is left."""
    s = _MARKS.sub("", str(name or ""))
    s = _EMOJI.sub("", s)
    s = _GLUED.sub("", s)
    for w in ("قیمت و خرید", "خرید و قیمت", "قیمت", "خرید") + SHOP_NAMES:
        s = s.replace(w, " ")
    return re.sub(r"\s+", " ", s).strip(" -|/")


def bad_name(name: str) -> str | None:
    """Why this can never be a category name, or None."""
    s = str(name or "").strip()
    low = s.lower()
    if not s:
        return "خالی"
    if any(w in low for w in OUT_OF_SCOPE):
        return "به کار سایت نمی‌آید"
    if _DATE.search(s) or any(s.startswith(w) for w in NOT_A_NAME):
        return "اسم دسته نیست"
    if clean_name(s) != s:
        return "قیمت/خرید، ایموجی، اسم فروشگاه یا انگلیسی چسبیده دارد"
    return None


def garden_parent(path: str) -> str:
    """Gardening never sits under power tools: 'ابزار برقی و شارژی > X > اره باغبانی' -> under GARDEN."""
    parts = [p.strip() for p in path.split(">") if p.strip()]
    if parts and parts[0] != GARDEN and any("باغبانی" in p for p in parts[1:]):
        return " > ".join([GARDEN, "ابزار باغبانی", parts[-1]] if parts[-1] != "ابزار باغبانی"
                          else [GARDEN, "ابزار باغبانی"])
    return " > ".join(parts)


# ------------------------------------------------------------------ taxonomy files

def _paths(cfg: dict) -> tuple[Path, Path]:
    d = cfg["_data_dir"]
    tax = Path(cfg.get("taxonomy", {}).get("file") or d / "taxonomy.csv")
    wp = d / "wp_categories.csv"
    if not wp.exists():
        src = Path(__file__).resolve().parent.parent / "assets" / "wp_categories.csv"
        if src.exists():
            shutil.copy(src, wp)
    return tax, wp


def _col(df: pd.DataFrame, prefix: str) -> str | None:
    return next((c for c in df.columns if c.strip().lower().startswith(prefix.lower())), None)


class Tree:
    """The raw taxonomy file (every column kept) plus the WordPress map, with new rows appended."""

    def __init__(self, cfg: dict):
        self.tax_p, self.wp_p = _paths(cfg)
        self.raw = read_csv(self.tax_p)
        self.c_id, self.c_path = _col(self.raw, "ID"), _col(self.raw, "Full Breadcrumb")
        self.c_parent = _col(self.raw, "Parent_ID")
        self.levels = [_col(self.raw, f"Level {i}") for i in range(1, MAX_LEVELS + 1)]
        self.by_path = {}
        for i, p in zip(self.raw[self.c_id], self.raw[self.c_path]):
            self.by_path.setdefault(self._key(p), str(i).strip())
        self.wp = read_csv(self.wp_p) if self.wp_p.exists() else pd.DataFrame(columns=["ID", "wp_id"])
        self.wp_of = {a.strip(): b.strip() for a, b in zip(self.wp["ID"], self.wp["wp_id"]) if str(b).strip()}
        ids = pd.to_numeric(self.raw[self.c_id], errors="coerce").dropna()
        self.next_id = int(ids.max()) + 1 if len(ids) else 1
        self.new_rows: list[dict] = []
        self.new_wp: list[dict] = []
        self.backed_up = False

    @staticmethod
    def _key(path: str) -> str:
        return " > ".join(re.sub(r"[\s‌]+", " ", p).strip() for p in str(path).split(">"))

    def find(self, path: str) -> str | None:
        return self.by_path.get(self._key(path))

    def add(self, parts: list[str], parent_id: str, wp_id: str) -> str:
        tid = str(self.next_id)
        self.next_id += 1
        row = {c: "" for c in self.raw.columns}
        row[self.c_id] = tid
        for c, v in zip(self.levels, parts):
            if c:
                row[c] = v
        row[self.c_path] = " > ".join(parts)
        if self.c_parent:
            row[self.c_parent] = parent_id
        for prefix, v in (("Is_Inferred", "False"), ("Source", "no-category"), ("Level", str(len(parts))),
                          ("Category_Name", parts[-1]), ("Parent_Name", parts[-2] if len(parts) > 1 else "")):
            c = next((c for c in self.raw.columns if c.strip() == prefix or c.strip().startswith(prefix + " ")), None)
            c = c or _col(self.raw, prefix)
            if c and c not in self.levels and c not in (self.c_id, self.c_path, self.c_parent):
                row[c] = v
        self.new_rows.append(row)
        self.by_path[self._key(row[self.c_path])] = tid
        if wp_id:
            self.wp_of[tid] = wp_id
            self.new_wp.append({"ID": tid, "wp_id": wp_id})
        return tid

    def save(self) -> None:
        if not self.new_rows:
            return
        if not self.backed_up:                 # one backup per run, before the first change
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy(self.tax_p, self.tax_p.with_name(f"{self.tax_p.name}.bak-{stamp}"))
            if self.wp_p.exists():
                shutil.copy(self.wp_p, self.wp_p.with_name(f"{self.wp_p.name}.bak-{stamp}"))
            self.backed_up = True
        write_csv(pd.concat([self.raw, pd.DataFrame(self.new_rows)], ignore_index=True), self.tax_p)
        if self.new_wp:
            write_csv(pd.concat([self.wp, pd.DataFrame(self.new_wp)], ignore_index=True), self.wp_p)


# ------------------------------------------------------------------ site terms

def _site_term(wc, name: str, parent_wp: int) -> int:
    """The site's category with this name under this parent, made if missing."""
    found = wc._call(lambda: wc._wc("GET", "products/categories",
                                    params={"search": name, "parent": parent_wp, "per_page": 100}),
                     f"category search {name}")
    norm = lambda s: re.sub(r"[\s‌]+", " ", str(s)).strip()         # noqa: E731
    for t in found or []:
        if norm(t.get("name", "")).replace("&amp;", "&") == norm(name) and int(t.get("parent") or 0) == parent_wp:
            return int(t["id"])
    r = wc._wc("POST", "products/categories", json={"name": name, "parent": parent_wp})
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.ok and j.get("id"):
        return int(j["id"])
    rid = (j.get("data") or {}).get("resource_id") if isinstance(j, dict) else None
    if j.get("code") == "term_exists" and rid:       # the search missed it (spelling): the site says which one
        return int(rid)
    from .wc import WCError
    raise WCError(f"category create {name}: HTTP {r.status_code} {j.get('code', '')} {j.get('message', '')}", r.status_code)


# ------------------------------------------------------------------ the tool

def make(cfg: dict, decisions: pd.DataFrame, dry_run: bool = True, wc=None) -> dict:
    """decisions: group, path, url. Returns what was (or would be) made and placed."""
    tree = Tree(cfg)
    if not dry_run and wc is None:
        from .wc import WC
        wc = WC()
    rejected, made, reused, placements = [], [], [], []
    done: dict[str, str] = {}
    for path, part in decisions.groupby("path", sort=False):
        path = garden_parent(path)
        parts = [p.strip() for p in path.split(">") if p.strip()]
        why = next((f"{p}: {bad_name(p)}" for p in parts[1:] if bad_name(p)), None)
        if len(parts) < 2 or len(parts) > MAX_LEVELS:
            why = why or f"{len(parts)} سطح"
        if why:
            rejected.append({"path": path, "why": why, "pages": len(part)})
            continue
        if tree.find(parts[0]) is None:
            rejected.append({"path": path, "why": "دستهٔ اصلی در سایت نیست", "pages": len(part)})
            continue
        tid = None
        for i in range(1, len(parts) + 1):
            sub = " > ".join(parts[:i])
            tid = tree.find(sub)
            if tid is not None:
                if sub not in done:
                    reused.append(sub)
                    done[sub] = tid
                continue
            parent = tree.find(" > ".join(parts[:i - 1]))
            parent_wp = tree.wp_of.get(parent, "")
            wp_id = ""
            if not dry_run:
                if not parent_wp:
                    raise ValueError(f"دستهٔ بالای «{sub}» در وردپرس نگاشت ندارد")
                wp_id = str(_site_term(wc, parts[i - 1], int(parent_wp)))
            tid = tree.add(parts[:i], parent, wp_id or ("" if not dry_run else "dry"))
            made.append({"path": sub, "taxonomy_id": tid, "wp_id": wp_id})
            done[sub] = tid
            if not dry_run:
                tree.save()                    # after every term: a stop half way never loses a made term
                tree.new_rows.clear()
                tree.raw = read_csv(tree.tax_p)
                tree.wp = read_csv(tree.wp_p)
                tree.new_wp.clear()
        for r in part.to_dict("records"):
            placements.append({"url": r["url"], "taxonomy_id": tid, "breadcrumb": path, "group": r.get("group", "")})
    if not dry_run:
        tree.save()
        p = cfg["_data_dir"] / PLACEMENTS
        old = read_csv(p) if p.exists() else pd.DataFrame(columns=PLACE_COLS)
        new = pd.DataFrame(placements, columns=PLACE_COLS)
        write_csv(pd.concat([old, new], ignore_index=True).drop_duplicates("url", keep="last"), p)
    log.info("category maker%s: %d new categories, %d existing used, %d pages placed, %d paths refused",
             " (dry run)" if dry_run else "", len(made), len(reused), len(placements), len(rejected))
    return {"dry_run": dry_run, "made": made, "reused": len(reused), "placed": len(placements),
            "rejected": rejected}


def placements(cfg: dict) -> dict[str, dict]:
    p = cfg["_data_dir"] / PLACEMENTS
    if not p.exists():
        return {}
    return {r["url"]: r for r in read_csv(p).to_dict("records") if str(r.get("taxonomy_id", "")).strip()}


def apply_placements(cfg: dict, df: pd.DataFrame, crawled: pd.DataFrame) -> pd.DataFrame:
    """Pages a person placed in a category (category maker) join the run in that category, before
    the waiting list is written. A page the run already placed keeps its own category too."""
    pl = placements(cfg)
    if not pl or crawled is None or crawled.empty:
        return df
    have = set(zip(df["url"], df["taxonomy_id"].astype(str))) if len(df) else set()
    add = []
    for url, rows in crawled[crawled["url"].isin(pl)].groupby("url", sort=False):
        x = pl[url]
        if (url, str(x["taxonomy_id"])) in have:
            continue
        r = rows.iloc[0].to_dict()
        r.update({"taxonomy_id": str(x["taxonomy_id"]), "breadcrumb": x["breadcrumb"], "relevant": True,
                  "relevance_confidence": 1.0, "relevance_reason": "دسته‌ی ساخته‌شده از فهرست انتظار"})
        add.append(r)
    if add:
        log.info("placed by hand (new categories): %d page(s)", len(add))
        df = pd.concat([df, pd.DataFrame(add)], ignore_index=True)
    return df
