# -*- coding: utf-8 -*-
"""recipes.db 的表结构, 入库脚本共用(和 app/food.py 里的 RECIPE_SCHEMA 一致)。"""
import sqlite3

SCHEMA = '''
PRAGMA journal_mode=WAL;
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


def open_recipes_db(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con
