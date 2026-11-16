"""(16 Nov, owner) Suppliers of a PRODUCT (not a brand): each source its own section, then one merged contact list.

The brand finder (suppliers.py) is good for some brands and weak for others. The owner found suppliers by hand in:
  1. Google and Google Maps (as before, with product queries: تولیدکننده / پخش عمده / واردکننده ...)
  2. associations: «انجمن / سندیکا / اتحادیه + کالا», their members page lists companies with phones
  3. Telegram: public channels of shops, representatives and makers that post the product with a phone
     (found through Google; inside a public channel the keyword is searched without any login: t.me/s/<ch>?q=)
  4. Eitaa, Bale, Rubika: the channels Google knows here; their in-app global search, and the Tehran
     exhibition's company list (calendar.iranfair.com, which asks robots not to read it), are read in the person's
     own browser by a bookmarklet that copies the results; they are pasted into the tool (imports).

Every row: name, kind, phones, emails, site, channel, city, source, a line of evidence. The merge joins rows by phone,
then site, then channel; a supplier found in more sources goes up; the person's verdicts (suppliers.note, keyed by
the keyword) put the ones that answered first next time. Out: DATA_DIR/suppliers/kw_<keyword>.json.
Cost: about 12 Serper searches per keyword (under 2 cents) plus page reads.
"""
from __future__ import annotations

import html as _html
import json
import re
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from .normalize import norm_text
from .suppliers import OFFTOPIC, SKIP_HOSTS, _host, _phones, _serper, kind_of, notes_for, sdir

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
TG_ID = re.compile(r"(?<![\w@.])@([A-Za-z][A-Za-z0-9_]{4,31})\b|\b([A-Za-z][A-Za-z0-9_]{4,31})@(?![\w.])")


def _ids(t: str) -> list[str]:
    """@id, also written id@ where a right-to-left page shows it (Bale: «۱۱ عضو | morayco@»)."""
    return list(dict.fromkeys(a or b for a, b in TG_ID.findall(t or "")))
SELLER = re.compile(r"فروش|عمده|پخش|توزیع|تولید|سازنده|نمایندگی|نماینده|واردات|وارد\s*کننده|واردکننده|لیست\s*قیمت|قیمت|"
                    r"سفارش|تامین|تأمین|بازرگانی|فروشگاه|کارخانه|شرکت", re.I)
NEWS = re.compile(r"خبر|اخبار|نیوز|news|پایگاه\s*خبری|صدای|آوای|روزنامه|هفته\s*نامه|خبرگزاری|شبکه\s*اطلاع|"
                  r"منتظران|بیت\s*مایند|کریپتو|crypto", re.I)
ARTICLE = re.compile(r"بهترین|معرفی|چیست|چگونه|راهنمای\s*خرید|لیست\s*ده|مقایسه|آموزش|محققان|دانشمندان|"
                     r"یافت\s*نشد|نتایج:\s*0|0\s*آگهی", re.I)
ASSOC = re.compile(r"انجمن|سندیکا|اتحادیه|کانون|اتاق\s*بازرگانی|تشکل", re.I)
MEMBER_LINK = re.compile(r"اعضا|عضو|members?|فهرست\s*شرکت|لیست\s*شرکت|اعضای", re.I)
NOT_MEMBERS = re.compile(r"انواع\s*عضویت|فرم\s*عضویت|شرایط\s*عضویت|ثبت\s*نام|هیات\s*مدیره|هیئت\s*مدیره|"
                         r"membership|register|leaders?|board", re.I)
LABEL = re.compile(r"^(نام\s*شرکت|نام|شرکت|مدیر\s*عامل|تلفن|شماره\s*ثابت|شماره\s*تماس|شماره|فاکس|ایمیل|"
                   r"آدرس\s*پستی|آدرس|وب\s*سایت|سایت|موبایل|همراه|کد\s*پستی)\s*[:：]\s*", re.I)
CHANNEL_APPS = {"t.me": "تلگرام", "eitaa.com": "ایتا", "ble.ir": "بله", "rubika.ir": "روبیکا"}
SOURCE_FA = {"google": "گوگل", "maps": "نقشه گوگل", "assoc": "انجمن", "telegram": "تلگرام", "eitaa": "ایتا",
             "bale": "بله", "rubika": "روبیکا", "iranfair": "نمایشگاه بین‌المللی", "import": "از مرورگر"}


