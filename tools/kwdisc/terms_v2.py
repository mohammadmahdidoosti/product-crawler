"""Second pass over the full discovery (no new searches, nothing paid): stricter rules after the owner's review.
* search-result hosts that are not shops (business directories, academic sites, app stores, translators, news) no
  longer count as specialised shops, and their menus give no keywords;
* each keyword gets business / consumer signals from its own results (titles, shop names), so the label no longer
  depends on the taxonomy group;
* the wholesale test counts real wholesalers only (not titles that merely repeat the word «عمده» of the query);
* a new term is "family" only when it contains the keyword's head word (its first meaningful word), not any shared
  word; menu junk rules fixed;
* every term is checked against the site (category names, and product names) -> "داریم".
In:  /data/runs/disc-full/disc.jsonl, hosts.jsonl; /data/tmp/nd.csv; the site export and taxonomy
Out: /data/runs/disc-full2/kw_v2.csv, terms_v2.csv
Run in the app container: python tools/kwdisc/terms_v2.py"""
import sys, re, json, csv
from collections import defaultdict, Counter
from pathlib import Path
sys.path.insert(0, "/app")

D, O = Path("/data/runs/disc-full"), Path("/data/runs/disc-full2")
O.mkdir(parents=True, exist_ok=True)


def norm(s):
    s = (s or "").replace("ي", "ی").replace("ك", "ک").replace("‌", " ").replace("ـ", "").replace("أ", "ا")
    return re.sub(r"\s+", " ", s).strip()


def tight(s):
    return re.sub(r"[\s\-_]", "", norm(s))


NONSHOP = re.compile(r"iran-tejarat|hamsanat|niazerooz|eforosh|iranmarkets|sanatbazar|factoryland|dmrco|iranyab|"
                     r"civilica|sid\.ir|\.ac\.ir|noormags|magiran|ensani|irandoc|elmnet|translate\.google|"
                     r"play\.google|apps\.apple|soft98|p30download|achareh|tabnak|zoomit|digiato|mobo\.news|"
                     r"cloud\.ir|my\.post|post\.ir|gov\.ir|\.org\.ir$|wikipedia|aparat|instagram|youtube|"
                     r"balad|neshan|telewebion|namnak|blog\.|mag\.|news|khabar|daneshjoo|faradars|maktabkhooneh|"
                     r"amazon|alibaba|aliexpress|made-in-china|stockcake|pinterest", re.I)
MARKETS = re.compile(r"digikala|torob|basalam|divar|emalls|sheypoor|technolife|snappshop|digistyle|khanoumi|"
                     r"banimode|modiseh|timcheh|esam|okala|cafebazaar|myket", re.I)
BIZ = re.compile(r"صنعت|صنعتی|تجهیزات|ماشین ?آلات|عمده|پخش|تولید|کارخانه|بازرگانی|شرکت|تامین|تأمین|مهندسی|فنی|"
                 r"آزمایشگاه|پزشکی|بیمارستان|اداری|رستوران|هتل|فروشگاهی|کارگاه|ساختمان|انبار|بسته ?بندی|یدک|"
                 r"قطعات|کشاورزی|دام|ایمنی|آتش ?نشانی|برق|الکتریک|هیدرولیک|پنوماتیک|ابزار|اتوماسیون|ترافیک|استیل|"
                 r"فولاد|آهن|پلیمر|شیمیایی|مواد اولیه|لیست قیمت|قیمت روز|کیلویی|تنی|رولی|کارتنی|نمایندگی|"
                 r"industrial|industry|tech|sanat|tajhiz|abzar|machine|eng", re.I)
CONS = re.compile(r"خانگی|کودک|بچه|نوزاد|زنانه|مردانه|دخترانه|پسرانه|هدیه|کادو|دکوری|شیک|فانتزی|لاکچری|آرایش|"
                  r"زیبایی|پوشاک|اکسسوری|سرگرمی|اسباب ?بازی|گیفت|موبایل|گوشی|ورزشی|سفری|کمپینگ|مد ", re.I)
WH = re.compile(r"پخش|تولید|کارخانه|بنکدار|عمده ?فروش|فروش عمده|مرکز عمده|omde|pakhsh|bonak|wholesale", re.I)
SVC_KW = re.compile(r"^(تعمیر|اجاره|خدمات|نرم ?افزار|طراحی|نصب|مشاوره|حمل|باربری|آموزش|سرویس|کالیبراسیون|"
                    r"بازرسی|شستشو|نظافت|ساخت|چاپ|برش|تراش)", re.I)
