"""Catalog products -> the WooCommerce site, as drafts (variable products with one variation per size).

  * SKU: 'CAT-…' from supplier + brand + product name, so next month's price list updates the same
    product; each variation's SKU adds its values ('CAT-…-3F2A1'). A product found on the site is
    updated (its status is never changed); its variations are matched by SKU: prices updated, new
    sizes added, sizes no longer in the catalog left alone.
  * price: only the dollar price is ever written to regular_price (the site multiplies it by its own
    rate). usd_price() refuses anything else: a variation priced in rial/toman whose 'dollar' figure
    equals the printed number, a price above catalog.max_usd, or none at all. The toman price goes to
    the variation's meta (_caravan_price_toman) only.
  * one priced row -> a simple product; two or more -> variable, with the varying columns as variation
    attributes and the rest of the table in the description (sizes and specs, never prices).
  * a product whose prices were read from a page image (vision model) goes as 'pending' (در انتظار
    بررسی) so 'publish all' leaves it for a person.
Results: C6_published.csv (C6_publish_check.csv for a check without sending).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

import pandas as pd

from . import catalog as C
from .config import run_dir
from .io import read_csv, write_csv
from .normalize import norm_text
from .wc import WC, WCError, category_map

log = logging.getLogger(__name__)
RESULT = "C6_published.csv"
CHECK = "C6_publish_check.csv"
COLS = ["pkey", "sku", "action", "product_id", "status", "type", "name", "variations", "price_min_usd",
        "price_max_usd", "categories", "images", "brand", "edit_url", "error", "published_at"]
HOLD = ("duplicate", "too_high", "too_low", "unit_unknown", "unit_unconfirmed", "usd_doubt")


def parent_sku(p: dict, supplier: str = "") -> str:
    return "CAT-" + hashlib.sha1(f"{norm_text(supplier)}|{norm_text(p['brand'])}|{norm_text(p['family'])}".encode()
                                 ).hexdigest()[:10].upper()


def variation_sku(psku: str, attrs: dict) -> str:
    key = "|".join(f"{norm_text(k)}={norm_text(v)}" for k, v in sorted(attrs.items()))
    return f"{psku}-{hashlib.sha1(key.encode()).hexdigest()[:5].upper()}"


def usd_price(v: dict, max_usd: float = 100_000) -> str:
    """The variation's dollar price as the site's price field takes it, or '' when it is not safe."""
    from .catalog import parse_price
    try:
        usd = float(v.get("price_usd") or "")
    except ValueError:
        return ""
    if not 0 < usd <= max_usd:
        return ""
    raw = parse_price(v.get("price_raw"))
    if v.get("price_unit") in ("rial", "toman") and (not v.get("usd_rate_toman") or (raw and abs(raw - usd) < 0.005)):
        return ""                                        # never a toman / rial figure in the price field
    return f"{usd:.2f}".rstrip("0").rstrip(".")


def _brand(cfg: dict, raw: str) -> dict:
    from . import brands_fa as BF
    raw = str(raw or "").strip()
    if not raw:
        return {"status": "none", "display": ""}
    br = BF.resolve({"brand": raw}, BF.table(cfg))
    if br["status"] == "ok":
        return br
    # the maker named in its own catalog: used as written (a later brands-table entry takes over)
    fa, en = BF.split_display(raw)
    return {"status": "ok", "display": raw, "fa": fa, "en": en, "source": raw, "from_catalog": True}


def build(cfg: dict, run: str, p: dict, cats: dict[str, str]) -> tuple[dict, list[dict], list[str], dict]:
    """(parent payload, variation payloads, problems, info) for one catalog product."""
    from . import brands_fa as BF
    st = C.settings(cfg, run)
    max_usd = float(cfg.get("catalog", {}).get("max_usd", 100_000))
    psku = parent_sku(p, st.get("supplier", ""))
    br = _brand(cfg, p["brand"])
    name = BF.name_with_brand(p["family"], br)
    problems, held = [], []
    priced = []
    for v in p["variants"]:
        price = usd_price(v, max_usd)
        if not price or any(str(v.get("price_check") or "").startswith(h) or f"; {h}" in str(v.get("price_check") or "")
                            for h in HOLD):
            held.append(v)
            continue
        priced.append((v, price))
    if not priced:
        problems.append("هیچ ردیفی قیمت دلاری مطمئن ندارد" + (f" ({held[0].get('price_check')})" if held and held[0].get('price_check') else ""))
    tid = str(p.get("category", {}).get("taxonomy_id") or "")
    if not tid:
        problems.append("دسته ندارد")
    elif tid not in cats:
        problems.append(f"دسته‌ی وردپرس برای شناسه‌ی {tid} پیدا نشد")
    desc = p.get("description") or {}
    pv = {**p, "brand_display": br.get("display", ""), "variants": [v for v, _ in priced] or p["variants"]}
    attrs = [a for a in p["attributes"] if len({v["attributes"].get(a, "") for v, _ in priced}) > 1] if len(priced) > 1 else []
    if len(priced) == 1 and len(p["variants"]) > 1:
        # one row of a size table is left (the others held): a simple product named and described
        # by that row's own values, never passed off as the whole range
        v0 = priced[0][0]
        pv["common"] = {**p["common"], **v0["attributes"], **v0["specs"]}
        name = BF.name_with_brand(" ".join([p["family"]] + [f"{k} {x}" for k, x in v0["attributes"].items() if x]), br)
    meta = [{"key": "_caravan_source", "value": "catalog"},
            {"key": "_caravan_catalog", "value": str(st.get("source") or "")},
            {"key": "_caravan_catalog_run", "value": run},
            {"key": "_caravan_supplier", "value": str(st.get("supplier") or p["brand"] or "")},
            {"key": "_caravan_taxonomy_id", "value": tid},
            {"key": "_caravan_price_date", "value": str((priced[0][0] if priced else {}).get("usd_rate_date") or "")}]
    data = {"name": name, "type": "variable" if attrs else "simple", "status": "draft", "sku": psku,
            "description": (desc.get("html") or "") + C.spec_html(pv), "short_description": desc.get("short") or "",
            "catalog_visibility": "visible",
            "categories": [{"id": int(cats[tid])}] if tid in cats else [], "meta_data": meta}
    if any(str(v.get("price_check") or "").startswith("vision") for v, _ in priced):
        data["status"] = "pending"
    attributes = []
    for a in attrs:
        opts = list(dict.fromkeys(v["attributes"].get(a, "") for v, _ in priced if v["attributes"].get(a)))
        attributes.append({"name": a, "options": opts, "visible": True, "variation": True})
    if br.get("display"):
        attributes.append({"name": "برند", "options": [br["display"]], "visible": True, "variation": False})
    data["attributes"] = attributes
    variations = []
    if attrs:
        for v, price in priced:
            a = {k: v["attributes"].get(k, "") for k in attrs}
            spec = " · ".join(f"{k}: {x}" for k, x in {**{k: v["attributes"][k] for k in v["attributes"] if k not in attrs},
                                                         **v["specs"]}.items() if x)
            variations.append({"sku": variation_sku(psku, a), "regular_price": price,
                               "attributes": [{"name": k, "option": x} for k, x in a.items()],
                               "description": spec,
                               "meta_data": [{"key": "_caravan_price_toman", "value": str(v.get("price_toman") or "")},
                                             {"key": "_caravan_price_usd", "value": str(v.get("price_usd") or "")},
                                             {"key": "_caravan_usd_rate", "value": str(v.get("usd_rate_toman") or "")},
                                             {"key": "_caravan_price_date", "value": str(v.get("usd_rate_date") or "")},
                                             {"key": "_caravan_price_unit", "value": str(v.get("price_unit") or "")}]})
    elif priced:
        v, price = priced[0]
        data["regular_price"] = price
        data["meta_data"] += [{"key": "_caravan_price_toman", "value": str(v.get("price_toman") or "")},
                              {"key": "_caravan_price_usd", "value": str(v.get("price_usd") or "")},
                              {"key": "_caravan_usd_rate", "value": str(v.get("usd_rate_toman") or "")}]
    prices = [float(x) for _, x in priced]
    info = {"sku": psku, "brand": br.get("display", "") + (" (از کاتالوگ)" if br.get("from_catalog") else ""),
            "doubtful": data["status"] == "pending",
            "held": len(held), "price_min_usd": min(prices) if prices else "", "price_max_usd": max(prices) if prices else ""}
    return data, variations, problems, info


def _results(cfg: dict, run: str) -> pd.DataFrame:
    p = run_dir(cfg, run) / RESULT
    return read_csv(p) if p.exists() else pd.DataFrame(columns=COLS)


def _files(cfg: dict, p: dict) -> list:
    root = cfg["_data_dir"] / "images"
    return [root / f.strip() for f in str(p.get("images", {}).get("files") or "").split("|")
            if f.strip() and (root / f.strip()).exists()]


def _merge_options(cur: dict, data: dict) -> None:
    """Options of the site's variation attributes are kept, and so are the site's attributes this
    list no longer has, so older variations stay valid (an attribute dropped from a product leaves
    its variations without a value)."""
    have = {a.get("name"): a for a in cur.get("attributes") or []}
    mine = {a["name"] for a in data.get("attributes") or []}
    for a in data.get("attributes") or []:
        old = have.get(a["name"])
        if old and old.get("id"):
            a["id"] = old["id"]                     # a global (pa_) attribute stays the same attribute
        if old and a.get("variation"):
            a["options"] = list(dict.fromkeys(list(old.get("options") or []) + a["options"]))
    keep = [{k: a[k] for k in ("id", "name", "options", "visible", "variation") if k in a and (k != "id" or a[k])}
            for a in cur.get("attributes") or [] if a.get("name") not in mine]
    if keep:
        data["attributes"] = list(data.get("attributes") or []) + keep


def _auto_out(v: dict) -> bool:
    return any(m.get("key") == "_caravan_auto_out" and str(m.get("value")) == "1" for m in v.get("meta_data") or [])


def _opts(attrs) -> frozenset:
    """A variation's attribute values as a comparable set (site: [{name, option}], ours the same)."""
    return frozenset((norm_text(a.get("name")), norm_text(a.get("option"))) for a in attrs or [])


