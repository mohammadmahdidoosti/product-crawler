"""Seed keywords (owner's sheet, 14 Nov) -> new taxonomy paths. Rules agreed with the owner:
sizes/brands stay separate; spelling-only variants merge; modifier-only pairs merge only when Google shows
>=6 of the same top-10 results; misplaced seeds go to their right family; families that already have a place
on the site go under it."""
import pandas as pd, re, json, gzip, base64, collections
s = pd.read_csv('seeds.csv')
s['seed'] = s.seed.astype(str).str.strip()
s['n'] = pd.to_numeric(s.n, errors='coerce').fillna(0)

M1 = 'ماشین‌آلات صنعتی و تولید'
FAM = {
 'رنگ ساختمانی و صنعتی': ('رنگ و پوشش',),
 'ورق MDF، نئوپان و تخته چندلایه': ('ورق ام دی اف، نئوپان و تخته',),
 'کارتن استاندارد و جعبه بسته‌بندی': ('ملزومات بسته‌بندی', 'کارتن و جعبه بسته‌بندی'),
 'لوازم بسته‌بندی صنعتی (استرچ پالت، تسمه بسته‌بندی، چسب نواری)': ('ملزومات بسته‌بندی', 'استرچ، تسمه و چسب بسته‌بندی'),
 'ظروف و بطری بسته‌بندی (PET، گالن، قوطی، شیشه)': ('ملزومات بسته‌بندی', 'ظروف و بطری بسته‌بندی'),
 'کیسه بافته، گونی و جامبوبگ': ('ملزومات بسته‌بندی', 'کیسه بافته، گونی و جامبوبگ'),
 'الکترود، سیم جوش و لوازم جوشکاری': ('جوشکاری و برش',),
 'تسمه، نوار نقاله و زنجیر صنعتی': ('قطعات مکانیکی و انتقال قدرت',),
 'روغن صنعتی، هیدرولیک و گریس': ('قطعات مکانیکی و انتقال قدرت', 'روغن و روانکار'),
 'گیربکس و موتورگیربکس صنعتی': ('قطعات مکانیکی و انتقال قدرت', 'گیربکس'),
 'دیگ بخار، بویلر و مشعل صنعتی': ('گرمایش، سرمایش و تهویه',),
 'چیلر و برج خنک‌کن صنعتی': ('گرمایش، سرمایش و تهویه', 'چیلر و برج خنک‌کن'),
 'المنت و هیتر صنعتی (نواری، کارتریج، فن‌دار)': ('گرمایش، سرمایش و تهویه', 'المنت و هیتر صنعتی'),
 'توری و مش فلزی': ('مصالح و شیمی ساختمان', 'توری، فنس و مش فلزی'),
 'عایق رطوبتی و حرارتی (ایزوگام، پشم شیشه، پشم سنگ)': ('مصالح و شیمی ساختمان',),
 'تجهیزات دامداری و مرغداری': ('ابزار و ماشین‌آلات کشاورزی و باغبانی', 'تجهیزات دامداری و مرغداری'),
 'خوراک دام و طیور (کیسه‌ای)': ('کود و نهاده‌های کشاورزی و دامی', 'خوراک دام و طیور'),
 'ابزار برش تراشکاری و CNC (اینسرت، فرز انگشتی، قلاویز)': (M1, 'ابزار برش تراشکاری و CNC'),
 'ماشین تراش و فرز CNC فلز': (M1, 'ماشین تراش و فرز'),
 'لیفتراک، جک پالت و تجهیزات انبار': (M1, 'لیفتراک، جک پالت و تجهیزات انبار'),
 'ماشین‌آلات صنایع غذایی (دیگ، میکسر، آسیاب، خشک‌کن)': (M1, 'ماشین‌آلات صنایع غذایی'),
 'کوره القایی و تجهیزات ریخته‌گری': (M1, 'کوره القایی و ریخته‌گری'),
 'میکسر بتن، بچینگ و بتونیر': (M1, 'میکسر بتن، پمپ بتن و بچینگ'),
 'دستگاه تزریق پلاستیک': (M1, 'دستگاه تزریق پلاستیک'),
 'دستگاه نایلون‌سازی و اکسترودر فیلم': (M1, 'دستگاه نایلون‌سازی و اکسترودر'),
 'آسیاب و گرانول‌ساز پلاستیک (بازیافت)': (M1, 'آسیاب و گرانول‌ساز پلاستیک'),
 'دستگاه کارتن‌سازی و چاپ فلکسو': (M1, 'دستگاه کارتن‌سازی و چاپ فلکسو'),
 'دستگاه خم و گیوتین ورق (پرس‌برک)': (M1, 'دستگاه خم و گیوتین ورق'),
 'دستگاه بلوک‌زنی و قطعات بتنی': (M1, 'دستگاه بلوک‌زنی و موزاییک'),
 'چرخ خیاطی و دوخت صنعتی': (M1, 'چرخ خیاطی و دوخت صنعتی'),
 'ماشین‌آلات ریسندگی و بافندگی': (M1, 'ماشین‌آلات بافندگی'),
 'دستگاه بادی و بطری‌سازی PET': (M1, 'دستگاه بادی و بطری‌سازی'),
 'دستگاه کیسه‌سازی و برش و دوخت نایلون': (M1, 'دستگاه کیسه‌سازی نایلون'),
 'تجهیزات رنگ الکترواستاتیک و کوره رنگ': (M1, 'تجهیزات رنگ الکترواستاتیک'),
 'دستگاه وکیوم‌فرمینگ و ظروف یکبارمصرف': (M1, 'دستگاه وکیوم‌فرمینگ'),
 'دستگاه‌های ساخت درب و پنجره UPVC': (M1, 'دستگاه ساخت درب و پنجره UPVC'),
 'ماشین‌آلات نجاری (پانل‌بر، لبه‌چسبان، CNC چوب)': (M1, 'ماشین‌آلات نجاری'),
 'دستگاه برش لیزر و پلاسما': ('جوشکاری و برش', 'دستگاه برش CNC و لیزر (چینی)'),
}
assert set(FAM) == set(s.fam), set(s.fam) - set(FAM)