GEN = {"صنعتی", "تجهیزات", "لوازم", "انواع", "خرید", "فروش", "قیمت", "دستگاه", "ابزار", "قطعات", "محصولات", "سیستم",
       "عمده", "جانبی", "مخصوص", "برای", "کامل", "و", "با", "از", "به", "در"}
JUNK = re.compile(r"مقال|ارتباط|پروژه|^آموزش|آموزش |راهنما(ی)? (خرید|انتخاب)|مقایسه|پس از فروش|نقشه سایت|علاقه|"
                  r"نمونه ?کار|^دانش|^عمده$|^سایر|^شرکت|اخبار|خبر|رویداد|تخفیف|جشنواره|ویژه|درباره|افتخار|گواهی|مجوز|"
                  r"سوال|سؤال|تماس|پشتیبانی|شعب|نمایندگی|همکاری|شغل|استخدام|شکایت|ضمانت|گارانتی|بازگشت|حریم|"
                  r"قوانین|معرفی|^چرا |چگونه|وبینار|کاتالوگ|ویدیو|ویدئو|^فیلم$|تصاویر|^کالا$|^محصولات?$|^همه|"
                  r"^جدید|^پیشنهاد|^پرفروش|^برند|^فروشگاه|^خانه$|^صفحه|^مجله|بلاگ|نظرات|^مشاوره|^دانلود|^ورود|"
                  r"^خروج|حساب کاربری|سبد|^منو|^زیر ?دسته|^دسته ?بندی|^بیشتر|موجود|^لیست|^اطلاعات|^مشخصات|"
                  r"^توضیحات|^قیمت$|^سفارش|استعلام|^نقد|^خرید$|^فروش$|^سایت|^کشور|^ایران$|^استان|^شهر|بازار$|"
                  r"اپلیکیشن|اینستاگرام|تلگرام|واتساپ|^امکانات|^تیم|^سازمان|^مشتری|^رزرو| ما$|^تاریخ|^محاسبه|"
                  r"^اهداف|چشم انداز|نشریه|navigate|^باربری|^(سفید|مشکی|طوسی|قرمز|آبی|سبز|زرد|نارنجی|بنفش|"
                  r"صورتی|قهوه ای|کرم|طلایی|نقره ای)$|^خدمات|^تماس|^فهرست|^ابزارک|^پرسش|^فرم|^ثبت|^پیگیری|"
                  r"^رهگیری|^کد تخفیف|^شرایط|^قوانین|^درباره|^دسترسی|^نقشه|^آدرس|^شماره|^ساعت کاری|^تعرفه", re.I)
BAD = re.compile(r"[0-9۰-۹×*/|+@#❯›»«→←]|^[A-Za-z .\-&']+$|english|العربیة|فارسی|"
                 r"ایران خودرو|سایپا|پژو|پراید|سمند|دنا|زانتیا|مگان|کوییک|ساینا|پرشیا|تیبا", re.I)

# ---------- the site: category names and product names
have_exact, prod_by_tok = set(), defaultdict(list)
try:
    import app
    cfg = app.build_cfg()
    tax = app.paths()["taxonomy"]
    if tax.exists():
        for r in csv.DictReader(open(tax, encoding="utf-8-sig")):
            for k in ("Category_Name", "name", "Name"):
                if r.get(k):
                    have_exact.add(norm(r[k]))
    from crawler.dedupe import load_site_catalog
    site = app.paths()["site"]
    if site.exists():
        sc = load_site_catalog(site, cfg["site_catalog"]["columns"])
        for nm in sc["name"].astype(str):
            n = norm(nm)
            for w in set(n.split()):
                if len(w) > 2:
                    prod_by_tok[w].append(n)
except Exception as e:                                    # the site check is a bonus: the rest runs without it
    print("SITE CHECK OFF", type(e).__name__, str(e)[:120], flush=True)
print("SITE categories", len(have_exact), "product words", len(prod_by_tok), flush=True)


def on_site(t):
    if t in have_exact:
        return "دسته‌ی سایت"
    ws = [w for w in t.split() if len(w) > 2]
    if not ws:
        return ""
    cand = min((prod_by_tok.get(w, []) for w in ws), key=len)
    if len(cand) > 5000:
        cand = cand[:5000]
    n = sum(1 for p in cand if t in p)
    return "محصول سایت" if n >= 3 else ""


hosts = {}
for l in open(D / "hosts.jsonl", encoding="utf-8"):
    try:
        d = json.loads(l)
        if not d.get("pending"):
            hosts[d["h"]] = d
    except Exception:
        pass
