"""小食谱 (recipe nook) —— 菜谱库 + 吃什么转盘 + 按餐记的饮食记录, 一个 FastAPI router。

库: data/recipes.db  菜谱 recipes (下厨房语料 xcf / 程序员做饭指南 htc) + 食物成分表 foods (每 100g)
    data/food.db     使用者自己的: meals 每一样吃的, settings 目标与分类, kcal_cache 估过的热量,
                     dish_images 维基找图缓存, day_scores AI 打的分, favorites 收藏
图: data/recipe_img/htc (随 HowToCook 带的), data/recipe_img/wiki (按需从中文维基拉下来缓存的)

单独跑: 见仓库根目录 server.py。嵌进别的 FastAPI 程序:
    from app import food
    food.check_token = 你的校验函数          # token -> bool, 不换就用环境变量 FOOD_TOKEN
    food.llm_complete = 你的补全函数         # async (system, user) -> str, 不换就用 app/llm.py
    app.include_router(food.router)
    tools.append(food.TOOL_DEF); ... await food.tool_dispatch(args)
"""
from __future__ import annotations
import asyncio, hashlib, hmac, json, os, random, re, sqlite3, time, uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from app import llm

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get('FOOD_DATA_DIR') or (ROOT / 'data'))
RDB = DATA_DIR / 'recipes.db'
FDB = DATA_DIR / 'food.db'
IMG_DIR = DATA_DIR / 'recipe_img'
router = APIRouter()

CUISINES = ['中餐', '日料', '韩餐', '西餐', '东南亚', '南亚中东']
DEFAULT_LOG_CATS = ['家常菜', '日料', '韩餐', '西餐', '东南亚', '快餐', '外卖', '甜品饮品', '其他']
SLOTS = ['早餐', '午餐', '晚餐', '加餐', '夜宵']
RATINGS = ['好吃', '一般', '难吃']
ACTIVITY = {'sedentary': 1.2, 'light': 1.375, 'moderate': 1.55, 'active': 1.725}
# 维基要求 UA 里带一个能联系到你的地址(网址或邮箱), 不带会 403。换成你自己的: 环境变量 WIKI_UA
UA = os.environ.get('WIKI_UA') or 'RecipeNook/1.0 (+https://github.com/wave2234/recipe-nook; self-hosted recipe book) python-httpx'


def _env_token_check(token: str) -> bool:
    """默认校验: 设了 FOOD_TOKEN 就要 Bearer 一致; 没设就不校验(只适合在自己电脑上跑)。"""
    want = os.environ.get('FOOD_TOKEN', '')
    return (not want) or hmac.compare_digest(token.encode(), want.encode())


check_token = _env_token_check   # 嵌进别的程序时换成那边的校验: food.check_token = fn
llm_complete = llm.complete      # 估热量用的模型: async (system, user) -> str


def _require_auth(authorization: str = Header(default='')) -> None:
    token = (authorization or '').removeprefix('Bearer ').strip()
    if not check_token(token):
        raise HTTPException(status_code=401, detail='unauthorized')


# ---------- 库 ----------
RECIPE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS recipes(
 id INTEGER PRIMARY KEY, src TEXT NOT NULL, src_id TEXT, name TEXT NOT NULL, dish TEXT, cuisine TEXT, region TEXT,
 kinds TEXT, descr TEXT, ingredients TEXT, steps TEXT, step_images TEXT, image TEXT, image_src TEXT, tags TEXT,
 author TEXT, score REAL, created TEXT DEFAULT (datetime('now')));