def anchor(r):
    x, f = r.seed, r.fam
    # (owner OK 14 Nov) seeds in the wrong family
    if 'فرز انگشتی' in x and f.startswith(('ابزار برش', 'ماشین تراش')) and not re.search(r'کارباید|الماسه|کربن|تیز کن|چوب', x):
        return ('پنوماتیک و هوای فشرده',) if 'بادی' in x else ('ابزار برقی و شارژی',)
    if 'زنجیره' in x and 'دوز' in x:
        return FAM['چرخ خیاطی و دوخت صنعتی']
    if x.startswith('پیستوله') and 'پودر' not in x:
        return ('پنوماتیک و هوای فشرده', 'پیستوله رنگ')
    if 'سشوار صنعتی' in x:
        return ('ابزار برقی و شارژی', 'سشوار صنعتی')
    if 'نوار لبه' in x and re.search(r'دستگاه|خط تولید', x):
        return FAM['ماشین‌آلات نجاری (پانل‌بر، لبه‌چسبان، CNC چوب)']
    if 'پلاسما' in x and f.startswith('دستگاه برش لیزر'):
        return ('جوشکاری و برش',)
    # (owner OK 14 Nov) under the site's existing categories
    if f.startswith('تسمه، نوار نقاله'):
        return ('قطعات مکانیکی و انتقال قدرت', 'زنجیر و چرخ زنجیر صنعتی') if 'زنجیر' in x else ('قطعات مکانیکی و انتقال قدرت', 'تسمه و نوار نقاله')
    if f.startswith('دیگ بخار'):
        return ('گرمایش، سرمایش و تهویه', 'مشعل') if 'مشعل' in x else ('گرمایش، سرمایش و تهویه', 'دیگ بخار و بویلر صنعتی')
    if f.startswith('عایق') and 'عایق رطوبتی' in x:
        return ('مصالح و شیمی ساختمان', 'عایق رطوبتی')
    return FAM[f]
s['anchor'] = s.apply(anchor, axis=1)

