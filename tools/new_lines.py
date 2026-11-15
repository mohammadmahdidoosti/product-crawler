"""(15 Nov, owner) Which product LINES do batches 1-150 bring that the site had no example of before the crawler,
and which shops sell them. Read-only.

A product's line is judged by its category on the site, without the crawler's own products (author 208):
  new_line     its level-2 family (e.g. «رنگ و پوشش > رنگ») has no product in its whole subtree
  new_subtype  its own category subtree is empty, but the family has products
  existing     its category already had products (a new model of a kind we sell)
Input: /data/tmp/site_cat_counts2.tsv (term_id, parent, orig, crawler: products of any status, from the shop),
the taxonomy and its site map, and each batch's 04_filtered / 05_* files.
Out: /data/tmp/newlines_shops.csv, /data/tmp/newlines_families.csv
"""
import collections
import glob
import pandas as pd

CRAWLER_AUTHOR = 208


def rd(p, **kw):
    try:
        return pd.read_csv(p, dtype=str, keep_default_na=False, encoding="utf-8-sig", **kw)
    except Exception:
        return pd.DataFrame()


cnt = pd.read_csv("/data/tmp/site_cat_counts2.tsv", sep="\t", header=None, names=["wp", "parent", "orig", "crawler"])
kids = collections.defaultdict(list)
for w, p in zip(cnt.wp, cnt.parent):
    kids[p].append(w)
own = dict(zip(cnt.wp, cnt.orig))
own_all = dict(zip(cnt.wp, cnt.orig + cnt.crawler))
sub_o, sub_a = {}, {}


def subtree(w):
    if w in sub_o:
        return sub_o[w], sub_a[w]
    o, a = own.get(w, 0), own_all.get(w, 0)
    stack, seen = list(kids.get(w, [])), set()
    while stack:
        k = stack.pop()
        if k in seen:
            continue
        seen.add(k)
        o += own.get(k, 0)
        a += own_all.get(k, 0)
        stack += kids.get(k, [])
    sub_o[w], sub_a[w] = o, a
    return o, a


tax = rd("/data/taxonomy.csv")
path = dict(zip(tax["ID"], tax["Full Breadcrumb Path"]))
parent = dict(zip(tax["ID"], tax["Parent_ID"]))
wpmap = {k: int(v) for k, v in zip(*[rd("/data/wp_categories.csv")[c] for c in ("ID", "wp_id")]) if str(v).isdigit()}


def level2(t):
    """The taxonomy ID of the level-2 ancestor (or itself at level 1/2)."""
    seen = 0
    while t in parent and str(path.get(t, "")).count(" > ") > 1 and seen < 8:
        t = parent[t]
        seen += 1
    return t


def wp_of(t):
    seen = 0
    while t and t not in wpmap and seen < 8:
        t = parent.get(t, "")
        seen += 1
    return wpmap.get(t)


def line_of(t):
    w = wp_of(t)
    w2 = wp_of(level2(t))
    leaf_o = subtree(w)[0] if w else 0
    fam_o = subtree(w2)[0] if w2 else 0
    if fam_o == 0:
        return "new_line"
    if leaf_o == 0:
        return "new_subtype"
    return "existing"


pages = {}
imported, onsite = set(), set()
for d in glob.glob("/data/runs/batch-*"):
    try:
        n = int(d.rsplit("-", 1)[1])
    except ValueError:
        continue
    if not 1 <= n <= 150:
        continue
    f = rd(d + "/04_filtered.csv")
    if len(f) and "relevant" in f:
        f = f[f["relevant"].str.lower().isin(["true", "yes", "1"])]
    for r in f.to_dict("records"):
        u = r.get("url")
        if u and u not in pages:
            pages[u] = r
    imported |= set(rd(d + "/05_new_products.csv").get("url", []))
    for fn in ("05_same_model_on_site.csv", "05_updates_existing.csv", "05_on_site_already.csv"):
        onsite |= set(rd(d + "/" + fn).get("url", []))

rows = []
for u, r in pages.items():
    t = r.get("taxonomy_id", "")
    price = pd.to_numeric(r.get("price_toman"), errors="coerce")
    rows.append({"url": u, "domain": r.get("source_domain", ""), "seller": r.get("seller_name", ""),
                 "phones": r.get("seller_phones", ""), "name": r.get("name", ""), "tax": t,
                 "family": " > ".join(str(path.get(level2(t), r.get("breadcrumb", ""))).split(" > ")[:2]),
                 "line": line_of(t), "priced": bool(price and price > 0),
                 "photo": bool(str(r.get("image_urls", "")).strip()),
                 "status": "on_site" if u in onsite else "imported" if u in imported else "not_imported"})
df = pd.DataFrame(rows)
print("pages", len(df), df.line.value_counts().to_dict(), df.status.value_counts().to_dict())

sh = rd(sorted(glob.glob("/data/shops/*/shops.csv"))[-1])[["domain", "catalog_size", "crawlable", "store_api"]] \
    if glob.glob("/data/shops/*/shops.csv") else pd.DataFrame(columns=["domain"])
g = df.groupby("domain")
shops = pd.DataFrame({
    "pages": g.size(),
    "new_line": g.apply(lambda x: (x.line == "new_line").sum()),
    "new_subtype": g.apply(lambda x: (x.line == "new_subtype").sum()),
    "new_line_priced": g.apply(lambda x: ((x.line == "new_line") & x.priced).sum()),
    "new_line_not_imported_priced": g.apply(lambda x: ((x.line != "existing") & x.priced & (x.status == "not_imported")).sum()),
    "imported": g.apply(lambda x: (x.status == "imported").sum()),
    "families": g.apply(lambda x: " | ".join(f"{k} ({v})" for k, v in
                                           x[x.line != "existing"].family.value_counts().head(3).items())),
    "seller": g.seller.agg(lambda s: next((v for v in s if v), "")),
    "phones": g.phones.agg(lambda s: next((v for v in s if v), "")),
}).reset_index()
shops["new_share"] = ((shops.new_line + shops.new_subtype) / shops.pages).round(2)
shops = shops.merge(sh, on="domain", how="left").sort_values(["new_line_priced", "new_line"], ascending=False)
shops.to_csv("/data/tmp/newlines_shops.csv", index=False, encoding="utf-8-sig")

nl = df[df.line != "existing"]
fg = nl.groupby(["family", "line"])
fam = pd.DataFrame({"pages": fg.size(), "priced": fg.priced.sum(),
                    "imported": fg.apply(lambda x: (x.status == "imported").sum()),
                    "shops": fg.domain.nunique(),
                    "top_shops": fg.domain.agg(lambda s: " | ".join(f"{k} ({v})" for k, v in s.value_counts().head(4).items())),
                    }).reset_index().sort_values("pages", ascending=False)
fam.to_csv("/data/tmp/newlines_families.csv", index=False, encoding="utf-8-sig")
print("shops with new-line pages", (shops.new_line > 0).sum(), "with >=20 priced new-line", (shops.new_line_priced >= 20).sum())
print(shops.head(25)[["domain", "pages", "new_line", "new_line_priced", "new_subtype", "imported",
                      "new_line_not_imported_priced", "catalog_size", "crawlable"]].to_string(index=False))
print(fam[fam.line == "new_line"].head(25)[["family", "pages", "priced", "imported", "shops"]].to_string(index=False))
