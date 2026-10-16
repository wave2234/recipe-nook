# -*- coding: utf-8 -*-
"""从下厨房语料库挑菜入库（省内存两遍版）。

语料: XiaChuFang Recipe Corpus 的 recipe_corpus_finetune.json(约 148 万条, 1.9GB, 一行一条 JSON),
来自 https://github.com/xxxiaol/counterfactual-recipe-generation 。没有声明许可, 只建议个人、非商业使用, 别再分发。

每道菜(dish)中餐留 2 条、其他菜系留 4 条, 按步骤/食材/描述打分挑最好的, 最后大约 28 万条。

第一遍只流式读，每条记一个 (分数, 文件偏移) 放进对应的分组，每组最多留几个；
第二遍按偏移直接 seek 读出选中的那二十来万条，写进 recipes.db。
全程内存只放分组表和偏移集合，几十 MB。千万别 json.load 整个文件——
第一版就是这么把一台 4G 内存的小服务器卡死的。保险起见再套一层内存上限:
  systemd-run --scope -p MemoryMax=600M python scripts/build_xcf.py <路径>
"""
import json, re, sqlite3, collections, sys, time, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if len(sys.argv) < 2:
    sys.exit('用法: python scripts/build_xcf.py <recipe_corpus_finetune.json 的路径>')
SRC = sys.argv[1]
DATA = os.environ.get('FOOD_DATA_DIR') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
os.makedirs(DATA, exist_ok=True)
DB = os.path.join(DATA, 'recipes.db')

from food_rules import cuisine_of, region_of, kinds_of, score, norm
from schema import open_recipes_db

KEEP = {'中餐': 2}
KEEP_OTHER = 4

# ---------- 第一遍：只记 (分数, 偏移) ----------
t0 = time.time()
groups = collections.defaultdict(list)
n = 0
with open(SRC, 'rb') as f:
    while True:
        off = f.tell()
        line = f.readline()
        if not line:
            break
        n += 1
        try:
            r = json.loads(line)
        except Exception:
            continue
        name = (r.get('name') or '').strip()
        dish = (r.get('dish') or '').strip()
        if dish == 'Unknown':
            dish = ''
        steps = [s.strip() for s in (r.get('recipeInstructions') or []) if s and s.strip()]
        ings = [i.strip() for i in (r.get('recipeIngredient') or []) if i and i.strip()]
        if not name or len(steps) < 3 or len(ings) < 2:
            continue
        tags = [k for k in (r.get('keywords') or []) if not re.search(r'做法$|怎么做$', k)]
        cu = cuisine_of(name, tags)
        key = (cu, dish or norm(name)[:12])
        sc = score(steps, ings, (r.get('description') or '').strip())
        if sc < 2:
            continue
        g = groups[key]
        g.append((sc, off))
        lim = KEEP.get(cu, KEEP_OTHER) * 3
        if len(g) > lim:
            g.sort(key=lambda x: -x[0])
            del g[lim:]
        if n % 200000 == 0:
            print('pass1', n, len(groups), round(time.time() - t0), 's', file=sys.stderr, flush=True)
print('pass1 done', n, 'groups', len(groups), round(time.time() - t0), 's', file=sys.stderr, flush=True)

# 选中的偏移 -> 组键
chosen = {}
for key, g in groups.items():
    g.sort(key=lambda x: -x[0])
    for sc, off in g:
        chosen[off] = (key, sc)
del groups
print('candidates', len(chosen), file=sys.stderr, flush=True)

# ---------- 第二遍：按偏移读出，去重后入库 ----------
con = open_recipes_db(DB)
con.execute("DELETE FROM recipes WHERE src='xcf'")
con.commit()
seen = collections.defaultdict(set)   # key -> set(norm name)
rows = 0
percu = collections.Counter()
with open(SRC, 'rb') as f:
    for off in sorted(chosen):
        key, sc = chosen[off]
        cu = key[0]
        if len(seen[key]) >= KEEP.get(cu, KEEP_OTHER):
            continue
        f.seek(off)
        try:
            r = json.loads(f.readline())
        except Exception:
            continue
        name = (r.get('name') or '').strip()
        nm = norm(name)
        if nm in seen[key]:
            continue
        seen[key].add(nm)
        dish = (r.get('dish') or '').strip()
        if dish == 'Unknown':
            dish = ''
        steps = [s.strip() for s in (r.get('recipeInstructions') or []) if s and s.strip()]
        ings = [i.strip() for i in (r.get('recipeIngredient') or []) if i and i.strip()]
        tags = [k for k in (r.get('keywords') or []) if not re.search(r'做法$|怎么做$', k)]
        desc = (r.get('description') or '').strip()[:600]
        con.execute('INSERT INTO recipes(src,src_id,name,dish,cuisine,region,kinds,descr,ingredients,steps,step_images,image,image_src,tags,author,score) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('xcf', str(off), name, dish, cu, region_of(name, tags) if cu == '中餐' else '',
                     json.dumps(kinds_of(name, tags), ensure_ascii=False), desc,
                     json.dumps(ings, ensure_ascii=False), json.dumps(steps, ensure_ascii=False), '[]', '', '',
                     json.dumps(tags[:8], ensure_ascii=False), r.get('author') or '', sc))
        rows += 1
        percu[cu] += 1
        if rows % 10000 == 0:
            con.commit()
            print('pass2', rows, round(time.time() - t0), 's', file=sys.stderr, flush=True)
con.commit()
con.close()
print('inserted', rows, dict(percu), 'in', round(time.time() - t0), 's')
