"""Failed site searches ("جستجوهای بی‌جواب"): what visitors looked for on caravansanat.com and
did not find, turned into crawl work only after a person approves it.

The list comes from the WebiSearch plugin on the site (Search Logger > جستجوهای بی‌نتیجه >
«در محصولات شما نیست» > «خروجی همین فهرست (CSV)»); the plugin has no API, so a person uploads
that file. Nothing on the site is read or changed here.

1. upload()   the CSV is read (any Excel encoding), each search term is cleaned:
               Persian/Arabic letters and digits, half-spaces, words glued to numbers
               ('90درجه'), words glued together ('کمربندسایز'), a Latin word typed with the
               keyboard in English ('uhdr' -> 'عایق'), spelling against our own vocabulary.
2. clusters    terms that are one search are put together: the same after cleaning, one is the
               start of another ('گیج ف' / 'گیج فش'), one model code ('Airtac4v210-08' /
               '4v210-060'), or a run of attempts typed one after another ('فلسایز 355',
               'فانسایز 355', …: within a few minutes and alike). The most searched one speaks.
3. kind        product | model (a part number) | brand (only a brand) | not_product (an order,
               an invoice, an e-mail…) | unclear (no word of it is known to us) | fragments.
4. category    the best taxonomy category for the cleaned term, with a score and the next two
               candidates; below the bar it is 'no category' (a new category may be needed).
5. proposal    approve (a clear product with a good category) or reject (not a product,
               unclear) — only a proposal: every row waits for a person's decision.
6. queue       approved rows that have a category become a crawl run 'search-…' (one queue row
               per category; its searches are the approved terms). Starting that crawl is a
               separate click, and publishing its products is the usual separate step.

Files in DATA_DIR/searches/: uploads/<time>.csv (the raw files), review.csv (one row per
cluster of the latest upload) and decisions.csv (a person's decisions, kept across uploads by
the cluster's key, so the same term is not asked twice).
"""
from __future__ import annotations

import difflib
import io
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .io import QUEUE_COLS, read_csv, write_csv
from .normalize import norm_text

log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Tehran")

REVIEW_COLS = ["key", "term", "searches", "last_search", "members", "cleaned", "kind", "proposal", "doubt", "note",
               "taxonomy_id", "breadcrumb", "match_score", "site_product_count", "alternatives",
               "decision", "query", "decided_taxonomy_id", "decided_breadcrumb", "decided_at", "queued_run"]
DECISION_COLS = ["key", "decision", "query", "taxonomy_id", "breadcrumb", "decided_at", "queued_run", "term"]
KIND_FA = {"product": "محصول", "model": "کد مدل", "brand": "فقط برند", "not_product": "محصول نیست",
           "unclear": "نامفهوم"}
NOT_PRODUCT = [norm_text(w) for w in (
    "فاکتور", "ایمیل", "سفارش", "پیگیری", "ورود", "رمز", "حساب کاربری", "ثبت نام", "تماس", "آدرس", "شماره تماس",
    "پرداخت", "ارسال ایمیل", "کد رهگیری", "مرجوعی", "لغو", "اقساط", "تخفیف", "کد تخفیف", "درباره ما", "همکاری",
)]
TERM_HEAD = ("عبارت", "term", "search", "کلمه", "جستجو شده")
COUNT_HEAD = ("جستجوها", "تعداد", "count", "searches", "hits")
DATE_HEAD = ("آخرین", "last", "date", "تاریخ")
STATUS_HEAD = ("وضعیت", "status", "group")
NOT_IN_CATALOG = norm_text("در محصولات شما نیست")
CODE = re.compile(r"^(?=.*\d)(?=.*[a-z])[a-z0-9][a-z0-9\-/.]{2,}$")
# everyday words of a product search: known even when no category or product uses them
COMMON = {norm_text(w) for w in (
    "سایز", "سایزی", "اینچ", "اینچی", "میل", "میلی", "متر", "متری", "میلیمتر", "سانت", "درجه", "کیلووات", "وات", "ولت",
    "آمپر", "اسب", "فاز", "تک", "دو", "سه", "چهار", "یک", "طرفه", "دار", "دوبل", "فلزی", "پلاستیکی", "استیل", "برنجی",
    "چدنی", "آهنی", "مسی", "گالوانیزه", "پلیمری", "کوچک", "بزرگ", "مینی", "صنعتی", "خانگی", "ارسال", "خرید", "قیمت",
    "باز", "کردن", "دیدن", "راهی", "زانو", "بوش", "بوشن", "تبدیل", "مغزی", "مغزي", "درپوش", "رابط", "شیلنگ", "لوله",
    "پمپ", "شیر", "فیلتر", "موتور", "الکترو", "الکتروموتور", "انتهایی", "خارج", "داخل", "نر", "ماده", "رزوه", "رزو",
    "ایمیل", "پیامک", "فاکتور", "سفارش", "بوبین", "سوکت", "کنتاکتور",
)}
SUFFIXES = ("های", "ها", "ای", "ی")
# a Persian word typed with the keyboard left on English (standard Persian layout)
LAYOUT = dict(zip("qwertyuiop[]asdfghjkl;'zxcvbnm,", "ضصثقفغعهخحجچشسیبلاتنمکگظطزرذدئو"))


