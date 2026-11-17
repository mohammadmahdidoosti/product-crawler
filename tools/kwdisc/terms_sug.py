"""Second half of the keyword discovery: from the shop menus read by disc_full.py, take the candidate new keywords of
each "missing" keyword (same rules as the 100-keyword test: names sharing a word with the keyword = family, names
repeated in 2+ of its shops = adjacent), then ask Google Suggest about each unique term (free; 8 threads, backs off
when Google pushes back; resumable). Nothing is dropped; labelling is done afterwards from the CSVs.
In:  /data/runs/disc-full/disc.jsonl, hosts.jsonl, /data/tmp/nd.csv
Out: /data/runs/disc-full/kw_summary.csv, terms.csv, sug.csv (all CSV, so the run's zip download carries them)
Run in the app container: python tools/kwdisc/terms_sug.py [threads]"""
import sys, re, json, csv, time, threading, urllib.request, urllib.parse
from collections import defaultdict, Counter
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

D = Path("/data/runs/disc-full")
W = int(sys.argv[1]) if len(sys.argv) > 1 else 8
GEN = {"صنعتی", "تجهیزات", "لوازم", "انواع", "خرید", "فروش", "قیمت", "دستگاه", "ابزار", "قطعات", "محصولات", "سیستم",
       "عمده", "جانبی", "مخصوص", "برای", "کامل"}
BAD = re.compile(r"[0-9۰-۹×*/|+@#]|رمز|العربیة|فارسی|english|بلاگ|وبلاگ|صفحه|سفارش|لیست|عضویت|تسویه|ارسال|رضایت|رزومه|"
                 r"مشتریان|درباره|تماس|ورود|ثبت ?نام|سبد|حساب|پیگیری|سوالات|قوانین|حریم|شرایط|همکاری|نمایندگی ما|"
                 r"مقاله|اخبار|دانلود|گالری|کاتالوگ|استخدام|پشتیبانی|پرداخت|ضمانت|تخفیف|شگفت|جدیدترین|پرفروش|محبوب|"
                 r"مشاهده|بیشتر|ادامه|بازگشت|منو|دسته ?بندی|همه|تومان|ریال|^خانه$|^فروشگاه$|^محصولات$|^خدمات$|"
                 r"^برندها?$|^برند ها$|^[A-Za-z .\-&']+$|در (تهران|مشهد|یزد|شیراز|اصفهان|تبریز|کرج|قم|اهواز)|"
                 r"ایران خودرو|سایپا|پژو|پراید|سمند|دنا|زانتیا|مگان|کوییک|ساینا|پرشیا|تیبا|نقد و|سریال|فیلم و", re.I)


def norm(s):
    s = s.replace("ي", "ی").replace("ك", "ک").replace("‌", " ").replace("ـ", "")
    return re.sub(r"\s+", " ", s).strip()


def clean_name(x):
    x = norm(re.sub(r"^(قیمت|خرید|فروش|انواع|لیست قیمت|خرید و قیمت)\s+", "", norm(x)))
    if BAD.search(x) or len(x) < 3 or len(x) > 40 or len(x.split()) > 5:
        return None
    return x


hosts = {}
for l in open(D / "hosts.jsonl", encoding="utf-8"):
    try:
        d = json.loads(l)
        if not d.get("pending"):
            hosts[d["h"]] = d
    except Exception:
        pass
nd = {norm(l.strip()) for l in open("/data/tmp/nd.csv", encoding="utf-8") if l.strip()}