nd = {norm(l.strip()) for l in open("/data/tmp/nd.csv", encoding="utf-8") if l.strip()}


def clean_name(x):
    x = norm(re.sub(r"^(قیمت|خرید|فروش|انواع|لیست قیمت|خرید و قیمت)\s+", "", norm(x)))
    if BAD.search(x) or len(x) < 3 or len(x) > 40 or len(x.split()) > 5:
        return None
    return x


def head_of(kw):
    for w in kw.split():
        if w not in GEN and len(w) > 1:
            return w
    return kw.split()[0]


def is_shop(o):
    return o["k"] == "site" and o["h"] and not NONSHOP.search(o["h"])


term_kws, term_type, term_mentions, term_junk = defaultdict(set), {}, Counter(), {}
kw_rows = []
for l in open(D / "disc.jsonl", encoding="utf-8"):
    r = json.loads(l)
    kw = norm(r["kw"])
    org, org2 = r.get("org") or [], r.get("org2") or []
    shops1 = list(dict.fromkeys(o["h"] for o in org if is_shop(o)))
    shops2 = list(dict.fromkeys(o["h"] for o in org2 if is_shop(o) and o["h"] not in shops1))
    tk = tight(kw)
    match = sum(1 for o in org if tk in tight(o["t"])) / max(1, len(org))
    mk1 = sum(1 for o in org if MARKETS.search(o["h"]))
    mk2 = sum(1 for o in org2 if MARKETS.search(o["h"]))
    shop_titles = [(hosts.get(h) or {}).get("title", "") for h in shops1 + shops2]
    texts = [o["t"] + " " + o["h"] for o in org + org2 if not MARKETS.search(o["h"]) and not NONSHOP.search(o["h"])]
    biz = sum(1 for t in texts + shop_titles if BIZ.search(t))
    cons = sum(1 for t in texts + shop_titles if CONS.search(t)) + mk1
    whole = sum(1 for o in org2 if r.get("q2k") == "omde" and is_shop(o) and WH.search(o["t"] + " " + o["h"]))
    svc = bool(SVC_KW.search(kw))
    # ---- terms from the menus of real shops only
    head = head_of(kw)
    per = Counter()
    for h in shops1 + shops2:
        d = hosts.get(h) or {}
        for n in {n for n in (clean_name(x) for x in (d.get("cats") or []) + (d.get("menu") or [])) if n}:
            per[n] += 1
    fam = [n for n in per if n != kw and head in n.split()]
    fam.sort(key=lambda n: -per[n])
    adj = [n for n, c in per.most_common() if c >= 2 and n not in fam and n != kw]
    for n in fam[:30]:
        term_kws[n].add(r["id"]); term_type[n] = "هم‌خانواده"; term_mentions[n] += per[n]
    for n in adj[:15]:
        term_kws[n].add(r["id"]); term_type.setdefault(n, "هم‌جوار"); term_mentions[n] += per[n]
    kw_rows.append([r["id"], r["kw"], r.get("leaf", ""), round(match, 2), mk1, len(shops1), r.get("q2k", ""),
                    r.get("q2", ""), mk2, len(shops2), whole, biz, cons, int(svc), len(fam), len(adj),
                    " ¦ ".join(shops1[:5]), " ¦ ".join(shops2[:4]), " ¦ ".join(t[:50] for t in shop_titles[:4]),
                    " ¦ ".join(o["t"][:60] for o in org[:6]), " ¦ ".join(o["t"][:60] for o in org2[:5])])

with open(O / "kw_v2.csv", "w", encoding="utf-8", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["id", "kw", "leaf", "match", "markets1", "shops1", "q2_kind", "q2", "markets2", "shops2",
                "wholesalers", "biz", "cons", "svc_word", "family", "adjacent", "shop_hosts1", "shop_hosts2",
                "shop_titles", "titles1", "titles2"])
    w.writerows(sorted(kw_rows))
with open(O / "terms_v2.csv", "w", encoding="utf-8", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["term", "type", "junk", "in_missing_list", "on_site", "n_keywords", "mentions", "keyword_ids"])
    for t in sorted(term_kws, key=lambda t: (-len(term_kws[t]), t)):
        w.writerow([t, term_type[t], int(bool(JUNK.search(t))), int(t in nd), on_site(t), len(term_kws[t]),
                    term_mentions[t], " ".join(map(str, sorted(term_kws[t])[:30]))])
print("DONE kw", len(kw_rows), "terms", len(term_kws), flush=True)