def settings(cfg: dict) -> dict:
    return {"match_min": 0.75, "match_doubt": 0.5, "typing_minutes": 5, "max_queries_per_category": 3,
            **(cfg.get("searches") or {})}


def sdir(cfg: dict) -> Path:
    d = Path(cfg["_data_dir"]) / "searches"
    (d / "uploads").mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ reading the plugin's file

def _col(cols: list[str], heads: tuple) -> str | None:
    for h in heads:
        for c in cols:
            if norm_text(h) in norm_text(c):
                return c
    return None


def read_export(df: pd.DataFrame) -> pd.DataFrame:
    """term, searches, last_search from the plugin's CSV (Persian or English headers); rows of
    other groups ('نتیجه نزدیک', …) are left out when the file has a status column."""
    cols = [str(c) for c in df.columns]
    tc = _col(cols, TERM_HEAD) or cols[0]
    cc, dc, sc = _col(cols, COUNT_HEAD), _col(cols, DATE_HEAD), _col(cols, STATUS_HEAD)
    if sc:
        df = df[df[sc].map(norm_text).isin({NOT_IN_CATALOG, "", "not in catalog", "not_in_catalog"})]
    out = pd.DataFrame({"term": df[tc].astype(str).str.strip(),
                        "searches": pd.to_numeric(df[cc], errors="coerce").fillna(1).astype(int) if cc else 1,
                        "last_search": df[dc].astype(str).str.strip() if dc else ""})
    out = out[out["term"].map(lambda t: bool(norm_text(t)))]
    return out.reset_index(drop=True)


# ------------------------------------------------------------------ vocabulary and cleaning

