"""Merge the 4 benchmark trees (Made-in-China, McMaster, Grainger, MonotaRO) into one table."""
import csv,glob,re,openpyxl
B='/home/claude/product-crawler/assets/benchmarks/'
rows=[]
def add(src,path,fa,orig,cls,cnt,leaf,lvl):
    rows.append(dict(src=src,path=' > '.join(p for p in path if p and p!='-'),fa=fa,orig=orig,cls=cls or 'B2B',cnt=cnt,leaf=leaf,lvl=lvl))
# Made-in-China
wb=openpyxl.load_workbook(B+'made-in-china-categories-fa.xlsx',read_only=True)
for r in list(wb['زیردسته‌ها'].iter_rows(values_only=True))[1:]:
    add('Made-in-China',[r[0]],r[1],r[6],r[2],'',0,2)
for r in list(wb['شاخه‌ها'].iter_rows(values_only=True))[1:]:
    if r[4]: continue              # duplicate of a leaf under another sub
    add('Made-in-China',[r[0],r[1]],r[2],r[7],r[3],'',1,3)
# MonotaRO
wb=openpyxl.load_workbook(B+'monotaro/monotaro-categories-fa.xlsx',read_only=True)
for r in list(wb['دسته‌ها (سطح ۳)'].iter_rows(values_only=True))[1:]:
    add('MonotaRO',[r[0],r[1]],r[2],r[6],r[3],'',0,3)
for r in list(wb['شاخه‌ها (سطح ۴)'].iter_rows(values_only=True))[1:]:
    add('MonotaRO',[r[0],r[1],r[2]],r[3],r[6],r[4],r[5],1,4)
# McMaster
wb=openpyxl.load_workbook(B+'mcmaster-categories-fa.xlsx',read_only=True)
for r in list(wb['دسته‌ها (سطح ۳)'].iter_rows(values_only=True))[1:]:
    add('McMaster',[r[0],r[1]],r[2],r[6],r[3],'',0,3)
for r in list(wb['خانواده‌ها (سطح ۴)'].iter_rows(values_only=True))[1:]:
    add('McMaster',[r[0],r[1],r[2]],r[3],r[6],r[4],r[5],1,4)
# Grainger
wb=openpyxl.load_workbook(B+'grainger-categories-fa.xlsx',read_only=True)
for r in list(wb['درخت کامل'].iter_rows(values_only=True))[1:]:
    path=[p for p in r[7:15] if p]
    add('Grainger',path[:-1],r[2],r[3],r[4],'',1 if r[6] else 0,r[1])
def norm(s):
    s=re.sub(r'\(.*?\)','',str(s or ''));s=s.replace('‌',' ').replace('ي','ی').replace('ك','ک')
    s=re.sub(r'[^\w\s]',' ',s);return ' '.join(s.split())
from collections import defaultdict
srcs=defaultdict(set)
for x in rows: x['key']=norm(x['fa']);srcs[x['key']].add(x['src'])
for x in rows: x['nsrc']=len(srcs[x['key']]);x['srcs']='، '.join(sorted(srcs[x['key']]))
with open(B+'all_benchmarks_fa.csv','w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0].keys()));w.writeheader();w.writerows(rows)
import collections
print(len(rows),collections.Counter(x['src'] for x in rows),collections.Counter(x['nsrc'] for x in rows if x['leaf']))