def out_path(cfg: dict, kw: str) -> Path:
    return sdir(cfg) / f"kw_{norm_text(kw).replace(' ', '_')}.json"


def _text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", h or "")
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", h)
    return _html.unescape(re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"<[^>]+>", " ", h)))


def _emails(t: str) -> list[str]:
    return list(dict.fromkeys(e.strip(".").lower() for e in EMAIL.findall(t or "")
                              if not e.lower().endswith((".png", ".jpg", ".webp", ".svg"))))


def _sites(t: str) -> list[str]:
    out = []
    for m in re.findall(r"(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9-]{1,62}\.(?:ir|com|co|net|org|info|biz|shop)(?:\.ir)?)\b",
                        (t or "").lower()):
        if "@" in m or m.split(".")[0] in ("gmail", "yahoo", "hotmail", "outlook", "chmail"):
            continue
        out.append(m)
    return list(dict.fromkeys(out))


GENERIC = {"دستگاه", "ماشین", "انواع", "لوازم", "تجهیزات", "قطعات", "ابزار", "خرید", "فروش", "قیمت", "عمده"}


def kw_words(kw: str) -> list[str]:
    return [w for w in norm_text(kw).split() if len(w) >= 2]


def main_word(kw: str) -> str:
    """The word that names the trade: «بذر» for «بذر هویج», «جوش» for «دستگاه جوش» (not the generic «دستگاه»)."""
    w = kw_words(kw)
    return next((x for x in w if x not in GENERIC), w[0] if w else "")


def mentions(text: str, kw: str) -> bool:
    m = main_word(kw)
    return bool(m) and m in norm_text(text)


def broader(kw: str) -> list[str]:
    """Wider words to try where a list (the exhibition, an association) knows only the trade:
    «بذر هویج» -> «بذر», «دستگاه جوش» -> «جوش»."""
    w = norm_text(kw).split()
    if len(w) > 1 and w[0] in GENERIC:
        return [" ".join(w[1:])] + broader(" ".join(w[1:]))
    out = [" ".join(w[:i]) for i in range(len(w) - 1, 0, -1)]
    return [x for x in out if len(x) >= 2 and x not in GENERIC]


def row(name: str, source: str, *, kind: str = "", phones=None, emails=None, site: str = "", channel: str = "",
        app: str = "", city: str = "", evidence: str = "", url: str = "", score: int = 0) -> dict:
    return {"name": re.sub(r"\s+", " ", str(name or "")).strip()[:90], "source": source, "kind": kind or "shop",
            "phones": list(dict.fromkeys(phones or []))[:6], "emails": list(dict.fromkeys(emails or []))[:4],
            "site": site, "channel": channel, "app": app, "city": city, "evidence": str(evidence or "")[:220],
            "url": url, "score": score}


# ---------------------------------------------------------------- contact blocks in a page (associations, imports)
NOT_NAME = re.compile(r"[:：]\s*$|^(اطلاعات\s*تماس|تماس\s*بگیرید|استان|شهر|خیابان|بلوار|میدان|کوچه|جاده|پلاک|"
                      r"نام\s*و\s*نام\s*خانوادگی|رئیس|رییس|دبیر|مدیر|هیئت|هیات|عضو|ارسال|دانلود|موضوع)", re.I)


def _name_of(lines: list[str]) -> str:
    for ln in lines:
        m = re.match(r"^(نام\s*شرکت|نام\s*واحد|نام\s*فروشگاه)\s*[:：]\s*(.+)$", ln)
        if m:
            return m.group(2).strip()
    for ln in lines:
        if LABEL.match(ln) or NOT_NAME.search(ln) or _phones(ln) or EMAIL.search(ln) or len(ln) < 3 or len(ln) > 70:
            continue
        return ln
    return ""


LANDLINE = re.compile(r"(?:شماره\s*ثابت|تلفن|تلفکس|شماره\s*تماس)\s*[:：]?\s*([\d۰-۹٠-٩][\d۰-۹٠-٩\-–\s]{6,14})")