class Vocab:
    """Words we know: the taxonomy (names and synonyms), brands, and names of crawled products."""

    def __init__(self, cfg: dict, tax: pd.DataFrame | None):
        from collections import Counter
        self.count: Counter = Counter()
        self.brands: set[str] = set()
        if tax is not None:
            for b in tax["breadcrumb"]:
                self.count.update(w for w in norm_text(b.replace(">", " ")).split() if len(w) > 1)
            if "synonyms" in tax.columns:
                for s in tax["synonyms"]:
                    self.count.update(w for w in norm_text(str(s).replace("|", " ")).split() if len(w) > 1)
        from .config import ROOT
        bp = ROOT / "brands.csv"
        if bp.exists():
            b = read_csv(bp)
            for c in ("brand", "canonical"):
                if c in b.columns:
                    self.brands |= {norm_text(x) for x in b[c] if norm_text(x)}
        self.count.update(w for b in self.brands for w in b.split())
        from .rules import ADJ, NEGATIVE, NUM_WORDS, ORIGIN_WORDS, STOP, UNITS
        self.count.update(COMMON | ADJ | NUM_WORDS | ORIGIN_WORDS | STOP | UNITS
                          | {w for x in NEGATIVE for w in x.split()})
        pp = Path(cfg["_data_dir"]) / "crawl_pool.csv"
        if pp.exists():
            try:
                names = pd.read_csv(pp, encoding="utf-8-sig", dtype=str, usecols=["name"], keep_default_na=False)["name"]
                for n in names:
                    self.count.update(w for w in norm_text(n).split() if len(w) > 1 and not re.search(r"\d", w))
            except (ValueError, OSError):
                pass
        self.words = {w for w, n in self.count.items()}
        self._by_len: dict[int, list[str]] = {}
        for w in self.words:
            if not re.search(r"\d", w):
                self._by_len.setdefault(len(w), []).append(w)

    def known(self, w: str) -> bool:
        if w in self.words or re.search(r"\d", w) or CODE.match(w):
            return True
        # 'موتوری', 'شیرهای', 'فیلترها': a known word with a Persian ending
        return any(w.endswith(x) and len(w) - len(x) >= 3 and w[:-len(x)] in self.words for x in SUFFIXES)

    def closest(self, w: str) -> str | None:
        """A careful spelling fix: a short word (up to 5 letters) only by one changed letter
        ('نراز' -> 'تراز'), a longer one by one letter more, less or changed, or two when it is
        long ('کاکتور' -> 'کنتاکتور' is not taken: two letters in a six-letter word)."""
        if len(w) < 4 or re.search(r"\d", w) or re.fullmatch(r"[a-z\-]+", w):
            return None
        if len(w) <= 5:
            pool = [x for x in self._by_len.get(len(w), []) if sum(a != b for a, b in zip(w, x)) == 1]
        else:
            lim = 1 if len(w) < 8 else 2
            near = [x for n in range(len(w) - lim, len(w) + lim + 1) for x in self._by_len.get(n, [])]
            try:
                from rapidfuzz import process
                from rapidfuzz.distance import Levenshtein
                pool = [x for x, _, _ in process.extract(w, near, scorer=Levenshtein.distance, score_cutoff=lim,
                                                         limit=50)]
            except ImportError:
                pool = [x for x in near if _edits(w, x) <= lim]
        pool = [x for x in pool if x not in NOT_PRODUCT]   # never turn a product into 'فاکتور'
        if not pool:
            return None
        return max(pool, key=lambda x: (self.count[x], -abs(len(x) - len(w))))

    def split(self, w: str) -> list[str] | None:
        """'کمربندسایز' -> ['کمربند', 'سایز'] when both halves are known."""
        if self.known(w) or len(w) < 5:
            return None
        best = None
        for i in range(2, len(w) - 1):
            a, b = w[:i], w[i:]
            if a in self.words and b in self.words:
                score = min(self.count[a], self.count[b])
                if best is None or score > best[0]:
                    best = (score, [a, b])
        return best[1] if best else None