def _match_vars(site: list[dict], mine: list[dict]) -> dict[str, int]:
    """Our variation SKU -> the site variation it is: by SKU, else by the same attribute values (the
    SKU hashes the varying columns, which change when another column starts varying)."""
    by_sku = {v.get("sku"): v["id"] for v in site if v.get("sku")}
    by_opts: dict[frozenset, list[int]] = {}
    for v in site:
        by_opts.setdefault(_opts(v.get("attributes")), []).append(v["id"])
    out, used = {}, set()
    for v in mine:                                   # 1. SKU
        if v["sku"] in by_sku and by_sku[v["sku"]] not in used:
            out[v["sku"]] = by_sku[v["sku"]]
            used.add(out[v["sku"]])
    for v in mine:                                   # 2. the same values
        if v["sku"] in out:
            continue
        ids = [i for i in by_opts.get(_opts(v["attributes"]), []) if i not in used]
        if len(ids) == 1:
            out[v["sku"]] = ids[0]
            used.add(ids[0])
    for v in mine:                                   # 3. a column started or stopped varying: one holds the other
        if v["sku"] in out:
            continue
        o = _opts(v["attributes"])
        ids = [i for k, xs in by_opts.items() if o and k and (o <= k or k <= o) for i in xs if i not in used]
        if len(ids) == 1:
            out[v["sku"]] = ids[0]
            used.add(ids[0])
    return out


