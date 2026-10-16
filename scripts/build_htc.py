# -*- coding: utf-8 -*-
"""把《程序员做饭指南》(HowToCook, The Unlicense) 的 markdown 菜谱解析进 recipes.db，图片拷到 data/recipe_img/htc/。
几百道，带不少成品图和步骤图，几秒钟跑完——最小可用的菜谱库就是它。"""
import os, re, json, sqlite3, shutil, sys, hashlib

if len(sys.argv) < 2:
    sys.exit('用法: python scripts/build_htc.py <HowToCook 仓库的路径>   (git clone --depth 1 https://github.com/Anduin2017/HowToCook)')
ROOT = os.path.join(sys.argv[1], 'dishes')
DATA = os.environ.get('FOOD_DATA_DIR') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
IMG_DIR = os.path.join(DATA, 'recipe_img', 'htc')
DB = os.path.join(DATA, 'recipes.db')
CAT = {'meat_dish': '荤菜', 'vegetable_dish': '素菜', 'aquatic': '水产', 'breakfast': '早餐', 'staple': '主食',
       'semi-finished': '半成品', 'soup': '汤羹', 'drink': '饮品', 'condiment': '酱料', 'dessert': '甜品'}
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from food_rules import cuisine_of, region_of  # 和下厨房入库用同一套规则
from schema import open_recipes_db

IMG_RE = re.compile(r'!\[[^\]]*\]\(([^)\s]+)\)')


def read_md(path):
    txt = open(path, encoding='utf-8').read()
    lines = txt.split('\n')
    title = ''
    for l in lines:
        if l.startswith('# '):
            title = l[2:].strip()
            break
    title = re.sub(r'的做法$|的制作方法$', '', title).strip()
    # 按二级标题切块
    sections = {}
    cur = '_head'
    sections[cur] = []
    for l in lines:
        if l.startswith('## '):
            cur = l[3:].strip()
            sections[cur] = []
        else:
            sections[cur].append(l)

    def sec(*names):
        for k in sections:
            if any(nm in k for nm in names):
                return sections[k]
        return []

    def bullets(ls):
        out = []
        for l in ls:
            s = l.strip()
            if not s or s.startswith('#'):
                continue
            s2 = re.sub(r'^([-*+]|\d+[.、)]|\(\d+\))\s*', '', s)
            if s2 and s2 != s:
                out.append(s2)
            elif out and not re.match(r'^!\[', s):
                out[-1] = out[-1] + ' ' + s2  # 续行
        return out

    head = sections.get('_head', [])
    main_img = ''
    desc = []
    for l in head:
        m = IMG_RE.search(l)
        if m and not main_img:
            main_img = m.group(1)
            continue
        s = l.strip()
        if not s or s.startswith('#') or s.startswith('!['):
            continue
        if s.startswith('预估烹饪难度'):
            continue
        desc.append(s)
    diff = ''
    m = re.search(r'预估烹饪难度[：:]\s*(★+)', txt)
    if m:
        diff = str(len(m.group(1)))
    ingredients = bullets(sec('必备原料', '原料'))
    calc = [x for x in bullets(sec('计算')) if x]
    ops = sec('操作')
    steps, step_imgs = [], []
    for l in ops:
        s = l.strip()
        m = IMG_RE.search(s)
        if m:
            if steps:
                step_imgs[-1].append(m.group(1))
            continue
        if not s or s.startswith('#'):
            continue
        s2 = re.sub(r'^([-*+]|\d+[.、)]|\(\d+\))\s*', '', s)
        if s2 != s:
            steps.append(s2)
            step_imgs.append([])
        elif steps:
            steps[-1] += ' ' + s2
    extra = [x.strip() for x in sec('附加内容') if x.strip() and not x.strip().startswith('#')]
    return dict(title=title, main_img=main_img, desc='\n'.join(desc)[:600], diff=diff, ingredients=ingredients,
                calc=calc, steps=steps, step_imgs=step_imgs, extra='\n'.join(extra)[:1200])


def copy_img(src_path, key):
    if not os.path.isfile(src_path):
        return ''
    ext = os.path.splitext(src_path)[1].lower() or '.jpg'
    name = hashlib.md5(key.encode()).hexdigest()[:12] + ext
    os.makedirs(IMG_DIR, exist_ok=True)
    dst = os.path.join(IMG_DIR, name)
    if not os.path.exists(dst):
        shutil.copyfile(src_path, dst)
    return 'htc/' + name


os.makedirs(DATA, exist_ok=True)
con = open_recipes_db(DB)
con.execute("DELETE FROM recipes WHERE src='htc'")
rows = 0
withimg = 0
for cat in sorted(os.listdir(ROOT)):
    if cat == 'template' or cat not in CAT:
        continue
    for dp, dn, fn in os.walk(os.path.join(ROOT, cat)):
        for f in fn:
            if not f.endswith('.md'):
                continue
            p = os.path.join(dp, f)
            try:
                r = read_md(p)
            except Exception as e:
                print('skip', p, e, file=sys.stderr)
                continue
            if not r['title'] or len(r['steps']) < 2 or not r['ingredients']:
                print('thin', p, len(r['steps']), len(r['ingredients']), file=sys.stderr)
                continue
            rel = os.path.relpath(p, ROOT)
            img = copy_img(os.path.join(dp, r['main_img']), rel) if r['main_img'] else ''
            simgs = [[copy_img(os.path.join(dp, x), rel + '#' + x) for x in L] for L in r['step_imgs']]
            simgs = [[x for x in L if x] for L in simgs]
            if img:
                withimg += 1
            ings = r['ingredients']
            if r['calc']:
                ings = ings + ['— 用量 —'] + r['calc']
            desc = r['desc']
            if r['extra']:
                desc = (desc + '\n\n' if desc else '') + '小贴士：' + r['extra']
            tags = [CAT[cat]] + (['难度' + r['diff'] + '星'] if r['diff'] else [])
            name = r['title']
            con.execute('INSERT INTO recipes(src,src_id,name,dish,cuisine,region,kinds,descr,ingredients,steps,step_images,image,image_src,tags,author,score) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('htc', rel, name, name, cuisine_of(name, tags), region_of(name, tags), json.dumps([CAT[cat]], ensure_ascii=False),
                         desc[:1500], json.dumps(ings, ensure_ascii=False), json.dumps(r['steps'], ensure_ascii=False),
                         json.dumps(simgs, ensure_ascii=False), img, 'HowToCook' if img else '', json.dumps(tags, ensure_ascii=False),
                         'HowToCook', 10.0))
            rows += 1
con.commit()
con.close()
print('inserted', rows, 'with image', withimg)