def nk(x):  # spelling key
    x = str(x).replace('ي', 'ی').replace('ك', 'ک').replace('آ', 'ا').replace('أ', 'ا').lower()
    return re.sub(r'[‌\s]+', '', x)
def toks(x):
    return frozenset(re.sub(r'\s+', ' ', str(x).replace('ي', 'ی').replace('ك', 'ک').replace('آ', 'ا').replace('‌', ' ').lower()).split())
DATE = re.compile(r'\b1[34]\d\d\b')

# 1) spelling-only: keep the most searched spelling
s = s.sort_values('n', ascending=False)
syn = collections.defaultdict(list)
keep = {}
for r in s.itertuples():
    k = nk(r.seed)
    if k in keep:
        syn[keep[k]].append(r.seed)
    else:
        keep[k] = r.seed
u = s[~s.seed.map(nk).duplicated()].copy()
# 2) Google: modifier-only pairs with >=6 of 10 same results; each longer seed goes to ONE shorter one
d = json.loads(gzip.decompress(base64.b64decode(open('pairs.b64').read())))
Q, P = d['q'], d['p']
M = [int(x) for x in open('M6.txt').read().split(',')]
target = {}
for i in M:
    a, b = Q[P[i][0]], Q[P[i][1]]
    if b not in target or len(toks(a)) > len(toks(target[b])):
        target[b] = a
def root(x):
    seen = set()
    while x in target and x not in seen:
        seen.add(x); x = target[x]
    return x
# 3) a year in the name is not a product (catmake refuses dates): goes to the same name without it
present = set(u.seed)
for x in u.seed:
    if DATE.search(x):
        base = re.sub(r'\s+', ' ', DATE.sub('', x)).strip()
        target[x] = base if base in present else target.get(x, base)
import sys; sys.path.insert(0, '/home/claude/product-crawler')
from crawler.catmake import bad_name
for x in u.seed:                         # a name the site refuses (a date, a glued English word): kept as a search word of its parent
    if bad_name(x) and x not in target:
        tx = toks(x)
        cands = [y for y in u.seed if toks(y) < tx and not bad_name(y)]
        if cands:
            target[x] = max(cands, key=lambda y: len(toks(y)))
merged = {}
for x in u.seed:
    r = root(x)
    if r != x:
        merged[x] = r
for x, r in merged.items():
    syn[r] += [x] + syn.pop(x, [])
u = u[~u.seed.isin(merged)].copy()
missing_roots = {r for r in merged.values() if r not in set(u.seed)}
print('seeds', len(s), 'spelling merged', int(s.seed.map(nk).duplicated().sum()),
      'google/year merged', len(merged), 'left', len(u), 'roots not in list', missing_roots)

# 4) tree per anchor: parent = the most specific other seed whose words are all in this one
rows = []
for anc, g in u.groupby('anchor'):
    L = list(g.itertuples())
    T = {r.seed: toks(r.seed) for r in L}
    par = {}
    for x, tx in T.items():
        cands = [y for y, ty in T.items() if ty < tx]
        if cands:
            par[x] = max(cands, key=lambda y: (x.startswith(y + ' ') or x.endswith(' ' + y), len(T[y]), -len(y)))
    base = len(anc)
    def chain(x):
        c = [x]
        while c[-1] in par:
            c.append(par[c[-1]])
        return c[::-1]          # head ... x
    for r in L:
        c = chain(r.seed)
        if nk(c[0]) == nk(anc[-1]):
            c = c[1:]
        room = 4 - base          # levels left for seeds
        if len(c) > room:
            c = c[:room - 1] + [c[-1]]
        rows.append({'path': ' > '.join(list(anc) + c), 'seed': r.seed, 'family': r.fam, 'n': int(r.n),
                     'buy': r.buy, 'synonyms': ' | '.join(dict.fromkeys(syn.get(r.seed, [])))})
out = pd.DataFrame(rows).sort_values('path')
out.to_csv('new_tax.csv', index=False)
print('paths', len(out), 'depth', out.path.str.count('>').add(1).value_counts().to_dict())
print(out.path.str.split(' > ').str[0].value_counts().to_string())