CREATE INDEX IF NOT EXISTS ix_recipes_cuisine ON recipes(cuisine);
CREATE INDEX IF NOT EXISTS ix_recipes_dish ON recipes(dish);
CREATE INDEX IF NOT EXISTS ix_recipes_name ON recipes(name);
CREATE TABLE IF NOT EXISTS foods(id INTEGER PRIMARY KEY, name TEXT NOT NULL, kcal REAL, protein REAL, fat REAL, carb REAL, fiber REAL, sodium REAL, raw TEXT);
CREATE INDEX IF NOT EXISTS ix_foods_name ON foods(name);
'''
_schema_ok = False


def _rdb() -> sqlite3.Connection:
    global _schema_ok
    RDB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(RDB, timeout=10)
    c.row_factory = sqlite3.Row
    if not _schema_ok:  # 还没跑建库脚本时也能打开页面, 只是空的
        c.executescript(RECIPE_SCHEMA)
        _schema_ok = True
    return c


def _fdb() -> sqlite3.Connection:
    FDB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(FDB, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript('''
    CREATE TABLE IF NOT EXISTS meals(id TEXT PRIMARY KEY, dt TEXT, d TEXT, slot TEXT, name TEXT, cuisine TEXT,
      rating TEXT, note TEXT, portion TEXT, kcal REAL, kcal_src TEXT, recipe_id INTEGER, source TEXT, created TEXT);
    CREATE INDEX IF NOT EXISTS ix_meals_d ON meals(d);
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS kcal_cache(key TEXT PRIMARY KEY, name TEXT, portion TEXT, kcal REAL, protein REAL, fat REAL, carb REAL, note TEXT, src TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS dish_images(key TEXT PRIMARY KEY, path TEXT, src TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS day_scores(d TEXT PRIMARY KEY, score INTEGER, comment TEXT, by TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS favorites(recipe_id INTEGER PRIMARY KEY, created TEXT);
    ''')
    return c


def _setting(c: sqlite3.Connection, k: str, default=None):
    r = c.execute('SELECT v FROM settings WHERE k=?', (k,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r['v'])
    except Exception:
        return r['v']


def _set_setting(c: sqlite3.Connection, k: str, v) -> None:
    c.execute('INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, json.dumps(v, ensure_ascii=False)))


def _log_cats(c: sqlite3.Connection) -> list[str]:
    v = _setting(c, 'cuisines')
    return v if isinstance(v, list) and v else DEFAULT_LOG_CATS[:]


# ---------- 小工具 ----------
def _parse_date(s) -> date:
    s = str(s or '').strip()
    if not s:
        return datetime.now().date()
    m = re.match(r'^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$', s)
    if not m:
        raise HTTPException(400, '日期要写成 YYYY-MM-DD')
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    except ValueError:
        raise HTTPException(400, '没有这一天')


def _span(rng: str, base: date) -> tuple[date, date, str]:
    if rng == 'day':
        wd = '日一二三四五六'[int(base.strftime('%w'))]
        return base, base, f'{base.year}年{base.month}月{base.day}日 周{wd}'
    if rng == 'week':
        a = base - timedelta(days=base.weekday())
        return a, a + timedelta(days=6), f'{a.month}月{a.day}日 – {(a + timedelta(days=6)).month}月{(a + timedelta(days=6)).day}日'
    if rng == 'year':
        return date(base.year, 1, 1), date(base.year, 12, 31), f'{base.year}年'
    a = date(base.year, base.month, 1)
    b = (date(base.year + 1, 1, 1) if base.month == 12 else date(base.year, base.month + 1, 1)) - timedelta(days=1)
    return a, b, f'{base.year}年{base.month}月'


def _kc(n) -> str:
    try:
        return str(int(round(float(n))))
    except Exception:
        return '?'


def _j(s, default):
    try:
        v = json.loads(s) if s else default
        return v if v is not None else default
    except Exception:
        return default


_GENERIC_TERMS = {'早餐', '午餐', '晚餐', '宵夜', '夜宵', '便当', '料理', '家常菜', '甜品', '饮品', '小吃', '菜', '饭', '汤', '粥', '面', '零食', '下午茶', '烘焙', '蛋糕', '沙拉', '鸡尾酒'}


def _clean_dish(name: str) -> str:
    """查图用的菜名: 去掉各种括号里的话、前面的形容、后面的做法字样, 留一个短菜名; 太泛的词(早餐/甜品)不拿去查, 免得配上不相干的图。"""
    s = re.sub(r'[【\[（(「『《〈][^】\]）)」』》〉]*[】\]）)」』》〉]', '', name or '')
    s = re.split(r'[,，。!！?？:：;；~～|丨/\-—–\s]', s)[0]
    s = re.sub(r'(的做法|做法|怎么做|教程|详解|版|超好吃|好吃|简单|懒人|快手|家常|自制|正宗|零失败|新手|必备|最全|秘制|私房|减脂|低卡|超级|巨|无敌|史上最)', '', s)
    s = re.sub(r'^[^一-鿿]+', '', s)
    s = s.strip()[:12]
    return '' if s in _GENERIC_TERMS or len(s) < 2 else s


# ---------- 菜谱 ----------
def _card(r) -> dict:
    img = r['image'] or ''
    return {'id': r['id'], 'name': r['name'], 'dish': r['dish'] or '', 'cuisine': r['cuisine'] or '', 'region': r['region'] or '',
            'kinds': _j(r['kinds'], []), 'tags': _j(r['tags'], []), 'score': r['score'], 'src': r['src'],
            'image': ('/food-img/' + img) if img else '', 'image_src': r['image_src'] or ''}


def _full(r) -> dict:
    d = _card(r)
    d.update({'descr': r['descr'] or '', 'ingredients': _j(r['ingredients'], []), 'steps': _j(r['steps'], []),
              'step_images': [[('/food-img/' + x) for x in L] for L in _j(r['step_images'], [])], 'author': r['author'] or ''})
    return d


CARD_COLS = 'id,src,name,dish,cuisine,region,kinds,tags,score,image,image_src'


def search_recipes(q: str = '', cuisine: str = '', region: str = '', kind: str = '', ing: bool = False, page: int = 0, limit: int = 30) -> dict:
    c = _rdb()
    where, args = [], []
    q = (q or '').strip()[:40]
    terms = [t for t in re.split(r'[\s,，、;；]+', q) if t][:5]  # "鸡蛋 西红柿" 这样写就是都要有
    for t in terms:
        if ing:
            where.append('ingredients LIKE ?'); args.append(f'%{t}%')
        else:
            where.append('(name LIKE ? OR dish LIKE ? OR tags LIKE ?)'); args += [f'%{t}%'] * 3
    if cuisine:
        where.append('cuisine=?'); args.append(cuisine)
    if region:
        where.append('region=?'); args.append(region)
    if kind:
        where.append('kinds LIKE ?'); args.append(f'%"{kind}"%')
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    limit = max(1, min(int(limit or 30), 60)); page = max(0, int(page or 0))
    # 没搜索词时不排序全表(28 万行排序太慢), 给带图/高分的一页
    order = " ORDER BY (image!='') DESC, score DESC, id" if (q or kind or region) else " ORDER BY (image!='') DESC, id"
    rows = c.execute(f'SELECT {CARD_COLS} FROM recipes{w}{order} LIMIT ? OFFSET ?', args + [limit + 1, page * limit]).fetchall()
    more = len(rows) > limit
    return {'items': [_card(r) for r in rows[:limit]], 'more': more, 'page': page}


def get_recipe(rid: int) -> dict | None:
    c = _rdb()
    r = c.execute('SELECT * FROM recipes WHERE id=?', (rid,)).fetchone()
    return _full(r) if r else None


def random_recipes(cuisine: str = '', kind: str = '', n: int = 8, seed_q: str = '') -> list[dict]:
    """转盘候选: 优先带图或有正经菜名(dish)且分数高的, 不够再放宽。"""
    c = _rdb()
    n = max(1, min(int(n or 8), 16))
    base, args = [], []
    if cuisine:
        base.append('cuisine=?'); args.append(cuisine)
    if kind:
        base.append('kinds LIKE ?'); args.append(f'%"{kind}"%')
    if seed_q:
        base.append('(name LIKE ? OR dish LIKE ? OR ingredients LIKE ?)'); args += [f'%{seed_q}%'] * 3
    out, seen = [], set()
    for extra in ["(image!='' OR src='htc')", "dish!='' AND score>=6", "score>=6", "1=1"]:
        if len(out) >= n:
            break
        w = ' AND '.join(base + [extra])
        rows = c.execute(f'SELECT {CARD_COLS} FROM recipes WHERE {w} ORDER BY RANDOM() LIMIT ?', args + [n * 2]).fetchall()
        for r in rows:
            key = (r['dish'] or _clean_dish(r['name']) or r['name'])
            if key in seen:
                continue
            seen.add(key); out.append(_card(r))
            if len(out) >= n:
                break
    random.shuffle(out)
    return out[:n]


# ---------- 图: 本地没有就去维基百科找一张存下来 ----------
_img_locks: dict[str, asyncio.Lock] = {}


async def _wiki_lookup(term: str) -> str:
    """返回一张图的 URL, 找不到给空串。只认中文维基词条的首图(词条名对得上才算), 不去 Commons 全文搜——搜出来的常常不是这道菜。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=6.0), headers={'User-Agent': UA}, follow_redirects=True) as cl:
            r = await cl.get('https://zh.wikipedia.org/w/api.php', params={'action': 'query', 'prop': 'pageimages', 'titles': term, 'pithumbsize': 800, 'format': 'json', 'redirects': 1, 'converttitles': 1})
            for p in (r.json().get('query') or {}).get('pages', {}).values():
                th = (p.get('thumbnail') or {}).get('source')
                if th and not (p.get('pageimage') or '').lower().endswith('.svg'):
                    return th
    except Exception:
        pass
    return ''


async def _download(url: str, key: str) -> str:
    import httpx
    name = hashlib.md5(key.encode()).hexdigest()[:14] + '.jpg'
    dst = IMG_DIR / 'wiki' / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=6.0), headers={'User-Agent': UA}, follow_redirects=True) as cl:
        r = await cl.get(url)
        r.raise_for_status()
        if len(r.content) < 2000 or len(r.content) > 8_000_000:
            raise ValueError('bad image size')
        raw = r.content
    # 维基给的常是 960 宽的 PNG, 一张就大几百 K; 压成 800 宽的 JPEG 再存, 手机上看够了
    try:
        import io
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im).convert('RGB')
        im.thumbnail((800, 800))
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=82, optimize=True)
        dst.write_bytes(buf.getvalue())
    except Exception:
        dst.write_bytes(raw)
    return 'wiki/' + name


async def ensure_image(rid: int) -> dict:
    c = _rdb()
    r = c.execute('SELECT id,name,dish,image,image_src FROM recipes WHERE id=?', (rid,)).fetchone()
    if not r:
        raise HTTPException(404, '没有这道菜')
    if r['image']:
        return {'id': rid, 'image': '/food-img/' + r['image'], 'image_src': r['image_src'] or ''}
    terms = []
    for t in (r['dish'] or '', _clean_dish(r['name'])):
        t = (t or '').strip()
        if t and len(t) >= 2 and t not in terms:
            terms.append(t)
    if not terms:
        return {'id': rid, 'image': '', 'image_src': ''}
    f = _fdb()
    for t in terms:
        hit = f.execute('SELECT path,src,created FROM dish_images WHERE key=?', (t,)).fetchone()
        if hit and hit['path']:
            c.execute("UPDATE recipes SET image=?, image_src=? WHERE id=? OR (dish=? AND image='')", (hit['path'], hit['src'], rid, t)); c.commit()
            return {'id': rid, 'image': '/food-img/' + hit['path'], 'image_src': hit['src']}
        if hit and not hit['path'] and (time.time() - float(hit['created'] or 0)) < 30 * 86400:
            continue  # 一个月内查过没有, 先别再去烦
        lock = _img_locks.setdefault(t, asyncio.Lock())
        async with lock:
            url = await _wiki_lookup(t)
            path = ''
            if url:
                try:
                    path = await _download(url, t)
                except Exception:
                    path = ''
            f.execute('INSERT INTO dish_images(key,path,src,created) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET path=excluded.path, src=excluded.src, created=excluded.created',
                      (t, path, 'Wikipedia' if path else '', str(time.time())))
            f.commit()
        if path:
            # 同一道菜(dish 相同)的其他菜谱也一并配上, 列表里就不用一个个点开了
            c.execute("UPDATE recipes SET image=?, image_src=? WHERE id=? OR (dish=? AND image='')", (path, 'Wikipedia', rid, t)); c.commit()
            return {'id': rid, 'image': '/food-img/' + path, 'image_src': 'Wikipedia'}
    return {'id': rid, 'image': '', 'image_src': ''}


# ---------- 热量 ----------
def lookup_kcal(q: str, limit: int = 8) -> list[dict]:
    q = (q or '').strip()[:30]
    if not q:
        return []
    out = []
    c = _rdb()  # 成分表排前面(有据可查), 之前模型估过的排后面
    for r in c.execute("SELECT name,kcal,protein,fat,carb FROM foods WHERE name LIKE ? ORDER BY (name=?) DESC, length(name) LIMIT ?", (f'%{q}%', q, limit)):
        out.append({'name': r['name'], 'portion': '100g', 'kcal': r['kcal'], 'protein': r['protein'], 'fat': r['fat'], 'carb': r['carb'], 'note': '食物成分表, 每100克', 'src': 'table', 'per': '100g'})
    f = _fdb()
    for r in f.execute('SELECT name,portion,kcal,protein,fat,carb,note,src FROM kcal_cache WHERE name LIKE ? ORDER BY created DESC LIMIT ?', (f'%{q}%', limit)):
        out.append({'name': r['name'], 'portion': r['portion'] or '', 'kcal': r['kcal'], 'protein': r['protein'], 'fat': r['fat'], 'carb': r['carb'], 'note': r['note'] or '', 'src': r['src'] or 'ai', 'per': 'portion'})
    return out[: limit * 2]


async def estimate_kcal(name: str, portion: str = '') -> dict:
    """表里查不到就让模型估一个(用便宜的小模型就够), 结果缓存, 同样的菜和份量不再问第二次。"""
    name = (name or '').strip()[:40]
    portion = (portion or '').strip()[:30]
    if not name:
        raise HTTPException(400, '先写吃了什么')
    key = name + '|' + portion
    f = _fdb()
    hit = f.execute('SELECT * FROM kcal_cache WHERE key=?', (key,)).fetchone()
    if hit:
        return {'name': hit['name'], 'portion': hit['portion'], 'kcal': hit['kcal'], 'protein': hit['protein'], 'fat': hit['fat'], 'carb': hit['carb'], 'note': hit['note'], 'src': hit['src'], 'cached': True}
    sys_p = ('你是营养估算器, 以《中国食物成分表》为口径估一餐的热量(参考: 蒸米饭 117 kcal/100g, 馒头 233/100g, 鸡胸脯肉 133/100g 生重, 苹果 52/100g, 鸡蛋 1 个约 70)。'
             '只输出一行 JSON, 不要别的字: {"kcal":整数,"protein":克,"fat":克,"carb":克,"note":"十几个字说明按什么份量估的"}。'
             '份量没说就按一人份普通分量; 烹饪用油按实际少量计入, 不要为了保险往高里估, 给最可能的那个数。')
    try:
        txt = await llm_complete(sys_p, f'吃的: {name}' + (f'\n份量: {portion}' if portion else ''))
    except llm.NotConfigured:
        raise HTTPException(503, '没有配置估算用的模型 (见 .env.example 的 LLM_*)')
    except Exception as e:
        raise HTTPException(502, f'估算模型出错: {type(e).__name__}')
    m = re.search(r'\{.*\}', txt, re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
        kcal = float(d.get('kcal'))
    except Exception:
        raise HTTPException(502, '模型没给出能用的数字: ' + txt[:80])
    def _f(x):
        try:
            return float(x)
        except Exception:
            return None
    row = (key, name, portion, kcal, _f(d.get('protein')), _f(d.get('fat')), _f(d.get('carb')), str(d.get('note') or '')[:60], 'ai', datetime.now().isoformat(timespec='seconds'))
    f.execute('INSERT OR REPLACE INTO kcal_cache(key,name,portion,kcal,protein,fat,carb,note,src,created) VALUES(?,?,?,?,?,?,?,?,?,?)', row)
    f.commit()
    return {'name': name, 'portion': portion, 'kcal': kcal, 'protein': row[4], 'fat': row[5], 'carb': row[6], 'note': row[7], 'src': 'ai', 'cached': False}


# ---------- 目标 ----------
def targets(c: sqlite3.Connection | None = None) -> dict:
    c = c or _fdb()
    s = _setting(c, 'target') or {}
    method = s.get('method') or 'fixed'
    out = {'method': method, 'kcal': None, 'bmr': None, 'tdee': None, 'detail': s}
    if method == 'calc':
        try:
            h, w, a = float(s['height']), float(s['weight']), float(s['age'])
            sex = s.get('sex') or 'f'
            bmr = 10 * w + 6.25 * h - 5 * a + (5 if sex == 'm' else -161)
            tdee = bmr * ACTIVITY.get(s.get('activity') or 'light', 1.375)
            goal = s.get('goal') or 'keep'
            kcal = tdee + ({'lose': -300, 'gain': 300}.get(goal, 0))
            out.update({'kcal': round(kcal), 'bmr': round(bmr), 'tdee': round(tdee)})
        except Exception:
            out['kcal'] = None
    else:
        try:
            out['kcal'] = round(float(s.get('kcal'))) if s.get('kcal') else None
        except Exception:
            out['kcal'] = None
    return out


# ---------- 饮食记录 ----------
def _row(r) -> dict:
    return {'id': r['id'], 'dt': r['dt'], 'date': r['d'], 'slot': r['slot'] or '', 'name': r['name'], 'cuisine': r['cuisine'] or '',
            'rating': r['rating'] or '', 'note': r['note'] or '', 'portion': r['portion'] or '', 'kcal': r['kcal'], 'kcal_src': r['kcal_src'] or '',
            'recipe_id': r['recipe_id'], 'source': r['source'] or 'user'}


def _guess_slot(t: datetime) -> str:
    h = t.hour + t.minute / 60
    if h < 4: return '夜宵'
    if h < 10.5: return '早餐'
    if h < 14.5: return '午餐'
    if h < 17: return '加餐'
    if h < 21.5: return '晚餐'
    return '夜宵'


def add_meal(name: str, cuisine: str = '', slot: str = '', rating: str = '', note: str = '', portion: str = '', kcal=None, kcal_src: str = '',
             date_s: str = '', time_s: str = '', recipe_id=None, source: str = 'user') -> dict:
    name = (name or '').strip()[:60]
    if not name:
        raise HTTPException(400, '吃了什么总得写一个')
    d = _parse_date(date_s)
    now = datetime.now()
    if time_s and re.match(r'^\d{1,2}:\d{2}$', time_s.strip()):
        hh, mm = time_s.strip().split(':')
        t = datetime(d.year, d.month, d.day, int(hh), int(mm))
    elif d == now.date():
        t = now
    else:
        t = datetime(d.year, d.month, d.day, 12, 0)
    slot = slot if slot in SLOTS else _guess_slot(t)
    rating = rating if rating in RATINGS else ''
    c = _fdb()
    cuisine = (cuisine or '').strip()[:12]  # 不在清单里的分类也照记, 页面上看得到
    try:
        kcal_v = float(kcal) if kcal not in (None, '') else None
    except Exception:
        kcal_v = None
    row = (str(uuid.uuid4())[:8], t.isoformat(timespec='minutes'), d.isoformat(), slot, name, cuisine, rating, (note or '').strip()[:300],
           (portion or '').strip()[:30], kcal_v, (kcal_src or ('manual' if kcal_v is not None else ''))[:10],
           int(recipe_id) if recipe_id else None, source, now.isoformat(timespec='seconds'))
    c.execute('INSERT INTO meals(id,dt,d,slot,name,cuisine,rating,note,portion,kcal,kcal_src,recipe_id,source,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', row)
    c.commit()
    return _row(c.execute('SELECT * FROM meals WHERE id=?', (row[0],)).fetchone())


def summarize(rng: str, base: date) -> dict:
    a, b, label = _span(rng, base)
    c = _fdb()
    rows = [_row(r) for r in c.execute('SELECT * FROM meals WHERE d BETWEEN ? AND ? ORDER BY dt', (a.isoformat(), b.isoformat()))]
    tg = targets(c)
    days: dict[str, dict] = {}
    cuis: dict[str, int] = {}
    rate: dict[str, int] = {}
    slots: dict[str, float] = {}
    total = 0.0; known = 0
    for e in rows:
        dd = days.setdefault(e['date'], {'kcal': 0.0, 'n': 0, 'unknown': 0})
        dd['n'] += 1
        if e['kcal'] is not None:
            dd['kcal'] += e['kcal']; total += e['kcal']; known += 1
            slots[e['slot']] = slots.get(e['slot'], 0) + e['kcal']
        else:
            dd['unknown'] += 1
        if e['cuisine']:
            cuis[e['cuisine']] = cuis.get(e['cuisine'], 0) + 1
        if e['rating']:
            rate[e['rating']] = rate.get(e['rating'], 0) + 1
    scores = {r['d']: {'score': r['score'], 'comment': r['comment'] or '', 'by': r['by'] or ''}
              for r in c.execute('SELECT * FROM day_scores WHERE d BETWEEN ? AND ?', (a.isoformat(), b.isoformat()))}
    ndays = len(days) or 1
    # 一「餐」= 同一天同一个餐次, 里面可以有好几样菜; 每天几餐按餐次数
    for dd in days.values():
        dd['meals'] = 0
    for e in rows:
        days[e['date']].setdefault('_slots', set()).add(e['slot'])
    for dd in days.values():
        dd['meals'] = len(dd.pop('_slots', set()))
    return {'range': rng, 'label': label, 'from': a.isoformat(), 'to': b.isoformat(), 'entries': rows, 'count': len(rows),
            'groups': group_meals(rows), 'meals': sum(dd['meals'] for dd in days.values()),
            'kcal_total': round(total), 'kcal_known': known, 'kcal_avg': round(total / ndays) if rows else 0, 'days': days,
            'by_cuisine': cuis, 'by_rating': rate, 'by_slot': {k: round(v) for k, v in slots.items()},
            'target': tg['kcal'], 'scores': scores, 'cats': _log_cats(c), 'slots': SLOTS, 'ratings': RATINGS}


def group_meals(rows: list[dict]) -> list[dict]:
    """按 (日期, 餐次) 把菜归成一餐一餐的, 按这餐第一样的时间排。"""
    groups: dict[tuple, dict] = {}
    for e in rows:
        g = groups.setdefault((e['date'], e['slot']), {'date': e['date'], 'slot': e['slot'], 'time': (e['dt'] or '')[11:16], 'kcal': 0.0, 'unknown': 0, 'items': []})
        g['items'].append(e)
        if e['kcal'] is not None:
            g['kcal'] += e['kcal']
        else:
            g['unknown'] += 1
        if (e['dt'] or '')[11:16] < g['time']:
            g['time'] = (e['dt'] or '')[11:16]
    out = sorted(groups.values(), key=lambda g: (g['date'], g['time']))
    for g in out:
        g['kcal'] = round(g['kcal'])
    return out


def _group_line(g: dict, with_detail: bool = True) -> str:
    """一餐压成一行: 午餐 12:10 · 番茄炒蛋 一份 约210 好吃 · 米饭 150g 约175 (小计约 385 kcal)"""
    bits = []
    for e in g['items']:
        b = e['name']
        if e['portion']:
            b += ' ' + e['portion']
        if with_detail:
            b += (f" 约{_kc(e['kcal'])}" if e['kcal'] is not None else ' 热量没算')
            if e['rating']:
                b += ' ' + e['rating']
            if e['note']:
                b += ' 「' + e['note'] + '」'
        bits.append(b)
    sub = f"小计约 {g['kcal']} kcal" + (f", {g['unknown']} 样没算" if g['unknown'] else '')
    return f"{g['slot']} {g['time']} · " + ' · '.join(bits) + f" ({sub})"


def meal_card(d: date) -> str:
    """一键发给 AI 的那张卡: 当天每一餐 + 合计 vs 目标。"""
    s = summarize('day', d)
    if not s['entries']:
        return f"🍽 {s['label']} 还什么都没记。"
    lines = [f"🍽 饮食卡 · {s['label']} · {s['meals']} 餐 {s['count']} 样"]
    for g in s['groups']:
        lines.append(_group_line(g))
    tail = f"合计约 {s['kcal_total']} kcal"
    if s['kcal_known'] < s['count']:
        tail += f"({s['count'] - s['kcal_known']} 样没算热量)"
    if s['target']:
        tail += f" / 目标 {s['target']} kcal 左右 ({round(100 * s['kcal_total'] / s['target'])}%)"
    lines.append(tail)
    lines.append('给我今天的饮食打个分吧, 顺便说说哪里可以吃得更好 ♡')
    return '\n'.join(lines)


def today_line() -> str:
    """今天记过吃的才带一行; 凌晨聊天把昨天的也带上。"""
    try:
        now = datetime.now()
        s = summarize('day', now.date())
        parts = []
        def _day_bits(ss: dict) -> str:
            return ' / '.join(f"{g['slot']}({'·'.join(e['name'] for e in g['items'][:8])} {g['kcal']})" for g in ss['groups'][:6])
        if s['count']:
            head = f"小食谱·今天已吃 {s['meals']} 餐 {s['count']} 样 约 {s['kcal_total']} kcal" + (f"/目标 {s['target']} 左右" if s['target'] else '') + ': '
            parts.append(head + _day_bits(s))
        if now.hour < 6:
            y = summarize('day', now.date() - timedelta(days=1))
            if y['count']:
                parts.append(f"昨天吃了 {y['meals']} 餐 约 {y['kcal_total']} kcal: " + _day_bits(y))
        if not parts:
            return ''
        return '; '.join(parts) + '。用户让你记吃的就用 food 工具 log_add, 记之前先看一眼别重复'
    except Exception:
        return ''


# ---------- 接口 ----------
@router.get('/api/food/recipes', dependencies=[Depends(_require_auth)])
def api_recipes(q: str = '', cuisine: str = '', region: str = '', kind: str = '', ing: int = 0, page: int = 0, limit: int = 30) -> dict:
    return search_recipes(q, cuisine, region, kind, bool(ing), page, limit)


@router.get('/api/food/recipe/{rid}', dependencies=[Depends(_require_auth)])
def api_recipe(rid: int) -> dict:
    r = get_recipe(rid)
    if not r:
        raise HTTPException(404, '没有这道菜')
    f = _fdb()
    r['fav'] = bool(f.execute('SELECT 1 FROM favorites WHERE recipe_id=?', (rid,)).fetchone())
    return r


@router.get('/api/food/random', dependencies=[Depends(_require_auth)])
def api_random(cuisine: str = '', kind: str = '', n: int = 8, q: str = '') -> dict:
    return {'items': random_recipes(cuisine, kind, n, (q or '').strip()[:20])}


@router.get('/api/food/image', dependencies=[Depends(_require_auth)])
async def api_image(id: int) -> dict:
    return await ensure_image(id)


@router.get('/api/food/meta', dependencies=[Depends(_require_auth)])
def api_meta() -> dict:
    c = _rdb()
    cu = {r['cuisine']: r['n'] for r in c.execute('SELECT cuisine, COUNT(*) n FROM recipes GROUP BY cuisine')}
    rg = {r['region']: r['n'] for r in c.execute("SELECT region, COUNT(*) n FROM recipes WHERE region!='' GROUP BY region ORDER BY n DESC")}
    f = _fdb()
    return {'cuisines': [x for x in CUISINES if x in cu], 'cuisine_counts': cu, 'regions': rg,
            'kinds': ['早餐', '主食', '汤羹', '素菜', '荤菜', '水产', '甜品', '烘焙', '饮品', '小吃', '凉菜', '减脂', '快手', '宴客', '辅食'],
            'log_cats': _log_cats(f), 'slots': SLOTS, 'ratings': RATINGS, 'target': targets(f),
            'fav_count': f.execute('SELECT COUNT(*) FROM favorites').fetchone()[0]}


@router.post('/api/food/fav/{rid}', dependencies=[Depends(_require_auth)])
def api_fav(rid: int, body: dict | None = None) -> dict:
    f = _fdb()
    on = bool((body or {}).get('on', True))
    if on:
        f.execute('INSERT OR IGNORE INTO favorites(recipe_id,created) VALUES(?,?)', (rid, datetime.now().isoformat(timespec='seconds')))
    else:
        f.execute('DELETE FROM favorites WHERE recipe_id=?', (rid,))
    f.commit()
    return {'ok': True, 'fav': on}


@router.get('/api/food/favs', dependencies=[Depends(_require_auth)])
def api_favs() -> dict:
    f = _fdb()
    ids = [r['recipe_id'] for r in f.execute('SELECT recipe_id FROM favorites ORDER BY created DESC LIMIT 200')]
    if not ids:
        return {'items': []}
    c = _rdb()
    rows = {r['id']: _card(r) for r in c.execute(f'SELECT {CARD_COLS} FROM recipes WHERE id IN ({",".join("?" * len(ids))})', ids)}
    return {'items': [rows[i] for i in ids if i in rows]}


@router.get('/api/food/kcal', dependencies=[Depends(_require_auth)])
def api_kcal(q: str = '') -> dict:
    return {'items': lookup_kcal(q)}


@router.post('/api/food/estimate', dependencies=[Depends(_require_auth)])
async def api_estimate(body: dict) -> dict:
    return await estimate_kcal(str(body.get('name') or ''), str(body.get('portion') or ''))


@router.get('/api/food/meals', dependencies=[Depends(_require_auth)])
def api_meals(range: str = 'day', date: str = '') -> dict:
    return summarize(range if range in ('day', 'week', 'month', 'year') else 'day', _parse_date(date))


@router.get('/api/food/card', dependencies=[Depends(_require_auth)])
def api_card(date: str = '') -> dict:
    return {'text': meal_card(_parse_date(date))}


@router.post('/api/food/meal', dependencies=[Depends(_require_auth)])
def api_meal_add(body: dict) -> dict:
    e = add_meal(body.get('name') or '', body.get('cuisine') or '', body.get('slot') or '', body.get('rating') or '', body.get('note') or '',
                 body.get('portion') or '', body.get('kcal'), body.get('kcal_src') or '', body.get('date') or '', body.get('time') or '',
                 body.get('recipe_id'), 'user')
    return {'ok': True, 'entry': e}


@router.patch('/api/food/meal/{mid}', dependencies=[Depends(_require_auth)])
def api_meal_edit(mid: str, body: dict) -> dict:
    c = _fdb()
    r = c.execute('SELECT * FROM meals WHERE id=?', (mid,)).fetchone()
    if not r:
        raise HTTPException(404, '没有这一餐')
    cur = _row(r)
    name = (body.get('name') or cur['name']).strip()[:60]
    slot = body.get('slot') if body.get('slot') in SLOTS else cur['slot']
    rating = body.get('rating') if body.get('rating') in RATINGS or body.get('rating') == '' else cur['rating']
    kcal = body.get('kcal', cur['kcal'])
    try:
        kcal = float(kcal) if kcal not in (None, '') else None
    except Exception:
        kcal = cur['kcal']
    kcal_src = body.get('kcal_src') or (cur['kcal_src'] if kcal == cur['kcal'] else 'manual')
    d = _parse_date(body.get('date') or cur['date'])
    dt = cur['dt']
    if body.get('time') and re.match(r'^\d{1,2}:\d{2}$', str(body['time']).strip()):
        dt = d.isoformat() + 'T' + str(body['time']).strip().zfill(5)
    elif d.isoformat() != cur['date']:
        dt = d.isoformat() + 'T' + (cur['dt'] or 'T12:00')[11:16]
    c.execute('UPDATE meals SET name=?,slot=?,cuisine=?,rating=?,note=?,portion=?,kcal=?,kcal_src=?,d=?,dt=? WHERE id=?',
              (name, slot, (body.get('cuisine') if body.get('cuisine') is not None else cur['cuisine'])[:12], rating,
               (body.get('note') if body.get('note') is not None else cur['note'])[:300],
               (body.get('portion') if body.get('portion') is not None else cur['portion'])[:30], kcal, kcal_src, d.isoformat(), dt, mid))
    c.commit()
    return {'ok': True, 'entry': _row(c.execute('SELECT * FROM meals WHERE id=?', (mid,)).fetchone())}


@router.delete('/api/food/meal/{mid}', dependencies=[Depends(_require_auth)])
def api_meal_del(mid: str) -> dict:
    c = _fdb()
    c.execute('DELETE FROM meals WHERE id=?', (mid,)); c.commit()
    return {'ok': True}


@router.get('/api/food/settings', dependencies=[Depends(_require_auth)])
def api_settings() -> dict:
    c = _fdb()
    return {'target': targets(c), 'cats': _log_cats(c)}


@router.post('/api/food/settings', dependencies=[Depends(_require_auth)])
def api_settings_save(body: dict) -> dict:
    c = _fdb()
    if isinstance(body.get('target'), dict):
        t = body['target']
        keep = {k: t.get(k) for k in ('method', 'kcal', 'height', 'weight', 'age', 'sex', 'activity', 'goal') if t.get(k) not in (None, '')}
        _set_setting(c, 'target', keep)
    if isinstance(body.get('cats'), list):
        cats = [str(x).strip()[:12] for x in body['cats'] if str(x).strip()]
        _set_setting(c, 'cuisines', list(dict.fromkeys(cats))[:20] or DEFAULT_LOG_CATS)
    c.commit()
    return {'ok': True, 'target': targets(c), 'cats': _log_cats(c)}


@router.get('/food-img/{sub}/{fn}')
def food_img(sub: str, fn: str):
    if sub not in ('htc', 'wiki') or not re.fullmatch(r'[A-Za-z0-9_.-]{3,80}', fn):
        raise HTTPException(404, 'no')
    p = IMG_DIR / sub / fn
    if not p.is_file():
        raise HTTPException(404, 'no')
    return FileResponse(p, headers={'Cache-Control': 'public, max-age=2592000'})


# ---------- 给 AI 的工具 ----------
TOOL_DEF = {"type": "function", "function": {
    "name": "food",
    "description": ("小食谱(菜谱库 + 用户的饮食记录)。菜谱库来自下厨房公开语料和程序员做饭指南, cuisine 分 中餐/日料/韩餐/西餐/东南亚/南亚中东。"
                    "action=search 找菜谱: q 关键词(菜名或标签; ing=true 时按食材找, 比如用户说'冰箱只有鸡蛋和西红柿'), 可加 cuisine/kind(早餐/主食/汤羹/素菜/荤菜/水产/甜品/烘焙/饮品/小吃/凉菜/减脂/快手/宴客/辅食), 返回 id 和菜名; "
                    "recipe 看一道菜的完整做法(id); random 随机几道(cuisine 可选, 用户纠结吃什么时用, 时令菜就按当天节气自己挑 q 里的食材再 random); "
                    "log_view 看用户吃了什么: range day/week/month, date 可选, 按餐次列出(一餐=同一餐次里的几样菜), 带热量合计和目标; "
                    "log_add 帮用户记一样吃的: name 一样菜(必填, 一餐有好几样就调几次, 同一个 slot 自动归到同一餐), cuisine 用「小食谱分类」里的, slot 早餐/午餐/晚餐/加餐/夜宵(不填按时间猜), rating 好吃/一般/难吃, note 吃后感, portion 份量, kcal 热量(用户没说就按食物成分表口径估一个最可能的数, 按一人份, 别往高里估; 估不出就不填), date 补记过去的日子; "
                    "kcal 查热量(name): 先查食物成分表(每100克)和之前估过的, 没有就自己估; target 看用户的每日热量目标; "
                    "score 给某天的饮食打分(date, score 1-10, comment 一两句): 用户发饮食卡来让你打分时, 回复里说完后调一次把分记到日历上。"
                    "打分和建议看的是均衡、多样、有没有好好吃饭, 目标是个区间不是上限, 绝不是吃得越少分越高; 明显没吃够要提醒用户多吃点。"
                    "用户自己也在页面记, 记之前先 log_view 当天别重复; 删改让用户去页面点那一餐。"),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["search", "recipe", "random", "log_view", "log_add", "kcal", "target", "score"]},
        "q": {"type": "string"}, "ing": {"type": "boolean"}, "cuisine": {"type": "string"}, "kind": {"type": "string"},
        "id": {"type": "integer"}, "n": {"type": "integer"},
        "range": {"type": "string", "enum": ["day", "week", "month"]}, "date": {"type": "string", "description": "YYYY-MM-DD"},
        "name": {"type": "string"}, "slot": {"type": "string"}, "rating": {"type": "string"}, "note": {"type": "string"},
        "portion": {"type": "string"}, "kcal": {"type": "number"}, "score": {"type": "integer"}, "comment": {"type": "string"},
    }, "required": ["action"]}}}


def _card_line(x: dict) -> str:
    t = f"#{x['id']} {x['name']}"
    if x['dish'] and x['dish'] != x['name']:
        t += f"({x['dish']})"
    extra = [x['cuisine'] or '', x['region'] or ''] + [k for k in x['kinds'][:2]]
    extra = [e for e in extra if e]
    if extra:
        t += ' · ' + '/'.join(extra)
    if x['image']:
        t += ' 📷'
    return t


async def tool_dispatch(args: dict) -> str:
    action = str(args.get('action') or 'search')
    try:
        if action == 'search':
            r = search_recipes(args.get('q') or '', args.get('cuisine') or '', '', args.get('kind') or '', bool(args.get('ing')), 0, min(int(args.get('n') or 12), 20))
            if not r['items']:
                return '没找到, 换个词试试(菜名/食材/标签都行)。'
            return f"找到这些(带 📷 的有图), 要做法用 recipe 带 id:\n" + '\n'.join(_card_line(x) for x in r['items']) + ('\n…还有更多' if r['more'] else '')
        if action == 'recipe':
            rid = int(args.get('id') or 0)
            r = get_recipe(rid)
            if not r:
                return '没有这个 id 的菜。'
            ings = '、'.join(r['ingredients'][:30])
            steps = '\n'.join(f"{i + 1}. {s}" for i, s in enumerate(r['steps'][:25]))
            head = f"《{r['name']}》 {r['cuisine']}{('/' + r['region']) if r['region'] else ''} 来源 {'程序员做饭指南' if r['src'] == 'htc' else '下厨房'}"
            return head + (f"\n{r['descr'][:300]}" if r['descr'] else '') + f"\n食材: {ings}\n步骤:\n{steps}"
        if action == 'random':
            items = random_recipes(args.get('cuisine') or '', args.get('kind') or '', min(int(args.get('n') or 5), 10), (args.get('q') or '')[:20])
            if not items:
                return '这个分类里没抽到, 换个分类。'
            return '抽到这几道:\n' + '\n'.join(_card_line(x) for x in items)
        if action == 'log_view':
            rng = args.get('range') if args.get('range') in ('day', 'week', 'month') else 'day'
            s = summarize(rng, _parse_date(args.get('date') or ''))
            if not s['entries']:
                return f"{s['label']} 没有记录。" + (f" 目标 {s['target']} kcal/天。" if s['target'] else ' 用户还没设热量目标。')
            lines = [f"{s['label']}: {s['meals']} 餐 {s['count']} 样, 合计约 {s['kcal_total']} kcal" + (f"({s['count'] - s['kcal_known']} 样没算)" if s['kcal_known'] < s['count'] else '')
                     + (f", 日均 {s['kcal_avg']}" if rng != 'day' else '') + (f", 目标 {s['target']}/天左右" if s['target'] else '')]
            for g in s['groups'][:30]:
                lines.append((g['date'][5:] + ' ' if rng != 'day' else '') + _group_line(g)
                             + (' (有你记的)' if any(e['source'] != 'user' for e in g['items']) else ''))
            if s['by_cuisine']:
                lines.append('分类: ' + ' '.join(f"{k}{v}" for k, v in sorted(s['by_cuisine'].items(), key=lambda x: -x[1])))
            if s['scores']:
                lines.append('已打分: ' + ' '.join(f"{d[5:]}={v['score']}" for d, v in sorted(s['scores'].items())))
            return '\n'.join(lines)
        if action == 'log_add':
            try:
                base = _parse_date(args.get('date') or '')
            except HTTPException as e:
                return f'没记上: {e.detail}'
            s = summarize('day', base)
            nm = (args.get('name') or '').strip()
            for e in s['entries']:
                if e['name'] == nm and (not args.get('slot') or e['slot'] == args.get('slot')):
                    return f"那天已经记过「{nm}」({e['slot']}), 没有重复记。要改的话让用户去页面点那一餐。"
            e = add_meal(nm, args.get('cuisine') or '', args.get('slot') or '', args.get('rating') or '', args.get('note') or '', args.get('portion') or '',
                         args.get('kcal'), 'ai' if args.get('kcal') not in (None, '') else '', base.isoformat(), '', None, 'ai')
            s2 = summarize('day', base)
            return (f"记上了: {e['date']} {e['slot']} {e['name']}" + (f" 约{_kc(e['kcal'])}kcal" if e['kcal'] is not None else '')
                    + f"。那天现在 {s2['meals']} 餐 {s2['count']} 样 合计约 {s2['kcal_total']} kcal" + (f"/目标 {s2['target']} 左右" if s2['target'] else '') + '。')
        if action == 'kcal':
            items = lookup_kcal(args.get('name') or args.get('q') or '')
            if not items:
                return '表里没有, 你按常见份量估一个吧。'
            return '\n'.join(f"{x['name']} {x['portion']}: {_kc(x['kcal'])} kcal" + (f" (蛋白{x['protein']}g 脂肪{x['fat']}g 碳水{x['carb']}g)" if x['protein'] is not None else '') + (' ·之前估过' if x['src'] == 'ai' else '') for x in items[:10])
        if action == 'target':
            t = targets()
            if not t['kcal']:
                return '用户还没设每日热量目标(小食谱页面右上角可以设)。'
            return f"每日目标 {t['kcal']} kcal" + (f" (基础代谢 {t['bmr']}, 维持 {t['tdee']}, 按身高体重算的)" if t['method'] == 'calc' else ' (用户自己定的数)')
        if action == 'score':
            try:
                d = _parse_date(args.get('date') or '')
                sc = max(1, min(10, int(args.get('score'))))
            except Exception:
                return '要 date 和 1-10 的 score。'
            c = _fdb()
            c.execute('INSERT INTO day_scores(d,score,comment,by,created) VALUES(?,?,?,?,?) ON CONFLICT(d) DO UPDATE SET score=excluded.score, comment=excluded.comment, by=excluded.by, created=excluded.created',
                      (d.isoformat(), sc, (args.get('comment') or '').strip()[:120], 'ai', datetime.now().isoformat(timespec='seconds')))
            c.commit()
            return f"{d.isoformat()} 记为 {sc} 分, 日历上能看到。"
        return '不认识这个 action。'
    except HTTPException as e:
        return f'出错了: {e.detail}'
    except Exception as e:
        return f'出错了: {type(e).__name__}: {e}'


# ---------- 给不是 Python 的聊天后端用: 工具走 HTTP ----------
@router.get('/api/food/context', dependencies=[Depends(_require_auth)])
def api_context() -> dict:
    """工具定义 + 今天吃了什么那一行。聊天后端每轮拿一次, 一个塞进 tools, 一个塞进系统提示。"""
    return {'tool': TOOL_DEF, 'today_line': today_line()}


@router.post('/api/food/tool', dependencies=[Depends(_require_auth)])
async def api_tool(body: dict) -> dict:
    """模型调 food 工具时, 把它给的 arguments 原样 POST 过来, 拿回要喂给模型的那段文字。"""
    return {'text': await tool_dispatch(body if isinstance(body, dict) else {})}
