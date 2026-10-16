# -*- coding: utf-8 -*-
"""把食物成分表(1725 种常见食物, 每 100 克的热量和营养素)装进 recipes.db 的 foods 表。

数据来自 npm 包 cn-food-mcp (MIT), 包里的 dist/data/foods.json。两种用法:
  python scripts/build_foods.py                    # 机器上有 npm 就自动 npm pack 下来
  python scripts/build_foods.py path/to/foods.json # 自己下好的
"""
import json, os, subprocess, sys, tarfile, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schema import open_recipes_db

DATA = os.environ.get('FOOD_DATA_DIR') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
os.makedirs(DATA, exist_ok=True)
DB = os.path.join(DATA, 'recipes.db')


def load_items():
    if len(sys.argv) > 1:
        return json.load(open(sys.argv[1], encoding='utf-8'))
    with tempfile.TemporaryDirectory() as td:
        try:
            out = subprocess.run(['npm', 'pack', 'cn-food-mcp', '--silent'], cwd=td, capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            sys.exit('没找到 npm。装个 Node, 或者自己下 cn-food-mcp 的 foods.json 再把路径传进来')
        tgz = os.path.join(td, (out.stdout.strip().splitlines() or [''])[-1])
        if not os.path.isfile(tgz):
            sys.exit('npm pack 没成功: ' + (out.stderr or out.stdout)[:300])
        with tarfile.open(tgz) as t:
            f = t.extractfile('package/dist/data/foods.json')
            return json.load(f)


items = load_items()
con = open_recipes_db(DB)
con.execute('DELETE FROM foods')
n = 0
for x in items:
    name = (x.get('name') or '').strip()
    if not name or x.get('energy') is None:
        continue
    con.execute('INSERT INTO foods(id,name,kcal,protein,fat,carb,fiber,sodium,raw) VALUES(?,?,?,?,?,?,?,?,?)',
                (x.get('id'), name, x.get('energy'), x.get('protein'), x.get('fat'), x.get('carbohydrate'),
                 x.get('fiber'), x.get('sodium'), json.dumps(x, ensure_ascii=False)))
    n += 1
con.commit()
con.close()
print('foods:', n)