def _edits(a: str, b: str) -> int:
    """Levenshtein distance (short words only)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def basic(term: str) -> str:
    """Letters, digits and half-spaces unified; numbers apart from words ('90درجه', 'cpvc75')."""
    s = norm_text(str(term).replace("‌", " "))
    s = re.sub(r"(?<=\d)(?=[^\d\s.,/\-x×*])|(?<=[^\d\s.,/\-x×*])(?=\d)", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean(term: str, voc: Vocab) -> tuple[str, list[str]]:
    """A cleaned search term and what was changed (Persian notes for a person)."""
    memo = voc.__dict__.setdefault("_memo", {})
    if term not in memo:
        memo[term] = _clean(term, voc)
    c, n = memo[term]
    return c, list(n)


def _clean(term: str, voc: Vocab) -> tuple[str, list[str]]:
    notes: list[str] = []
    raw = norm_text(term)
    # the keys of ک گ و ج چ are ; ' , [ ] — punctuation norm_text drops, so the typed text is read as is
    typed = re.sub(r"\s+", " ", str(term).strip().lower())
    if re.fullmatch(r"[a-z;',\[\]]+", typed.replace(" ", "")) and not voc.known(raw) and raw not in voc.brands:
        fa = "".join(LAYOUT.get(c, c) for c in typed)
        if all(w in voc.words for w in fa.split()):
            notes.append(f"با صفحه‌کلید انگلیسی تایپ شده بود: «{fa}»")
            return fa, notes
    words = []
    for w in basic(term).split():
        if voc.known(w):
            words.append(w)
            continue
        parts = voc.split(w)
        if parts:
            notes.append(f"«{w}» جدا شد: «{' '.join(parts)}»")
            words += parts
            continue
        near = voc.closest(w)
        if near:
            notes.append(f"اصلاح املایی: «{w}» ← «{near}»")
            words.append(near)
            continue
        words.append(w)
    return " ".join(words), notes


# ------------------------------------------------------------------ clusters

def _flat(s: str) -> str:
    return re.sub(r"[\s\-/.]", "", basic(s))


def _code_core(s: str) -> str | None:
    """'Airtac4v210-08' and '4v210-060' share '4v210': the part of a code with a digit and a letter."""
    m = re.findall(r"\d*[a-z]+\d+|\d+[a-z]+\d*", _flat(s))
    m = [x for x in m if len(x) >= 4]
    return max(m, key=len) if m else None


def _when(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).strip()[:19])
    except ValueError:
        return None


def clusters(df: pd.DataFrame, minutes: int = 5) -> list[list[int]]:
    """Row groups that are one search (see the module notes)."""
    n = len(df)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i, j):
        parent[find(i)] = find(j)

    flat = [_flat(t) for t in df["term"]]
    core = [_code_core(t) for t in df["term"]]
    when = [_when(t) for t in df["last_search"]]
    for i in range(n):
        for j in range(i + 1, n):
            a, b = flat[i], flat[j]
            if not a or not b:
                continue
            same = a == b
            prefix = min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)) and abs(len(a) - len(b)) <= 4
            code = core[i] and core[i] == core[j]
            typing = False
            if when[i] and when[j] and abs(when[i] - when[j]) <= timedelta(minutes=minutes):
                typing = difflib.SequenceMatcher(None, a, b).ratio() >= 0.7
            if same or prefix or code or typing:
                join(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


# ------------------------------------------------------------------ categories

class TaxIndex:
    """Taxonomy leaves, found by any word of their name or synonyms."""

    def __init__(self, tax: pd.DataFrame | None, cfg: dict):
        from .rules import STOP
        self.stop = STOP
        self.nodes: list[dict] = []
        self.by_word: dict[str, list[int]] = {}
        if tax is None:
            return
        skip = {norm_text(x) for x in (cfg.get("taxonomy", {}).get("exclude_level1") or [])}
        t = tax[tax["is_leaf"] & ~tax["level1"].map(norm_text).isin(skip) & (tax["leaf"] != "")]
        for r in t.to_dict("records"):
            names = [r["leaf"]] + [x for x in str(r.get("synonyms") or "").split("|") if x.strip()]
            for nm in names:
                ws = [w for w in basic(nm).split() if w not in STOP and len(w) > 1]
                if not ws:
                    continue
                i = len(self.nodes)
                self.nodes.append({"taxonomy_id": r["taxonomy_id"], "breadcrumb": r["breadcrumb"], "leaf": r["leaf"],
                                   "words": ws, "count": int(r.get("site_product_count") or 0)})
                for w in set(ws):
                    self.by_word.setdefault(w, []).append(i)

    def match(self, text: str, n: int = 3) -> list[dict]:
        """Best leaves for a cleaned term: share of the term's words in the leaf (60%) and of the
        leaf's words in the term (40%); the thing itself (the first word) must be in both."""
        from .rules import UNITS
        ws = [w for w in basic(text).split() if w not in self.stop and len(w) > 1 and not re.fullmatch(r"[\d.,/×x*\-]+", w)
              and w not in UNITS and w not in COMMON - {"پمپ", "شیر", "لوله", "فیلتر", "موتور", "شیلنگ", "زانو"}]
        if not ws:
            return []
        flat = "".join(ws)
        cand = {i for w in ws for i in self.by_word.get(w, [])}
        best: dict[str, dict] = {}
        for i in cand:
            nd = self.nodes[i]
            lw = nd["words"]
            in_leaf = sum(1 for w in ws if w in lw or (len(w) > 2 and w in "".join(lw)))
            in_term = sum(1 for w in lw if w in ws or (len(w) > 2 and w in flat))
            score = 0.6 * in_leaf / len(ws) + 0.4 * in_term / len(lw)
            if ws[0] not in lw and lw[0] not in ws:          # 'فیوز مینیاتوری' is not a 'کلید مینیاتوری'
                score *= 0.6
            score = round(score, 3)
            k = nd["taxonomy_id"]
            if k not in best or score > best[k]["score"]:
                best[k] = {**{x: nd[x] for x in ("taxonomy_id", "breadcrumb", "leaf", "count")}, "score": score}
        # a tie goes to the shorter (more general) leaf: 'شیر برقی' before 'شیر برقی ۲۴ ولت'
        return sorted(best.values(), key=lambda x: (-x["score"], len(x["breadcrumb"])))[:n]

    def search(self, q: str, n: int = 20) -> list[dict]:
        """For the category picker: leaves whose breadcrumb has every word of q."""
        ws = basic(q).split()
        seen, out = set(), []
        for nd in self.nodes:
            bc = basic(nd["breadcrumb"])
            if nd["taxonomy_id"] not in seen and all(w in bc for w in ws):
                seen.add(nd["taxonomy_id"])
                out.append({x: nd[x] for x in ("taxonomy_id", "breadcrumb", "leaf", "count")})
                if len(out) >= n:
                    break
        return out