def _earlier_products(cfg: dict, run: str) -> dict[str, int]:
    """Products this supplier's earlier lists put on the site: {normalized name: product id}; a name
    two products share is left out (no guessing)."""
    sup = norm_text(C.settings(cfg, run).get("supplier") or "")
    if not sup:                                   # no supplier named: no earlier list to compare with
        return {}
    seen: dict[str, set[int]] = {}
    root = run_dir(cfg, run).parent
    for f in root.glob(f"*/{RESULT}"):
        other = f.parent.name
        if other == run or norm_text(C.settings(cfg, other).get("supplier") or "") != sup:
            continue
        for r in read_csv(f).to_dict("records"):
            if r.get("action") in ("created", "updated") and str(r.get("product_id") or "").strip():
                seen.setdefault(norm_text(r.get("name")), set()).add(int(float(r["product_id"])))
    return {k: next(iter(v)) for k, v in seen.items() if k and len(v) == 1}


def _twin(earlier: dict[str, int], name: str, claimed: set[int]) -> int | None:
    """The earlier product with exactly this name (normalized, spacing ignored) not already taken in
    this run. A near name is not enough: 'پارما 24' and 'پارما 28' are 0.97 alike and two products."""
    n = norm_text(name).replace(" ", "")
    hit = next((i for k, i in earlier.items() if k.replace(" ", "") == n), None)
    return hit if hit is not None and hit not in claimed else None


