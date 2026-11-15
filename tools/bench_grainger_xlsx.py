import glob,openpyxl
from collections import defaultdict
from openpyxl.styles import Font,PatternFill,Alignment
B='/home/claude/product-crawler/assets/benchmarks/grainger/'
R={}
for f in glob.glob(B+'g*.txt'):
    for ln in open(f,encoding='utf-8'):
        ln=ln.rstrip('\n')
        if not ln.strip(): continue
        i,lv,n,fa,c=ln.split('|');R[int(i)]=dict(lv=int(lv),n=n,fa=fa.strip(),c=c)
ids=sorted(R);assert ids==list(range(len(ids)))
st=[];
for i in ids:
    x=R[i]
    while st and R[st[-1]]['lv']>=x['lv']: st.pop()
    x['par']=st[-1] if st else -1
    x['cls']=x['c'] or (R[x['par']]['cls'] if x['par']>=0 else 'B2B')
    x['path']=[R[p]['fa'] for p in st]+[x['fa']];x['pen']=[R[p]['n'] for p in st]+[x['n']]
    x['kids']=0
    if x['par']>=0: R[x['par']]['kids']+=1
    st.append(i)
wb=openpyxl.Workbook();hf=Font(bold=True,color='FFFFFF');fill=PatternFill('solid',fgColor='1F4E78')
def sheet(ws,hdr,rows,w):
    ws.sheet_view.rightToLeft=True;ws.append(hdr)
    for c in ws[1]: c.font=hf;c.fill=fill;c.alignment=Alignment(horizontal='center')
    for r in rows: ws.append(r)
    for k,v in enumerate(w): ws.column_dimensions[openpyxl.utils.get_column_letter(k+1)].width=v
    ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
S=defaultdict(lambda:defaultdict(int))
for i in ids:
    x=R[i];top=x['path'][0];S[top]['n']+=1;S[top]['leaf']+=x['kids']==0;S[top][x['cls']]+=x['kids']==0;S[top]['en']=x['pen'][0];S[top]['d']=max(S[top]['d'],x['lv'])
ws=wb.active;ws.title='خلاصه'
sheet(ws,['دسته اصلی','انگلیسی','کل دسته‌ها','شاخه نهایی','نهایی B2B','نهایی ترکیبی','نهایی مصرفی','عمق'],[[k,v['en'],v['n'],v['leaf'],v['B2B'],v['ترکیبی'],v['مصرفی'],v['d']] for k,v in S.items()],[30,30,12,12,12,12,12,8])
L=8
sheet(wb.create_sheet('درخت کامل'),['شناسه','سطح','دسته (فارسی)','دسته (انگلیسی)','نوع بازار','تعداد زیرشاخه','نهایی؟']+[f'سطح {k}' for k in range(1,L+1)]+['مسیر انگلیسی'],
      [[i,R[i]['lv'],R[i]['fa'],R[i]['n'],R[i]['cls'],R[i]['kids'],'بله' if R[i]['kids']==0 else '']+R[i]['path']+['']*(L-len(R[i]['path']))+[' > '.join(R[i]['pen'])] for i in ids],[7,5,32,34,9,9,6]+[22]*L+[60])
ws=wb.create_sheet('توضیحات');ws.sheet_view.rightToLeft=True
for t in [f'منبع: grainger.com/category — {len(S)} دسته اصلی، {len(ids):,} دسته و زیردسته تا عمق ۸ (آبان ۱۴۰۵).','درخت گرینجر واقعی است (هر دسته یک والد دارد)؛ ستون‌های «سطح ۱..۸» مسیر هر دسته را نشان می‌دهند.','گرینجر تعداد کالا را در صفحه دسته نشان نمی‌دهد؛ برای همین ستون تعداد کالا ندارد.','نوع بازار برای هر دسته از والدش به ارث می‌رسد مگر جایی که جدا تعیین شده. ترجمه‌ها دستی و بدون API.','چند نام در خود سایت ناقص است (مثل «Air»، «75 gpm»، «4140») و همان‌طور ترجمه/نگه داشته شده.']: ws.append([t])
ws.column_dimensions['A'].width=130
out='/home/claude/product-crawler/assets/benchmarks/grainger-categories-fa.xlsx';wb.save(out)
print(out,len(ids),sum(1 for i in ids if R[i]['kids']==0),{c:sum(1 for i in ids if R[i]['kids']==0 and R[i]['cls']==c) for c in ['B2B','ترکیبی','مصرفی']})
