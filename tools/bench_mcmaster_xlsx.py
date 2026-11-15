import glob,re,openpyxl
from openpyxl.styles import Font,PatternFill,Alignment
B='/home/claude/product-crawler/assets/benchmarks/mcmaster/'
L1={'Fastening & Joining':'اتصال و بست','Pipe, Tubing, Hose & Fittings':'لوله، تیوب، شیلنگ و اتصالات','Power Transmission':'انتقال قدرت','Sealing':'آب‌بندی','Flow & Level Control':'کنترل جریان و سطح','Pressure & Temperature Control':'کنترل فشار و دما','Filtering':'فیلتراسیون','Electrical & Lighting':'برق و روشنایی','Raw Materials':'مواد اولیه','Fabricating':'ساخت و ماشینکاری','Abrading & Polishing':'سنباده‌زنی و پولیش','Sawing & Cutting':'اره‌کاری و برش','Safety Supplies':'تجهیزات ایمنی','Pulling & Lifting':'کشش و بالابری','Material Handling':'جابجایی مواد','Suspending':'آویز و مهار','Lubricating':'روانکاری','Hand Tools':'ابزار دستی','Measuring & Inspecting':'اندازه‌گیری و بازرسی','Hardware':'یراق‌آلات','Furniture & Storage':'مبلمان و انبارش','Plumbing & Janitorial':'تأسیسات بهداشتی و نظافت','Heating & Cooling':'گرمایش و سرمایش','Building & Grounds':'ساختمان و محوطه','Shipping':'بسته‌بندی و ارسال','Office Supplies & Signs':'لوازم اداری و تابلو'}
L2={'Fasteners':'پیچ و مهره و بست','Adhesives & Tape':'چسب و نوار','Welding, Brazing & Soldering':'جوشکاری، لحیم سخت و لحیم‌کاری','-':'-','Rotary Motion':'حرکت دورانی','Linear Motion':'حرکت خطی','Hydraulics':'هیدرولیک','Pneumatics':'پنوماتیک','Flow':'جریان','Liquid Level':'سطح مایع','Pressure':'فشار','Temperature':'دما','Wiring & Connecting':'سیم‌کشی و اتصال','Switching & Controlling':'کلید و کنترل','Motor Maintenance':'موتور و نگهداری','Power Generating':'تولید برق','Lighting':'روشنایی','Machining':'ماشینکاری','Forming':'شکل‌دهی','Clamping & Positioning':'گیره و موقعیت‌دهی','Parts Cleaning & Treating':'شستشو و عملیات قطعات','Sanding, Filing & Grinding':'سنباده، سوهان و سنگ‌زنی','Polishing':'پولیش','Sawing':'اره‌کاری','Cutting':'برش','Personal Protective Equipment':'تجهیزات حفاظت فردی','Protective Equipment':'تجهیزات حفاظتی','Signs':'تابلو','Dimensional Measuring':'اندازه‌گیری ابعادی','Calibrating':'کالیبراسیون','Inspecting':'بازرسی','Motion & Speed Measuring':'اندازه‌گیری حرکت و سرعت','Machinery Hardware':'یراق ماشین‌آلات','Door & Cabinet Hardware':'یراق در و کابینت','Locks':'قفل','Doors':'در','Furniture':'مبلمان','Storage':'انبارش','Containers':'ظروف و مخازن','Office Supplies':'لوازم اداری','Identification & Marking':'شناسایی و نشانه‌گذاری'}
DEF={('Office Supplies & Signs','Office Supplies'):'ترکیبی',('Furniture & Storage','Furniture'):'ترکیبی'}
# l3
l3=[];i=-1;j=-1;prev=None
for ln in open(B+'l3.txt',encoding='utf-8'):
    ln=ln.rstrip('\n')
    if not ln: continue
    if ln.startswith('# '):
        a,b=ln[2:].split(' > ',1)
        if a!=prev: i+=1;j=-1;prev=a
        j+=1;m=-1;cur=(a,b);continue
    m+=1;n,fa,c=ln.split('|')
    l3.append(dict(key=f'{i}.{j}.{m}',l1=cur[0],l2=cur[1],n=n,fa=fa,c=c or DEF.get(cur,'B2B')))