def load_tax(cfg: dict) -> pd.DataFrame | None:
    from .taxonomy import load_taxonomy
    p = Path(cfg.get("taxonomy", {}).get("file") or "")
    if not p.exists():
        return None
    t = load_taxonomy(p)
    raw = read_csv(p)
    syn = next((c for c in raw.columns if c.strip().lower().startswith("synonyms")), None)
    t["synonyms"] = raw[syn].values if syn else ""
    return t


# ------------------------------------------------------------------ the review list

def kind_of(cleaned: str, raw: str, voc: Vocab) -> tuple[str, str]:
    c = norm_text(cleaned)
    if any(re.search(rf"(^| ){re.escape(w)}($| )", x) for w in NOT_PRODUCT for x in (basic(raw),)):
        return "not_product", "به نظر کار با سفارش یا حساب است، نه محصول"
    flat = _flat(raw)
    if re.fullmatch(r"\d{4,}", flat):
        return "model", "فقط عدد است؛ شاید کد قطعه باشد"
    if CODE.match(flat) or (_code_core(raw) and len(c.split()) <= 3 and not any(w in voc.words for w in c.split()
                                                                              if not re.search(r"\d", w))):
        return "model", "کد مدل یا قطعه است؛ دسته از روی کد معلوم نیست"
    if c in voc.brands:
        return "brand", "فقط نام برند است؛ معلوم نیست کدام محصولش"
    words = [w for w in c.split() if not re.search(r"\d", w)]
    if not words or (len(words) == 1 and len(words[0]) <= 4 and not voc.known(words[0])):
        return "unclear", "نامفهوم است (شاید غلط تایپی)"
    unknown = [w for w in words if not voc.known(w)]
    if unknown and len(unknown) == len(words):
        return "product", "هیچ واژه‌ی آن را در دسته‌ها و محصولات نداریم"
    return "product", ""


