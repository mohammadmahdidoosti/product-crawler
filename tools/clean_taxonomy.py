"""Clean the taxonomy sheet before import (issues found in the review of taxonomy-merged).

  1. a category named exactly like its parent ('هیدروموتور > هیدروموتور') is folded into
     the parent; its children move up one level
  2. rows with the same path are merged into one (lowest ID kept): their original
     keywords become synonyms, old site categories are joined with ' | '
  3. new columns for the WordPress import plugin: Level, Category_Name, Parent_Name,
     Category_Slug (the current site slug when the category exists, else built from the
     name, unique), Old_Category_Name, Old_Category_Slug, Old_Category_URL, Synonyms,
     Merged_IDs
The original columns stay, so the crawler reads the file as before.

    python tools/clean_taxonomy.py data/taxonomy.csv data/taxonomy-clean.csv data/taxonomy-changes.csv
"""
from __future__ import annotations

import re
import sys
from urllib.parse import unquote

import pandas as pd

P = "Full Breadcrumb Path"
LV = ["Level 1 (دسته اصلی)", "Level 2 (زیردسته سطح ۱)", "Level 3 (زیردسته سطح ۲)", "Level 4 (زیردسته سطح ۳)"]
KW, SRC, SNAME, SURL = "Original Keyword", "Source (منبع)", "Site_Category_Name (نام دسته در سایت)", "Site_Category_URL"
CNT, PRI, FLAG, NOTE = "Site_Product_Count (تعداد محصول)", "Review_Priority (اولویت بررسی)", "Review_Flag", "Review_Note (توضیح بررسی)"
PRI_RANK = {"بالا": 3, "متوسط": 2, "پایین": 1}


def parts(path: str) -> list[str]:
    return [x.strip() for x in str(path).split(">") if x.strip()]


def join(vals, sep=" | ") -> str:
    out = []
    for v in vals:
        for x in str(v).split(sep):
            x = x.strip()
            if x and x not in out:
                out.append(x)
    return sep.join(out)


def slugify(name: str) -> str:
    s = name.strip().lower().replace("‌", "-")
    s = re.sub(r"[\s_/]+", "-", s)
    s = re.sub(r"[^\w\-]", "", s)                     # \w keeps Persian letters and digits
    return re.sub(r"-{2,}", "-", s).strip("-")


def old_slug(url: str) -> str:
    segs = [x for x in unquote(url).split("/") if x]
    return segs[-1] if segs else ""


def clean(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.fillna("").copy()
    df[P] = df[P].map(lambda p: " > ".join(parts(p)))
    log = []

    # 1. child named like its parent -> folded into the parent
    by_path = dict(zip(df[P], df["ID"]))
    for _, r in df[df[P].map(lambda p: len(parts(p)) > 1 and parts(p)[-1] == parts(p)[-2])].iterrows():
        old, new = r[P], " > ".join(parts(r[P])[:-1])
        parent_id = by_path[new]
        for i in df.index:
            p = df.at[i, P]
            if p == old:
                df.at[i, P], df.at[i, "Parent_ID"] = new, df.loc[df["ID"] == parent_id, "Parent_ID"].iloc[0]
            elif p.startswith(old + " > "):
                df.at[i, P] = new + p[len(old):]
                if df.at[i, "Parent_ID"] == r["ID"]:
                    df.at[i, "Parent_ID"] = parent_id
        log.append({"action": "folded into parent", "id": r["ID"], "detail": f"{old} -> {new}"})

    # 2. merge rows with the same path
    keep_of: dict[str, str] = {}
    rows = []
    df["_id"] = df["ID"].astype(int)
    for path, g in df.sort_values("_id").groupby(P, sort=False):
        k = g.iloc[0].to_dict()
        ids = list(g["ID"])
        for i in ids:
            keep_of[i] = ids[0]
        if len(g) > 1:
            sites = g[g[SURL] != ""].drop_duplicates(SURL)
            k[KW] = join(g[KW])
            k[SRC] = join(g[SRC])
            k[SNAME] = join(sites[SNAME])
            k[SURL] = join(sites[SURL])
            k[CNT] = str(int(pd.to_numeric(sites[CNT], errors="coerce").fillna(0).sum())) if len(sites) else k[CNT]
            k[PRI] = max(g[PRI], key=lambda x: PRI_RANK.get(x, 0))
            k[FLAG] = join(g[FLAG], "; ")
            k[NOTE] = join(g[NOTE], "؛ ")
            k["Is_Inferred_Parent"] = "FALSE" if (g["Is_Inferred_Parent"].str.upper() == "FALSE").any() else k["Is_Inferred_Parent"]
            log.append({"action": "merged duplicates", "id": ids[0], "detail": f"{path} <- IDs {', '.join(ids[1:])}"})
        k["Merged_IDs"] = ", ".join(ids[1:])
        rows.append(k)
    out = pd.DataFrame(rows).drop(columns=["_id"])
    out["Parent_ID"] = out["Parent_ID"].map(lambda x: keep_of.get(x, x))

    # 3. levels and import columns
    pp = out[P].map(parts)
    for n, col in enumerate(LV):
        out[col] = pp.map(lambda x, n=n: x[n] if len(x) > n else "")
    out["Level"] = pp.map(len).astype(str)
    out["Category_Name"] = pp.map(lambda x: x[-1])
    out["Parent_Name"] = pp.map(lambda x: x[-2] if len(x) > 1 else "")
    out["Old_Category_Name"] = out[SNAME]
    out["Old_Category_URL"] = out[SURL]
    out["Old_Category_Slug"] = out[SURL].map(lambda u: join(old_slug(x) for x in u.split(" | ")) if u else "")
    out["Synonyms"] = [join(k for k in kw.split(" | ") if k.strip() and k.strip() != name
                            and not re.search(r"synthetic|suggested", k, re.I))
                       for kw, name in zip(out[KW], out["Category_Name"])]
    # slug: keep the site's current slug (URLs and Google index stay); else from the name, made unique
    used: set[str] = set()
    slugs = [""] * len(out)
    order = sorted(range(len(out)), key=lambda i: (out.at[i, "Old_Category_Slug"] == "", int(out.at[i, "Level"])))
    id_slug: dict[str, str] = {}
    for i in order:
        s = out.at[i, "Old_Category_Slug"].split(" | ")[0] if out.at[i, "Old_Category_Slug"] else ""
        if not s or s in used:
            base = slugify(out.at[i, "Category_Name"])
            s = base
            if s in used:                           # same name elsewhere: add the parent's name
                s = f"{base}-{slugify(out.at[i, 'Parent_Name'])}" if out.at[i, "Parent_Name"] else base
            if s in used:
                s = f"{base}-{out.at[i, 'ID']}"
        used.add(s)
        slugs[i] = s
    out["Category_Slug"] = slugs
    out = out.sort_values(["Level", P], key=lambda c: c.astype(int) if c.name == "Level" else c, kind="stable")
    return out.reset_index(drop=True), pd.DataFrame(log)


if __name__ == "__main__":
    src, dst, changes = sys.argv[1:4]
    df = pd.read_csv(src, dtype=str, encoding="utf-8-sig")
    out, log = clean(df)
    out.to_csv(dst, index=False, encoding="utf-8-sig")
    log.to_csv(changes, index=False, encoding="utf-8-sig")
    print(f"{len(df)} rows -> {len(out)} categories; {len(log)} changes")