K={x['key']:x for x in l3}
assert len(l3)==608
l4=[];bad=[]
for f in sorted(glob.glob(B+'l4_*.txt')):
    for ln in open(f,encoding='utf-8'):
        ln=ln.rstrip('\n').replace(' ',' ').replace('‑','-')
        if not ln: continue
        if ln.startswith('@'):
            k,nm=ln[1:].split(' ',1);p=K[k]
            if nm.strip()!=p['n'].strip(): bad.append((k,nm,p['n']))
            continue
        n,fa,c=ln.split('|');mm=re.match(r'(.*)\((\d+)\)$',n)
        l4.append(dict(p=p,n=mm.group(1),cnt=int(mm.group(2)),fa=fa.strip(),c=c or p['c']))
print('l3',len(l3),'l4',len(l4),'namemismatch',len(bad),bad[:5])
wb=openpyxl.Workbook();hf=Font(bold=True,color='FFFFFF');fill=PatternFill('solid',fgColor='1F4E78')
def sheet(ws,hdr,rows,w):
    ws.sheet_view.rightToLeft=True;ws.append(hdr)
    for c in ws[1]: c.font=hf;c.fill=fill;c.alignment=Alignment(horizontal='center')
    for r in rows: ws.append(r)
    for k,v in enumerate(w): ws.column_dimensions[chr(65+k)].width=v
    ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
fa1=lambda x:L1[x['l1']];fa2=lambda x:L2[x['l2']]
from collections import defaultdict
S=defaultdict(lambda:[0,0,0,0,0,0])
for x in l3: S[x['l1']][{'B2B':0,'ترکیبی':1,'مصرفی':2}[x['c']]]+=1
for y in l4: S[y['p']['l1']][3]+=1;S[y['p']['l1']][4]+=y['cnt']
ws=wb.active;ws.title='خلاصه'
sheet(ws,['دسته اصلی','انگلیسی','دسته سطح ۳ B2B','دسته سطح ۳ ترکیبی','دسته سطح ۳ مصرفی','تعداد خانواده سطح ۴','تعداد کالا (جمع خانواده‌ها)'],[[L1[k],k]+v[:5] for k,v in S.items()],[30,30,14,14,14,16,20])
sheet(wb.create_sheet('دسته‌ها (سطح ۳)'),['دسته اصلی','زیردسته','دسته','نوع بازار','L1 (انگلیسی)','L2 (انگلیسی)','L3 (انگلیسی)','کلید'],[[fa1(x),fa2(x),x['fa'],x['c'],x['l1'],x['l2'],x['n'],x['key']] for x in l3],[26,24,30,10,26,24,30,9])
sheet(wb.create_sheet('خانواده‌ها (سطح ۴)'),['دسته اصلی','زیردسته','دسته','خانواده','نوع بازار','تعداد کالا در مک‌مستر','خانواده (انگلیسی)','کلید دسته'],[[fa1(y['p']),fa2(y['p']),y['p']['fa'],y['fa'],y['c'],y['cnt'],y['n'],y['p']['key']] for y in l4],[24,22,26,34,10,12,50,9])
ws=wb.create_sheet('توضیحات');ws.sheet_view.rightToLeft=True
for t in ['منبع: mcmaster.com — ۲۶ دسته اصلی، ۵۴ زیردسته، ۶۰۸ دسته سطح ۳، ۱۰٬۶۱۱ خانواده یکتای سطح ۴ (آبان ۱۴۰۵).','صفحه‌های مک‌مستر جست‌وجومحورند: هر خانواده ممکن است در چند دسته تکرار شود؛ هر خانواده فقط زیر اولین دسته‌ای که در آن دیده شد آمده است (جمع خام با تکرار: ۲۴٬۳۶۸).','۸۰ دسته سطح ۳ زیرخانواده نشان ندادند (مستقیم جدول محصول) یا همه خانواده‌هایشان قبلاً آمده بود؛ ۱۶۸ دسته در شیت خانواده‌ها ردیف ندارند.','تعداد کالا = عددی که مک‌مستر کنار هر خانواده نشان می‌دهد (تعداد محصول/مدل).','ترجمه‌ها دستی و بدون API انجام شده؛ نوع بازار: B2B / ترکیبی / مصرفی بر اساس منطق خرید.']: ws.append([t])
ws.column_dimensions['A'].width=140
out='/home/claude/product-crawler/assets/benchmarks/mcmaster-categories-fa.xlsx';wb.save(out);print(out)
print({k:sum(1 for y in l4 if y['c']==k) for k in ['B2B','ترکیبی','مصرفی']}, sum(y['cnt'] for y in l4))