term_kws = defaultdict(set)      # term -> kw ids
term_type = {}                   # term -> 'هم‌خانواده' if family for any kw, else 'هم‌جوار'
term_hosts = Counter()
rows = []
for l in open(D / "disc.jsonl", encoding="utf-8"):
    r = json.loads(l)
    kw = norm(r["kw"])
    toks = [w for w in kw.split() if len(w) > 2 and w not in GEN] or kw.split()
    shop_list = list(dict.fromkeys(r.get("shops", []) + r.get("shops2", [])))
    per = Counter()
    for h in shop_list:
        d = hosts.get(h) or {}
        names = {n for n in (clean_name(x) for x in (d.get("cats") or []) + (d.get("menu") or [])) if n}
        for n in names:
            per[n] += 1
    fam = [n for n in per if n != kw and any(w in n for w in toks)]
    fam.sort(key=lambda n: -per[n])
    adj = [n for n, c in per.most_common() if c >= 2 and n not in fam and n != kw]
    for n in fam[:25]:
        term_kws[n].add(r["id"]); term_type[n] = "هم‌خانواده"; term_hosts[n] += per[n]
    for n in adj[:15]:
        term_kws[n].add(r["id"]); term_type.setdefault(n, "هم‌جوار"); term_hosts[n] += per[n]
    menus = sum(1 for h in shop_list if (hosts.get(h) or {}).get("cats") or (hosts.get(h) or {}).get("menu"))
    org = lambda o: " ¦ ".join(f"{x['k'][0]}:{x['h']}:{x['t'][:60]}" for x in (o or []))
    rows.append([r["id"], r["kw"], r["leaf"], r.get("match"), sum(1 for x in r["org"] if x["k"] == "market"),
                 len(r.get("shops", [])), r.get("q2k", ""), r.get("q2", ""), len(r.get("shops2", [])), menus,
                 len(fam), len(adj), org(r["org"]), org(r.get("org2"))])

with open(D / "kw_summary.csv", "w", encoding="utf-8", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["id", "kw", "leaf", "match", "markets", "shops", "q2_kind", "q2", "shops2", "menus_read",
                "family_terms", "adjacent_terms", "results", "results2"])
    w.writerows(sorted(rows))
terms = sorted(term_kws, key=lambda t: (-len(term_kws[t]), t))
with open(D / "terms.csv", "w", encoding="utf-8", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["term", "type", "in_missing_list", "n_keywords", "n_shop_mentions", "keyword_ids"])
    for t in terms:
        w.writerow([t, term_type[t], "نداریم" if t in nd else "", len(term_kws[t]), term_hosts[t],
                    " ".join(map(str, sorted(term_kws[t])[:30]))])
print("TERMS", len(terms), "kw", len(rows), flush=True)

# ---- Google Suggest on each new term (terms already in the missing list had theirs earlier).
# (17 Nov) runs only once the terms have been filtered and approved: the approved list is /data/tmp/sug_terms.txt
# (one term per line); without it the script stops here.
ap = Path("/data/tmp/sug_terms.txt")
if not ap.exists():
    print("EXTRACTED; Suggest waits for /data/tmp/sug_terms.txt", flush=True)
    sys.exit(0)
approved = {norm(l.strip()) for l in open(ap, encoding="utf-8") if l.strip()}
terms = [t for t in terms if t in approved] + sorted(approved - set(terms))
sp = D / "sug.jsonl"
done = set()
if sp.exists():
    for l in open(sp, encoding="utf-8"):
        try: done.add(json.loads(l)["t"])
        except Exception: pass
todo = [t for t in terms if t not in nd and t not in done]
LOCK = threading.Lock()
pause = [0.0]
bad = [0]


def sug(t):
    for k in range(6):
        while time.time() < pause[0]:
            time.sleep(1)
        try:
            u = ("https://suggestqueries.google.com/complete/search?client=firefox&hl=fa&gl=ir&q="
                 + urllib.parse.quote(t))
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore"))
            with LOCK:
                bad[0] = 0
                with open(sp, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"t": t, "s": d[1]}, ensure_ascii=False) + "\n")
            return
        except Exception as e:
            with LOCK:
                bad[0] += 1
                if bad[0] >= 5:                       # Google pushing back: everyone waits a while
                    pause[0] = time.time() + 120 * min(bad[0] // 5, 5)
                    print("PAUSE", bad[0], str(e)[:80], flush=True)
            time.sleep(3 + 5 * k)


print("SUGGEST todo", len(todo), "done", len(done), flush=True)
with ThreadPoolExecutor(W) as ex:
    for n, _ in enumerate(ex.map(sug, todo), 1):
        if n % 2000 == 0:
            print("SUG", n, len(todo), flush=True)
with open(D / "sug.csv", "w", encoding="utf-8", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["term", "n", "suggestions"])
    for l in open(sp, encoding="utf-8"):
        d = json.loads(l)
        w.writerow([d["t"], len(d["s"]), " | ".join(d["s"])])
print("DONE", flush=True)
