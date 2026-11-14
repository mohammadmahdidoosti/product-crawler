"""(14 Nov, owner) Who sells what in the keyword batches 101-150: candidate suppliers per family (read-only).
Per shop: relevant products, families, how cheap vs the same category's median, 'representative' claims, seller, phones.
Out: /data/tmp/supply_shops.csv, /data/tmp/supply_by_family.csv"""
import glob, re, statistics, collections
import pandas as pd
rows = []
for d in sorted(glob.glob("/data/runs/batch-1[0-5][0-9]")):
    n = int(d.split("-")[-1])
    if not 101 <= n <= 150:
        continue
    p = d + "/04_filtered.csv"
    try:
        f = pd.read_csv(p, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except Exception:
        continue
    if "relevant" in f:
        f = f[f["relevant"].str.lower().isin(["true", "yes", "1"])]
    f["batch"] = n
    rows.append(f)
df = pd.concat(rows, ignore_index=True)
df["price"] = pd.to_numeric(df["price_toman"], errors="coerce")
med = df[df.price > 0].groupby("taxonomy_id")["price"].median()
df["ratio"] = df.apply(lambda r: r.price / med.get(r.taxonomy_id) if r.price > 0 and med.get(r.taxonomy_id) else None, axis=1)
parts = df["breadcrumb"].str.split(" > ")
df["family"] = parts.map(lambda p: " > ".join(p[:2]) if len(p) > 1 else p[0])
df["rep"] = df["claims_representative"].str.lower().isin(["true", "yes", "1"])
out = []
for dom, g in df.groupby("source_domain"):
    if not dom:
        continue
    phones = collections.Counter(x.strip() for v in g["seller_phones"] for x in str(v).split("|") if x.strip())
    names = collections.Counter(x for x in g["seller_name"] if x and x != dom)
    r = [x for x in g["ratio"] if x]
    fam = g["family"].value_counts()
    out.append({"domain": dom, "seller": names.most_common(1)[0][0] if names else "",
                "type": "foreign" if (g["source_type"] == "foreign").mean() > 0.5 else "iran",
                "products": len(g), "priced": int((g.price > 0).sum()), "families": fam.size,
                "top_families": " | ".join(f"{k} ({v})" for k, v in fam.head(3).items()),
                "price_vs_median": round(statistics.median(r), 2) if r else "",
                "cheaper_share": round(sum(1 for x in r if x < 0.95) / len(r), 2) if r else "",
                "claims_rep": int(g["rep"].sum()), "badges": " | ".join(sorted({b for v in g["supplier_badges"] for b in str(v).split("|") if b.strip()}))[:120],
                "phones": " | ".join(p for p, _ in phones.most_common(3)), "batches": g["batch"].nunique()})
s = pd.DataFrame(out).sort_values(["products"], ascending=False)
s.to_csv("/data/tmp/supply_shops.csv", index=False, encoding="utf-8-sig")
fam_rows = []
for fam, g in df.groupby("family"):
    for dom, h in g.groupby("source_domain"):
        r = [x for x in h["ratio"] if x]
        fam_rows.append({"family": fam, "domain": dom, "products": len(h), "priced": int((h.price > 0).sum()),
                         "price_vs_median": round(statistics.median(r), 2) if r else "", "claims_rep": int(h["rep"].sum())})
fb = pd.DataFrame(fam_rows).sort_values(["family", "products"], ascending=[True, False]).groupby("family").head(5)
fb.to_csv("/data/tmp/supply_by_family.csv", index=False, encoding="utf-8-sig")
print("SUPPLY products", len(df), "shops", len(s), "iran", int((s.type == "iran").sum()), "foreign", int((s.type == "foreign").sum()),
      "families", df["family"].nunique(), "batches", df["batch"].nunique(), flush=True)