def publish(cfg: dict, run: str, limit: int = 0, pkeys: list[str] | None = None, dry_run: bool = False,
            republish: bool = False, wc: WC | None = None, progress=None, stop=None) -> dict:
    d = run_dir(cfg, run)
    prods = C.products(cfg, run)
    if not prods:
        raise ValueError("این کاتالوگ هنوز محصولی ندارد (مرحله‌ی «ساختار» را اجرا کنید)")
    wc = wc or WC()
    if not dry_run and not wc.ok():
        raise ValueError("WC_URL / WC_KEY / WC_SECRET روی سرور تنظیم نشده")
    cats = category_map()
    done = _results(cfg, run)
    sent = set(done.loc[done["action"].isin(["created", "updated"]), "pkey"]) if len(done) else set()
    known = {r["pkey"]: int(float(r["product_id"])) for r in done.to_dict("records")
             if str(r.get("product_id") or "").strip() and r.get("action") in ("created", "updated")}
    if pkeys:
        prods = [p for p in prods if p["pkey"] in set(pkeys)]
    elif not republish:
        prods = [p for p in prods if p["pkey"] not in sent]
    if limit:
        prods = prods[:int(limit)]
    from .publish import SiteBrands
    brands = SiteBrands(wc, dry_run) if wc.ok() else None
    earlier = _earlier_products(cfg, run)
    claimed: set[int] = set(known.values())
    out, counts = [], {"created": 0, "updated": 0, "skipped": 0, "error": 0, "checked": 0}
    for n, p in enumerate(prods, 1):
        if stop and stop():
            break
        data, variations, problems, info = build(cfg, run, p, cats)
        files = _files(cfg, p)
        res = {"pkey": p["pkey"], "sku": info["sku"], "name": data["name"], "type": data["type"],
               "variations": len(variations) or (1 if data.get("regular_price") else 0),
               "price_min_usd": info["price_min_usd"], "price_max_usd": info["price_max_usd"],
               "categories": ",".join(str(c["id"]) for c in data["categories"]), "images": len(files),
               "brand": info["brand"], "status": "", "product_id": "", "edit_url": "",
               "error": (f"{info['held']} ردیف کنار گذاشته شد (قیمت نامطمئن یا تکراری)" if info["held"] else ""),
               "published_at": time.strftime("%Y-%m-%d %H:%M")}
        if problems:
            res.update(action="skipped", error=" · ".join(problems + ([res["error"]] if res["error"] else [])))
            counts["skipped"] += 1
            out.append(res)
            continue
        if dry_run:
            res.update(action="checked", status=data["status"],
                       error=" · ".join(x for x in (res["error"], json.dumps(
                           {"price": data.get("regular_price"), "variations": [(v["attributes"], v["regular_price"]) for v in variations][:3]},
                           ensure_ascii=False)) if x))
            counts["checked"] += 1
            out.append(res)
            continue
        try:
            br = _brand(cfg, p["brand"])
            if brands is not None and brands.available and br.get("display"):
                bid, _ = brands.id_for(br)
                if bid:
                    data["brands"] = [{"id": int(bid)}]
            pid = known.get(p["pkey"])
            cur = None
            if pid is not None:
                try:
                    cur = wc.get_product(pid)
                except WCError as e:
                    if e.status != 404:
                        raise
                    cur, pid = None, None                     # deleted on the site: made again
            if cur is None:
                cur = wc.find_by_sku(info["sku"])
                pid = cur["id"] if cur else None
            if cur is None and (twin := _twin(earlier, data["name"], claimed)):
                # the same product sent from an earlier list under another SKU (a page read by the
                # vision model names it a little differently each time): updated, not made twice
                try:
                    cur, pid = wc.get_product(twin), twin
                except WCError as e:
                    if e.status != 404:
                        raise
            if cur is None:
                if files and wc.can_upload():
                    data["images"] = [{"id": wc.upload_media(f, f"{info['sku'].lower()}-{k}{f.suffix}", alt=data["name"])["id"]}
                                      for k, f in enumerate(files, 1)]
                prod = wc.create_product(data)
                pid, res["action"] = prod["id"], "created"
                have_vars = {}
            else:
                data.pop("status", None)                      # a product someone published stays published
                notes = []
                live = cur.get("status") == "publish"
                if live and info.get("doubtful"):
                    # prices read from a page image and not found in its text: a new product waits as
                    # 'pending', a live one keeps its prices until a person checks them
                    data.pop("regular_price", None)
                    variations = []
                    notes.append("قیمت‌ها از تصویر صفحه خوانده شده؛ روی محصول منتشرشده نوشته نشد")
                if cur.get("type") and cur.get("type") != data["type"]:
                    # the type of a product on the site is never changed: variable -> simple would drop
                    # its variations; its prices are left for a person
                    notes.append(f"نوع محصول روی سایت «{cur.get('type')}» است و این بار «{data['type']}» شد؛ "
                                 "نوع و قیمت‌ها دست نخورد")
                    data["type"] = cur["type"]
                    for k in ("regular_price", "attributes", "name", "description", "short_description"):
                        data.pop(k, None)          # built from the one row left: not the product on the site
                    variations = []
                if files and not cur.get("images") and wc.can_upload():
                    data["images"] = [{"id": wc.upload_media(f, f"{info['sku'].lower()}-{k}{f.suffix}", alt=data["name"])["id"]}
                                      for k, f in enumerate(files, 1)]
                data["categories"] = [{"id": i} for i in dict.fromkeys(
                    [c["id"] for c in cur.get("categories") or []] + [c["id"] for c in data["categories"]])]
                if "attributes" in data:
                    _merge_options(cur, data)
                prod = wc.update_product(pid, data)
                res["action"] = "updated"
                site_vars = wc.list_variations(pid) if cur.get("type") == "variable" else []
                have_vars = _match_vars(site_vars, variations)
                if notes:
                    res["error"] = " · ".join(x for x in [res["error"]] + notes if x)
            if variations:
                site_by_id = {v["id"]: v for v in (site_vars if res["action"] == "updated" else [])}
                create = [v for v in variations if v["sku"] not in have_vars]
                update = []
                for v in variations:
                    if v["sku"] not in have_vars:
                        continue
                    sv = site_by_id.get(have_vars[v["sku"]], {})
                    u = {k: x for k, x in v.items() if k != "sku" or not sv.get("sku")}   # a person's SKU stays
                    if _auto_out(sv):                    # we took it out of stock earlier: back in stock
                        u["stock_status"] = "instock"
                        u["meta_data"] = list(u.get("meta_data") or []) + [{"key": "_caravan_auto_out", "value": ""}]
                    update.append({**u, "id": have_vars[v["sku"]]})
                bad = []
                for k in range(0, max(len(create), len(update)), 50):
                    j = wc.batch_variations(pid, create[k:k + 50], update[k:k + 50])
                    bad += [x.get("error", {}).get("message", "?") for part in ("create", "update")
                            for x in (j.get(part) or []) if x.get("error")]
                if bad:
                    res["error"] = " · ".join(x for x in (res["error"], f"{len(bad)} تنوع ناموفق: {bad[0]}") if x)
                # sizes held this month or gone from the list: not left buyable at last month's price
                # (only after the new prices went in, and marked so a later list can bring them back)
                stale = [v for v in site_by_id.values() if v["id"] not in set(have_vars.values())
                         and v.get("stock_status") != "outofstock"] if not bad else []
                if stale:
                    wc.batch_variations(pid, None, [{"id": v["id"], "stock_status": "outofstock",
                                                     "meta_data": [{"key": "_caravan_auto_out", "value": "1"}]}
                                                    for v in stale])
                    res["error"] = " · ".join(x for x in (res["error"], f"{len(stale)} تنوعِ بدون قیمت مطمئن در این لیست «ناموجود» شد") if x)
            res.update(product_id=pid, status=prod.get("status", ""),
                       edit_url=f"{wc.base}/wp-admin/post.php?post={pid}&action=edit")
            counts[res["action"]] += 1
            log.info("catalog publish %s %s -> #%s (%d variation(s))", res["action"], info["sku"], pid, len(variations))
        except Exception as e:                        # noqa: BLE001 - one product never stops the rest
            res.update(action="error", error=str(e)[:400])
            counts["error"] += 1
            log.warning("catalog publish error %s: %s", info["sku"], e)
            if isinstance(e, WCError) and e.status in (401, 403):
                out.append(res)
                break
        out.append(res)
        if progress:
            progress(n, len(prods))
    if dry_run:
        write_csv(pd.DataFrame(out).reindex(columns=COLS).fillna(""), d / CHECK)
    elif out:
        # a product on the site that is skipped or fails this time keeps its record (ID, status); only
        # the reason is noted, so the next publish still finds and updates it
        on = {r["pkey"]: r for r in done.to_dict("records") if r.get("action") in ("created", "updated")
              and str(r.get("product_id") or "").strip()}
        rows = [{**on[r["pkey"]], "error": r.get("error", "")} if r["action"] in ("skipped", "error") and r["pkey"] in on
                else r for r in out]
        df = pd.concat([done, pd.DataFrame(rows)], ignore_index=True).drop_duplicates("pkey", keep="last")
        write_csv(df.reindex(columns=COLS).fillna(""), d / RESULT)
    log.info("catalog publish %s: %s", run, counts)
    return {"counts": counts, "rows": out}