def _block_phones(t: str) -> list[str]:
    """Phones of a members list, also the bare 8-digit Tehran numbers after «شماره ثابت/تلفن» (seedia: 88747645)."""
    out = _phones(t)
    for m in LANDLINE.findall(t):
        d = re.sub(r"\D", "", m.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")))
        if len(d) == 8 and d[0] != "0":
            d = "021" + d
        if len(d) == 11 and d.startswith("0") and not any(d[-8:] == p[-8:] for p in out):
            out.append(d)
    return out


def contact_blocks(page: str) -> list[dict]:
    """Companies with a phone or email in a page: the smallest block (list item, table row, card, accordion item)
    around each phone, its first line (or «نام شرکت: X») as the name. Hidden accordion text counts (seedia.ir)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page or "", "lxml")
    for t in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        t.decompose()
    blocks, seen = [], set()
    for node in soup.find_all(string=True):
        s = str(node)
        if not (_phones(s) or EMAIL.search(s)):
            continue
        el = node.parent
        best = el
        while el is not None and el.name not in ("body", "html", "[document]"):
            t = el.get_text("\n", strip=True)
            if len(t) > 700:
                break
            best = el
            if el.name in ("li", "tr", "article"):              # a list item is one member, named or not
                break
            if el.name in ("div", "section") and len(t) > 40 and _name_of([x.strip() for x in t.split("\n") if x.strip()]):
                break
            el = el.parent
        if id(best) in seen:
            continue
        seen.add(id(best))
        lines = [x.strip() for x in best.get_text("\n", strip=True).split("\n") if x.strip()]
        t = " ".join(lines)
        ph, em = _block_phones(t), _emails(t)
        if not (ph or em):
            continue
        blocks.append({"name": _name_of(lines), "phones": ph, "emails": em, "sites": _sites(t), "text": t[:300]})
    out, keys = [], set()                                   # one block per phone set (nested hits)
    for b in blocks:
        k = tuple(b["phones"] or b["emails"])
        if k in keys:
            continue
        keys.add(k)
        out.append(b)
    return out


# ---------------------------------------------------------------- 1. Google + Maps
def google_queries(kw: str) -> list[tuple[str, str]]:
    return [("search", f"تولید کننده {kw}"), ("search", f"پخش عمده {kw}"), ("search", f"واردکننده {kw}"),
            ("search", f"فروش عمده {kw} تهران"), ("search", f"لیست قیمت {kw}"), ("places", f"{kw} تهران"),
            ("places", f"فروش عمده {kw}")]


def google(cfg: dict, f, kw: str) -> dict:
    rows, n = [], 0
    for kind, q in google_queries(kw):
        j = _serper(f, kind, q)
        n += 1
        if kind == "places":
            for p in j.get("places") or []:
                t = f"{p.get('title', '')} {p.get('category', '')}"
                if OFFTOPIC.search(t):
                    continue
                rows.append(row(p.get("title", ""), "maps", kind=kind_of(t, p.get("website", "")),
                                phones=_phones(p.get("phoneNumber", "")), site=_host(p.get("website", "")),
                                city=p.get("address", ""), evidence=p.get("category", ""), url=p.get("website", ""),
                                score=2 if p.get("phoneNumber") else 0))
            continue
        for o in j.get("organic") or []:
            url, t = o.get("link", ""), f"{o.get('title', '')} {o.get('snippet', '')}"
            h = _host(url)
            if (any(s in h for s in SKIP_HOSTS) or OFFTOPIC.search(t) or not mentions(t, kw) or ARTICLE.search(t)
                    or NEWS.search(o.get("title", "")) or h.startswith("my.")):
                continue
            app = next((a for a in CHANNEL_APPS if h == a or h.endswith("." + a)), "")
            rows.append(row(o.get("title", ""), "google", kind=kind_of(t, url), phones=_phones(t), emails=_emails(t),
                            site="" if app else h, channel=url if app else "", app=CHANNEL_APPS.get(app, ""),
                            evidence=o.get("snippet", ""), url=url, score=1))
    return {"rows": rows, "searches": n}


# ---------------------------------------------------------------- 2. associations
def associations(cfg: dict, f, kw: str) -> dict:
    """«انجمن/سندیکا/اتحادیه + keyword» (and the broader word), then the association's members pages."""
    pages, n = [], 0
    for word in list(dict.fromkeys([kw] + broader(kw)[:1])):
        for q in (f"انجمن {word}", f"سندیکا {word}", f"اتحادیه {word} اعضا"):
            j = _serper(f, "search", q)
            n += 1
            for o in (j.get("organic") or [])[:8]:
                t = f"{o.get('title', '')} {o.get('snippet', '')}"
                h_ = _host(o.get("link", ""))
                if (ASSOC.search(o.get("title", "")) and mentions(o.get("title", "") + " " + o.get("snippet", ""), word)
                        and not any(h_ == a or h_.endswith("." + a) for a in CHANNEL_APPS) and not NEWS.search(o.get("title", ""))):
                    pages.append((o.get("link", ""), o.get("title", "")))
    rows, done_hosts, read = [], set(), 0
    for url, title in pages:
        h = _host(url)
        if not h or h in done_hosts or any(s in h for s in SKIP_HOSTS) or read >= 8:
            continue
        done_hosts.add(h)
        r = f.get(url)
        body = (r or {}).get("text") or ""
        read += 1
        targets = [url]
        for href, label in re.findall(r'href="([^"#]+)"[^>]*>(.{0,80}?)</a>', body, re.S):
            lab = re.sub(r"<[^>]+>", " ", label)
            if (MEMBER_LINK.search(lab) or MEMBER_LINK.search(href)) and not NOT_MEMBERS.search(lab + " " + href):
                u = urljoin(url, _html.unescape(href))
                if _host(u) == h and u not in targets:
                    targets.append(u)
        for t in targets[:4]:
            page = body if t == url else ((f.get(t) or {}).get("text") or "")
            if t != url:
                read += 1
            for b in contact_blocks(page):
                rows.append(row(b["name"] or title, "assoc", kind="wholesale", phones=b["phones"], emails=b["emails"],
                                site=(b["sites"] or [""])[0], evidence=f"عضو {title}: {b['text'][:150]}", url=t, score=2))
    return {"rows": rows, "searches": n, "associations": list(dict.fromkeys(t for _, t in pages))[:10]}


# ---------------------------------------------------------------- 3. Telegram, Eitaa, Bale, Rubika channels
def _channel_of(url: str) -> tuple[str, str]:
    p = urlparse(url)
    h = _host(url)
    app = next((a for a in CHANNEL_APPS if h == a or h.endswith("." + a)), "")
    parts = [x for x in p.path.split("/") if x]
    if not app or not parts:
        return "", ""
    ch = parts[1] if parts[0] == "s" and len(parts) > 1 else parts[0]
    if ch.lower() in ("joinchat", "addstickers", "share", "iv", "proxy", "c", "media", "post", "s") or ch.startswith("+"):
        return "", ""
    return app, ch


def channel_ok(title: str, text: str, kw: str = "") -> bool:
    """A seller's channel: names selling words (and the product), not a news, local-news or repair channel."""
    t = f"{title} {text}"
    return (bool(SELLER.search(t)) and not NEWS.search(title) and not OFFTOPIC.search(title)
            and (not kw or mentions(t, kw)))


def channels(cfg: dict, f, kw: str) -> dict:
    found, n = {}, 0
    for q in (f"site:t.me {kw}", f"site:t.me {kw} فروش", f"site:eitaa.com {kw}", f"site:ble.ir {kw}", f"site:rubika.ir {kw}"):
        j = _serper(f, "search", q)
        n += 1
        for o in (j.get("organic") or [])[:10]:
            app, ch = _channel_of(o.get("link", ""))
            if not ch:
                continue
            k = f"{app}/{ch.lower()}"
            t = f"{o.get('title', '')} {o.get('snippet', '')}"
            c = found.setdefault(k, {"app": app, "ch": ch, "title": o.get("title", ""), "text": "", "phones": []})
            c["text"] += " " + t
            c["phones"] = list(dict.fromkeys(c["phones"] + _phones(t)))
    rows, dropped = [], 0
    for c in found.values():
        posts, ids = 0, []
        if c["app"] == "t.me":                         # a public channel can be searched without login
            r = f.get(f"https://t.me/s/{c['ch']}?q={quote(kw)}")
            tx = _text((r or {}).get("text") or "")
            posts = norm_text(tx).count(main_word(kw)) if main_word(kw) else 0
            c["phones"] = list(dict.fromkeys(c["phones"] + _phones(tx)))
            ids = [i for i in _ids(tx) if i.lower() != c["ch"].lower()][:4]
            c["text"] += " " + tx[:4000]
        if not channel_ok(c["title"], c["text"], kw):
            dropped += 1
            continue
        src = {"t.me": "telegram", "eitaa.com": "eitaa", "ble.ir": "bale", "rubika.ir": "rubika"}[c["app"]]
        price = bool(re.search(r"لیست\s*قیمت|قیمت\s*روز|عمده", c["text"]))
        name = re.sub(r"^(ایتا|بله|روبیکا|Rubika|Telegram)\s*[-–:|]\s*", "", c["title"].split("–")[0].split("|")[0]).strip()
        if norm_text(name) in ("", "بله", "ایتا", "روبیکا", "rubika", "telegram", "تلگرام"):
            name = "@" + c["ch"]
        rows.append(row(name, src, kind="channel", phones=c["phones"],
                        channel=f"https://{c['app']}/{c['ch']}", app=CHANNEL_APPS[c["app"]],
                        evidence=(f"{posts} پست درباره‌ی کالا" if posts else "") + (" · لیست قیمت/عمده" if price else "")
                        + (f" · آیدی: {', '.join('@' + i for i in ids)}" if ids else ""),
                        url=f"https://{c['app']}/{c['ch']}", score=min(posts, 10) + (3 if price else 0) + (2 if c["phones"] else 0)))
    return {"rows": rows, "searches": n, "dropped": dropped}


# ---------------------------------------------------------------- 4. imports from the person's browser
BOOKMARKLET = ("javascript:(()=>{const L=[...document.querySelectorAll('a[href*=\"/companies/profile/\"]')];"
               "let rows=[];const S=new Set();for(const a of L){let c=a;for(let i=0;i<6&&c;i++){c=c.parentElement;"
               "if(c&&c.innerText&&c.innerText.includes('\\u0646\\u0645\\u0627\\u06cc\\u0634 \\u067e\\u0631\\u0648\\u0641\\u0627\\u06cc\\u0644')&&c.innerText.length>40)break;}"
               "if(c&&!S.has(a.href)){S.add(a.href);rows.push({url:a.href,text:c.innerText});}}"
               "const d={host:location.host,url:location.href,rows:rows,text:rows.length?'':document.body.innerText.slice(0,60000)};"
               "navigator.clipboard.writeText(JSON.stringify(d)).then(()=>alert('\\u06a9\\u067e\\u06cc \\u0634\\u062f: '+(rows.length||'\\u0645\\u062a\\u0646 \\u0635\\u0641\\u062d\\u0647')));})()")


def parse_import(data: dict) -> list[dict]:
    """What the bookmarklet copied: exhibition cards (name, activity, email, site, phone) or a messenger's search
    page as text (blocks around each @id / channel line)."""
    host = str(data.get("host") or "")
    src = ("iranfair" if "iranfair" in host else "bale" if "bale" in host else "eitaa" if "eitaa" in host
           else "rubika" if "rubika" in host else "telegram" if "telegram" in host or "t.me" in host else "import")
    out = []
    for r in data.get("rows") or []:
        lines = [x.strip() for x in str(r.get("text") or "").split("\n") if x.strip() and x.strip() not in ("-", "نمایش پروفایل")]
        t = " ".join(lines)
        if not lines:
            continue
        out.append(row(lines[0], src, kind=kind_of(t, ""), phones=_phones(t), emails=_emails(t),
                       site=(_sites(" ".join(l for l in lines if "@" not in l)) or [""])[0],
                       evidence=lines[1] if len(lines) > 1 else "", url=str(r.get("url") or ""), score=3))
    text = str(data.get("text") or "")
    if text and not out:                                 # Bale/Eitaa/Rubika global search, as page text
        lines = [x.strip() for x in text.split("\n") if x.strip()]
        for i, ln in enumerate(lines):
            ids = _ids(ln)
            if not ids:
                continue
            title = lines[i - 1] if i and not _ids(lines[i - 1]) else ln
            ctx = " ".join(lines[max(0, i - 1): i + 2])
            if not channel_ok(title, ctx):
                continue
            out.append(row(title, src, kind="channel", phones=_phones(ctx), channel="@" + ids[0],
                           app=SOURCE_FA.get(src, ""), evidence=ctx[:200], score=2))
    return out


def save_import(cfg: dict, kw: str, data: dict) -> dict:
    rows = parse_import(data)
    p = sdir(cfg) / f"kw_{norm_text(kw).replace(' ', '_')}.imports.json"
    old = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    old += rows
    p.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"added": len(rows), "total": len(old), "source": rows[0]["source"] if rows else ""}