def build(cfg: dict, export: pd.DataFrame, tax: pd.DataFrame | None = None) -> pd.DataFrame:
    """The review list (one row per cluster) for the uploaded terms; decisions already made are kept."""
    st = settings(cfg)
    tax = tax if tax is not None else load_tax(cfg)
    voc, idx = Vocab(cfg, tax), TaxIndex(tax, cfg)
    dec = decisions(cfg)
    rows = []
    for g in clusters(export, int(st["typing_minutes"])):
        part = export.iloc[g].sort_values(["searches", "last_search"], ascending=False)
        cl = [_flat(clean(t, voc)[0]) for t in part["term"]]
        wsets = [set(clean(t, voc)[0].split()) for t in part["term"]]

        def same(i, j):        # one search said twice: equal after cleaning, a start of the other, fewer words
            a, b = cl[i], cl[j]
            return a == b or a.startswith(b) or b.startswith(a) or wsets[i] <= wsets[j] or wsets[j] <= wsets[i]
        typing = any(not same(i, j) for i in range(len(part)) for j in range(i + 1, len(part)))
        # the speaker: most searched; on a tie the one with most known words, then the longest
        cand = part.assign(_known=[sum(w in voc.words for w in clean(t, voc)[0].split()) for t in part["term"]],
                           _len=part["term"].str.len())
        rep = cand.sort_values(["searches", "_known", "_len"], ascending=False).iloc[0]
        cleaned, notes = clean(rep["term"], voc)
        kind, why = kind_of(cleaned, rep["term"], voc)
        doubt = []
        if why:
            doubt.append(why)
        if notes:
            doubt += notes
        if typing:
            doubt.append(f"{len(part)} جستجوی پشت‌سرهم یا شبیه هم یکی شدند؛ منظور مشتری را بررسی کنید")
        m = idx.match(cleaned) if kind in ("product", "brand") else []
        top = m[0] if m and m[0]["score"] >= float(st["match_doubt"]) else None
        if kind == "product":
            if not top:
                doubt.append("دسته‌ای برایش پیدا نشد؛ شاید دسته‌ی تازه لازم است")
            elif top["score"] < float(st["match_min"]):
                doubt.append(f"تطبیق دسته ضعیف است ({top['score']:.2f})")
        proposal = ("reject" if kind in ("not_product", "unclear") and not typing
                    else "approve" if kind == "product" and top and top["score"] >= float(st["match_min"])
                    and not notes and not typing and not why else "check")
        key = _flat(rep["term"]) or norm_text(rep["term"])
        d = dec.get(key) or next((dec[_flat(t)] for t in part["term"] if _flat(t) in dec), None) or {}
        rows.append({"key": key, "term": rep["term"], "searches": int(part["searches"].sum()),
                     "last_search": max(part["last_search"]), "members": " | ".join(part["term"]),
                     "cleaned": cleaned, "kind": kind, "proposal": proposal, "doubt": "yes" if doubt else "",
                     "note": "؛ ".join(doubt), "taxonomy_id": top["taxonomy_id"] if top else "",
                     "breadcrumb": top["breadcrumb"] if top else "", "match_score": top["score"] if top else "",
                     "site_product_count": top["count"] if top else "",
                     "alternatives": json.dumps(m, ensure_ascii=False),
                     "decision": d.get("decision", ""), "query": d.get("query", ""),
                     "decided_taxonomy_id": d.get("taxonomy_id", ""), "decided_breadcrumb": d.get("breadcrumb", ""),
                     "decided_at": d.get("decided_at", ""), "queued_run": d.get("queued_run", "")})
    out = pd.DataFrame(rows, columns=REVIEW_COLS)
    order = {"approve": 0, "check": 1, "reject": 2}
    out = out.assign(_o=out["proposal"].map(order)).sort_values(["_o", "searches"], ascending=[True, False])
    return out.drop(columns="_o").reset_index(drop=True)


def upload(cfg: dict, data: bytes, filename: str = "", read_table=None) -> dict:
    """Store the plugin's file and rebuild the review list from it."""
    if read_table is None:
        df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    else:
        df, _ = read_table(data, filename)
    exp = read_export(df)
    if exp.empty:
        raise ValueError("هیچ عبارت جستجویی در فایل پیدا نشد؛ خروجی CSV صفحه‌ی «جستجوهای بی‌نتیجه» را بدهید")
    stamp = datetime.now(TZ).strftime("%Y-%m-%d_%H%M%S")
    write_csv(exp, sdir(cfg) / "uploads" / f"{stamp}.csv")
    rev = build(cfg, exp)
    write_csv(rev, sdir(cfg) / "review.csv", REVIEW_COLS)
    (sdir(cfg) / "meta.json").write_text(json.dumps({"uploaded": stamp, "file": filename, "terms": len(exp),
                                                    "rows": len(rev), "taxonomy": load_tax(cfg) is not None},
                                                   ensure_ascii=False), encoding="utf-8")
    log.info("SEARCHES_UPLOAD %d term(s) -> %d row(s)", len(exp), len(rev))
    return summary(cfg)


def rebuild(cfg: dict) -> dict:
    """Same latest upload, again (after the taxonomy or the vocabulary changed)."""
    ups = sorted((sdir(cfg) / "uploads").glob("*.csv"))
    if not ups:
        raise ValueError("هنوز فایلی بارگذاری نشده")
    write_csv(build(cfg, read_csv(ups[-1]).fillna("")), sdir(cfg) / "review.csv", REVIEW_COLS)
    return summary(cfg)


