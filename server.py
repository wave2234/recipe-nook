"""单独跑小食谱: uvicorn server:app --port 8790 , 然后打开 http://127.0.0.1:8790/"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_env(p: Path) -> None:
    """读同目录的 .env (KEY=VALUE 一行一个), 不覆盖已经设了的环境变量。省得多装一个 python-dotenv。"""
    if not p.is_file():
        return
    for line in p.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ROOT / '.env')

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from app import food  # noqa: E402

app = FastAPI(title='小食谱 recipe nook', docs_url=None, redoc_url=None)
app.include_router(food.router)


@app.get('/')
def index():
    return FileResponse(ROOT / 'static' / 'food.html', headers={'Cache-Control': 'no-cache'})