def imports_of(cfg: dict, kw: str) -> list[dict]:
    p = sdir(cfg) / f"kw_{norm_text(kw).replace(' ', '_')}.imports.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


# ---------------------------------------------------------------- merge
RANK = {"official": 5, "wholesale": 4, "price_list": 3, "channel": 3, "shop": 1}


def merge(rows: list[dict], notes: dict | None = None) -> list[dict]:
    """One row per supplier: joined by any shared phone, else site, else channel; more sources = higher."""
    groups: list[dict] = []
    idx: dict[str, int] = {}
    for r in rows:
        keys = [f"p:{p[-8:]}" for p in r["phones"]] + ([f"s:{r['site']}"] if r["site"] else []) \
            + ([f"c:{r['channel'].lower().rstrip('/').split('/')[-1].lstrip('@')}"] if r["channel"] else [])
        if not keys:
            keys = [f"n:{norm_text(r['name'])}"]
        gi = next((idx[k] for k in keys if k in idx), None)
        if gi is None:
            groups.append({**r, "sources": [r["source"]], "evidence": [r["evidence"]] if r["evidence"] else []})
            gi = len(groups) - 1
        else:
            g = groups[gi]
            for fld in ("phones", "emails"):
                g[fld] = list(dict.fromkeys(g[fld] + r[fld]))[:6]
            for fld in ("site", "channel", "app", "city", "url"):
                g[fld] = g[fld] or r[fld]
            if len(r["name"]) > 2 and (not g["name"] or g["source"] in ("google", "maps") and r["source"] in ("iranfair", "assoc")):
                g["name"] = r["name"]
            if r["source"] not in g["sources"]:
                g["sources"].append(r["source"])
            if r["evidence"] and r["evidence"] not in g["evidence"]:
                g["evidence"].append(r["evidence"])
            if RANK.get(r["kind"], 1) > RANK.get(g["kind"], 1):
                g["kind"] = r["kind"]
            g["score"] = max(g["score"], r["score"])
        for k in keys:
            idx.setdefault(k, gi)
    notes = notes or {}
    for g in groups:
        g["key"] = (g["phones"] or [g["site"] or g["channel"] or norm_text(g["name"])])[0]
        v = (notes.get(g["key"]) or {}).get("verdict", "")
        g["verdict"] = v
        g["total"] = (g["score"] + RANK.get(g["kind"], 1) * 3 + (len(g["sources"]) - 1) * 8 + (4 if g["phones"] else 0)
                      + {"good": 100, "ok": 50, "no": -100}.get(v, 0))
        g["evidence"] = " · ".join(g["evidence"])[:300]
    return sorted(groups, key=lambda g: -g["total"])


SOURCES = {"google": google, "assoc": associations, "channels": channels}


def find(cfg: dict, kw: str, which: list[str] | None = None) -> dict:
    """Run the chosen sources for a keyword; keep earlier sections that were not run again, and the imports."""
    from .pipeline import _fetchers
    f = _fetchers(cfg)["iran"]
    p = out_path(cfg, kw)
    old = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    sections = old.get("sections", {})
    searches = 0
    for name in (list(SOURCES) if which is None else which):
        if name not in SOURCES:
            continue
        res = SOURCES[name](cfg, f, kw)
        searches += res.get("searches", 0)
        sections[name] = {**res, "at": time.strftime("%Y-%m-%d %H:%M")}
    imp = imports_of(cfg, kw)
    sections["imports"] = {"rows": imp}
    allrows = [r for s in sections.values() for r in s.get("rows", [])]
    out = {"kw": kw, "at": time.strftime("%Y-%m-%d %H:%M"), "searches": searches, "broader": broader(kw),
           "sections": sections, "merged": merge(allrows, notes_for(cfg, kw))}
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out