def _dec_key(row: dict, dec: dict) -> str:
    """The decision that belongs to a review row: its own key, else one made for another spelling
    of the same search (a member of its group) before the list was rebuilt; '' when none."""
    keys = _dec_keys(row, dec)
    return keys[0] if keys else ""


def _dec_keys(row: dict, dec: dict) -> list[str]:
    """Every decision belonging to the row (its own key first, then other spellings of its group)."""
    ks = [row["key"]] + [_flat(t) for t in str(row.get("members") or "").split(" | ")]
    return [k for k in dict.fromkeys(ks) if k and k in dec]


DEC_FIELDS = ["decision", "query", "decided_taxonomy_id", "decided_breadcrumb", "decided_at", "queued_run"]


def review(cfg: dict) -> pd.DataFrame:
    p = sdir(cfg) / "review.csv"
    if not p.exists():
        return pd.DataFrame(columns=REVIEW_COLS)
    r = read_csv(p).reindex(columns=REVIEW_COLS, fill_value="").fillna("")
    dec = decisions(cfg)                         # the decisions file is the truth: an undo clears the row
    for i, row in enumerate(r.to_dict("records")):
        dk = _dec_key(row, dec)
        d = dec.get(dk) if dk else None
        r.loc[i, DEC_FIELDS] = ([d["decision"], d["query"], d["taxonomy_id"], d["breadcrumb"], d["decided_at"],
                                 d["queued_run"]] if d else [""] * len(DEC_FIELDS))
    return r


