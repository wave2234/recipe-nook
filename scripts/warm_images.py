# -*- coding: utf-8 -*-
"""给菜谱库里常见的菜名预先配图: 按菜名(dish)出现次数从高到低, 去中文维基词条拿首图, 压成 800 宽 JPEG 存到 data/recipe_img/wiki/。
和 app/food.py 的按需找图用同一套缓存(food.db 的 dish_images, key=菜名) 和同一种文件名, 两边不会重复下载。
只认词条名对得上的首图, 不做全文搜索, 宁缺毋滥。每 0.8 秒查一个菜名, 对维基客气点。
用法: WIKI_UA='...(+你的联系方式)' python3 scripts/warm_images.py [最多处理多少个菜名, 默认 6000]
6000 个菜名大约一个半小时; 中文维基里有词条且带图的菜, 大概占两成。
"""
import hashlib, io, os, sqlite3, sys, time
import httpx
from PIL import Image, ImageOps

DATA = os.environ.get('FOOD_DATA_DIR') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
RDB = os.path.join(DATA, 'recipes.db')
FDB = os.path.join(DATA, 'food.db')
IMG_DIR = os.path.join(DATA, 'recipe_img', 'wiki')
# 维基要求 UA 里有能联系到你的网址或邮箱(和 .env 里的 WIKI_UA 一样), 不然 403
UA = os.environ.get('WIKI_UA') or 'RecipeNook/1.0 (+https://github.com/wave2234/recipe-nook; self-hosted recipe book) python-httpx'
GENERIC = {'早餐', '午餐', '晚餐', '宵夜', '夜宵', '便当', '料理', '家常菜', '甜品', '饮品', '小吃', '菜', '饭', '汤', '粥', '面', '零食', '下午茶', '烘焙', '蛋糕', '沙拉', '鸡尾酒', '面包', '饼干', '主食', '素菜', '荤菜'}
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 6000

os.makedirs(IMG_DIR, exist_ok=True)
rc = sqlite3.connect(RDB, timeout=30)
fc = sqlite3.connect(FDB, timeout=30)
fc.execute('CREATE TABLE IF NOT EXISTS dish_images(key TEXT PRIMARY KEY, path TEXT, src TEXT, created TEXT)')
done = {r[0] for r in fc.execute('SELECT key FROM dish_images')}
dishes = [(r[0], r[1]) for r in rc.execute("SELECT dish, COUNT(*) n FROM recipes WHERE dish!='' AND image='' GROUP BY dish ORDER BY n DESC, dish")
          if r[0] not in done and r[0] not in GENERIC and 2 <= len(r[0]) <= 12][:LIMIT]
print(f'to look up: {len(dishes)} dishes (already cached {len(done)})', flush=True)

cl = httpx.Client(timeout=httpx.Timeout(12.0, connect=6.0), headers={'User-Agent': UA}, follow_redirects=True)
hit = miss = err = 0
t0 = time.time()
for i, (dish, n) in enumerate(dishes, 1):
    url = ''
    try:
        r = cl.get('https://zh.wikipedia.org/w/api.php', params={'action': 'query', 'prop': 'pageimages', 'titles': dish, 'pithumbsize': 800, 'format': 'json', 'redirects': 1, 'converttitles': 1})
        if r.status_code == 429:
            print('429, sleeping 60s', flush=True); time.sleep(60); continue
        for p in (r.json().get('query') or {}).get('pages', {}).values():
            th = (p.get('thumbnail') or {}).get('source')
            if th and not (p.get('pageimage') or '').lower().endswith('.svg'):
                url = th
    except Exception as e:
        err += 1; time.sleep(3); continue
    path = ''
    if url:
        try:
            raw = cl.get(url).content
            if 2000 < len(raw) < 8_000_000:
                im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert('RGB')
                im.thumbnail((800, 800))
                buf = io.BytesIO(); im.save(buf, 'JPEG', quality=82, optimize=True)
                name = hashlib.md5(dish.encode()).hexdigest()[:14] + '.jpg'
                with open(os.path.join(IMG_DIR, name), 'wb') as f:
                    f.write(buf.getvalue())
                path = 'wiki/' + name
        except Exception:
            path = ''
    fc.execute('INSERT OR REPLACE INTO dish_images(key,path,src,created) VALUES(?,?,?,?)', (dish, path, 'Wikipedia' if path else '', str(time.time())))
    fc.commit()
    if path:
        rc.execute("UPDATE recipes SET image=?, image_src='Wikipedia' WHERE dish=? AND image=''", (path, dish)); rc.commit(); hit += 1
    else:
        miss += 1
    if i % 100 == 0:
        print(f'{i}/{len(dishes)} hit {hit} miss {miss} err {err} {round(time.time() - t0)}s', flush=True)
    time.sleep(0.8)
print(f'done: {len(dishes)} looked up, hit {hit} miss {miss} err {err} in {round(time.time() - t0)}s', flush=True)