def summary(cfg: dict) -> dict:
    r = review(cfg)
    try:
        meta = json.loads((sdir(cfg) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    appr = r[(r["decision"] == "approved")]
    return {"meta": meta, "rows": len(r), "searches": int(pd.to_numeric(r["searches"], errors="coerce").fillna(0).sum()),
            "proposal": r["proposal"].value_counts().to_dict(), "kind": r["kind"].value_counts().to_dict(),
            "doubt": int((r["doubt"] == "yes").sum()), "decided": int((r["decision"] != "").sum()),
            "approved": len(appr), "rejected": int((r["decision"] == "rejected").sum()),
            "ready": int(((appr["queued_run"] == "") & (appr["decided_taxonomy_id"] != "")).sum()),
            "approved_no_category": int((appr["decided_taxonomy_id"] == "").sum()),
            "queued": int((r["queued_run"] != "").sum()), "kind_fa": KIND_FA}


# ------------------------------------------------------------------ decisions

def decisions(cfg: dict) -> dict[str, dict]:
    p = sdir(cfg) / "decisions.csv"
    if not p.exists():
        return {}
    d = read_csv(p).reindex(columns=DECISION_COLS, fill_value="").fillna("")
    return {r["key"]: r for r in d.to_dict("records")}


def decide(cfg: dict, items: list[dict]) -> dict:
    """[{key, decision: approved|rejected|'' (undo), query?, taxonomy_id?}] from the review page.
    An approval keeps the person's query and category; without them the cleaned term and the
    proposed category are used. A row already sent to a crawl is not changed."""
    r = review(cfg).set_index("key", drop=False)
    tax = load_tax(cfg)
    crumbs = dict(zip(tax["taxonomy_id"], tax["breadcrumb"])) if tax is not None else {}
    dec = decisions(cfg)
    now = datetime.now(TZ).isoformat(timespec="seconds")
    changed, skipped = 0, []
    for it in items:
        k = str(it.get("key") or "")
        if k not in r.index:
            skipped.append(k)
            continue
        row = r.loc[k]
        dks = _dec_keys(row.to_dict(), dec)            # the decision may sit under other spellings
        if any(dec[x].get("queued_run") for x in dks):
            skipped.append(k)
            continue
        what = it.get("decision")
        if what not in ("approved", "rejected", ""):
            raise ValueError(f"decision نامعتبر: {what}")
        for x in dks:
            if x != k:
                dec.pop(x, None)                       # moved to this row's own key (or undone)
        if what == "":
            dec.pop(k, None)
            changed += 1
            continue
        tid = str(it.get("taxonomy_id") if it.get("taxonomy_id") is not None else row["taxonomy_id"]).strip()
        if tid and crumbs and tid not in crumbs:
            raise ValueError(f"شناسه‌ی دسته {tid} در تکسونومی نیست")
        q = str(it.get("query") or row["cleaned"] or row["term"]).strip()
        dec[k] = {"key": k, "decision": what, "query": q if what == "approved" else "",
                  "taxonomy_id": tid if what == "approved" else "",
                  "breadcrumb": crumbs.get(tid, row["breadcrumb"] if tid == row["taxonomy_id"] else "") if what == "approved" else "",
                  "decided_at": now, "queued_run": "", "term": row["term"]}
        changed += 1
    write_csv(pd.DataFrame(list(dec.values()), columns=DECISION_COLS), sdir(cfg) / "decisions.csv", DECISION_COLS)
    log.info("SEARCHES_DECIDE %d changed, %d skipped", changed, len(skipped))
    return {"changed": changed, "skipped": skipped, **summary(cfg)}


# ------------------------------------------------------------------ the crawl queue

def make_queue(cfg: dict, run: str | None = None) -> dict:
    """Approved rows with a category -> DATA_DIR/runs/<run>/01_queue.csv (one row per category).
    The crawl is not started here."""
    from .config import run_dir
    from .taxonomy import resolve_route
    st = settings(cfg)
    tax = load_tax(cfg)
    if tax is None:
        raise ValueError("اول فایل تاکسونومی را بارگذاری کنید")
    r = review(cfg)
    ready = r[(r["decision"] == "approved") & (r["queued_run"] == "") & (r["decided_taxonomy_id"] != "")].copy()
    if ready.empty:
        raise ValueError("ردیف تأییدشده‌ی دسته‌داری برای کراول نیست")
    if not run:                                  # two queues in one minute: -2, -3 … never overwrite
        base = "search-" + datetime.now(TZ).strftime("%Y%m%d-%H%M")
        run, n = base, 1
        while (run_dir(cfg, run) / "01_queue.csv").exists():
            n += 1
            run = f"{base}-{n}"
    by_id = tax.set_index("taxonomy_id")
    rules = cfg.get("routes") or {}
    ready["_n"] = pd.to_numeric(ready["searches"], errors="coerce").fillna(0)
    rows, queued = [], []
    for tid, g in ready.sort_values("_n", ascending=False).groupby("decided_taxonomy_id", sort=False):
        if tid not in by_id.index:             # the category left the taxonomy: stays approved, not queued
            continue
        queued += list(g["key"])
        t = by_id.loc[tid].copy()
        t["taxonomy_id"] = tid
        qs = list(dict.fromkeys(g["query"]))
        rows.append({"taxonomy_id": tid, "query": qs[0], "breadcrumb": t["breadcrumb"], "leaf": t["leaf"],
                     "level1": t["level1"], "route": resolve_route(t, rules),
                     "site_product_count": int(t["site_product_count"]), "priority": len(rows),
                     "extra_queries": " | ".join(qs[1:int(st["max_queries_per_category"])]),
                     "from_searches": " | ".join(g["term"]), "searches": int(g["_n"].sum())})
    if not rows:
        raise ValueError("دسته‌ی هیچ‌کدام از ردیف‌های تأییدشده در تکسونومی نیست")
    q = pd.DataFrame(rows)
    d = run_dir(cfg, run)
    write_csv(q, d / "01_queue.csv", QUEUE_COLS + ["extra_queries", "from_searches", "searches"])
    dec = decisions(cfg)
    for row in ready[ready["key"].isin(queued)].to_dict("records"):
        for dk in _dec_keys(row, dec):
            dec[dk]["queued_run"] = run
    write_csv(pd.DataFrame(list(dec.values()), columns=DECISION_COLS), sdir(cfg) / "decisions.csv", DECISION_COLS)
    log.info("SEARCHES_QUEUE %s: %d categories from %d approved searches", run, len(q), len(queued))
    return {"run": run, "categories": len(q), "searches": len(queued)}
