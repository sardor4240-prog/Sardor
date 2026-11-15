"""
Tap Coin — бэкенд для Telegram Mini App.
Один процесс: API (FastAPI) + бот (aiogram, long polling).

Переменные окружения:
  BOT_TOKEN       токен от @BotFather
  BOT_USERNAME    username бота без @
  WEBAPP_URL      https-ссылка на ваш tap-coin.html
  APP_SHORT_NAME  (необязательно) short name мини-аппа из BotFather /newapp,
                  тогда реф-ссылки будут вида t.me/bot/app?startapp=ref_ID
  ALLOWED_ORIGIN  (необязательно) домен фронтенда для CORS, по умолчанию *
  DB_PATH         (необязательно) путь к файлу базы, по умолчанию game.db

Запуск:  uvicorn server:app --host 0.0.0.0 --port 8000
"""
import asyncio
import hashlib
import hmac
import json
import math
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           WebAppInfo)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ["BOT_USERNAME"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
APP_SHORT_NAME = os.getenv("APP_SHORT_NAME", "")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")
DB_PATH = os.getenv("DB_PATH", "game.db")

INVITER_BONUS = 5000      # пригласившему за каждого друга
INVITEE_BONUS = 2500      # новичку по реф-ссылке
OFFLINE_CAP = 3 * 3600    # максимум офлайн-дохода (сек)
MAX_TPS = 30              # потолок тапов в секунду (мультитач)
TOLERANCE = 500           # запас на погрешности
INIT_DATA_TTL = 2 * 86400 # срок годности initData

# Те же улучшения, что и в клиенте: id -> (base, mult, type, val)
UPGRADES = {
    "tap":   (50,   1.6,  "tap",   1),
    "cap":   (100,  1.7,  "cap",   500),
    "regen": (150,  1.8,  "regen", 1),
    "miner": (300,  1.5,  "pph",   100),
    "farm":  (1200, 1.55, "pph",   500),
    "bank":  (5000, 1.6,  "pph",   2500),
}
MAX_LEVEL = 200


# ---------- игровые формулы ----------
def sanitize_lvls(raw: dict) -> dict:
    out = {}
    for k in UPGRADES:
        try:
            v = int(raw.get(k, 0))
        except (TypeError, ValueError):
            v = 0
        out[k] = max(0, min(MAX_LEVEL, v))
    return out


def calc(lvls: dict, kind: str) -> int:
    return sum(UPGRADES[k][3] * lvls.get(k, 0) for k in UPGRADES if UPGRADES[k][2] == kind)


def tap_power(l): return 1 + calc(l, "tap")
def max_energy(l): return 1000 + calc(l, "cap")
def regen(l): return 1 + calc(l, "regen")
def pph(l): return calc(l, "pph")


def spent(lvls: dict) -> float:
    """Сколько монет суммарно потрачено на текущие уровни."""
    total = 0
    for k, (base, mult, _, _) in UPGRADES.items():
        for i in range(lvls.get(k, 0)):
            total += math.floor(base * mult ** i)
    return total


# ---------- база ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.Lock()
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  name TEXT,
  coins REAL DEFAULT 0,
  energy REAL DEFAULT 1000,
  lvls TEXT DEFAULT '{}',
  worth REAL DEFAULT 0,          -- монеты + потраченное: по нему рейтинг
  last_ts REAL,
  referrer_id INTEGER,
  pending_bonus REAL DEFAULT 0,  -- реф-бонусы, ещё не забранные клиентом
  created REAL
);
CREATE INDEX IF NOT EXISTS idx_worth ON users(worth DESC);
CREATE INDEX IF NOT EXISTS idx_ref ON users(referrer_id);
""")
db.commit()


def display_name(u: dict) -> str:
    name = (u.get("first_name") or u.get("username") or "Игрок").strip()
    return name[:32] or "Игрок"


def parse_ref(param):
    if param and param.startswith("ref_") and param[4:].isdigit():
        return int(param[4:])
    return None


def get_or_create(uid: int, name: str, ref_id=None):
    now = time.time()
    with lock:
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if row:
            if row["name"] != name:
                db.execute("UPDATE users SET name=? WHERE id=?", (name, uid))
                db.commit()
            return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

        ref = None
        if ref_id and ref_id != uid and db.execute(
                "SELECT 1 FROM users WHERE id=?", (ref_id,)).fetchone():
            ref = ref_id

        db.execute(
            "INSERT INTO users(id,name,last_ts,referrer_id,pending_bonus,created) VALUES(?,?,?,?,?,?)",
            (uid, name, now, ref, INVITEE_BONUS if ref else 0, now))
        if ref:
            db.execute("UPDATE users SET pending_bonus=pending_bonus+? WHERE id=?",
                       (INVITER_BONUS, ref))
        db.commit()
        return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


# ---------- проверка подписи Telegram ----------
def check_init_data(init_data: str):
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        raise HTTPException(401, "bad initData")
    their_hash = pairs.pop("hash", None)
    if not their_hash:
        raise HTTPException(401, "no hash")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    mine = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mine, their_hash):
        raise HTTPException(401, "bad signature")
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > INIT_DATA_TTL:
            raise HTTPException(401, "initData expired")
        user = json.loads(pairs["user"])
        user["id"] = int(user["id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, "bad user")
    return user, pairs.get("start_param")


# ---------- модели запросов ----------
class AuthReq(BaseModel):
    initData: str


class SyncReq(BaseModel):
    initData: str
    coins: float
    energy: float
    lvls: dict


def ref_link(uid: int) -> str:
    if APP_SHORT_NAME:
        return f"https://t.me/{BOT_USERNAME}/{APP_SHORT_NAME}?startapp=ref_{uid}"
    return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"


# ---------- бот ----------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def on_start(m: Message, command: CommandObject):
    name = display_name({"first_name": m.from_user.first_name, "username": m.from_user.username})
    get_or_create(m.from_user.id, name, parse_ref(command.args))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🪙 Играть", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await m.answer("Привет! Тапай монету, качай прибыль и поднимайся в рейтинге.\n"
                   "Приглашай друзей — получай бонусы 🎁", reply_markup=kb)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    yield
    task.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN] if ALLOWED_ORIGIN != "*" else ["*"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)


# ---------- API ----------
@app.post("/api/auth")
def api_auth(body: AuthReq):
    user, start_param = check_init_data(body.initData)
    row = get_or_create(user["id"], display_name(user), parse_ref(start_param))
    now = time.time()
    lv = sanitize_lvls(json.loads(row["lvls"]))
    elapsed = max(0.0, now - row["last_ts"])

    gain = pph(lv) * min(elapsed, OFFLINE_CAP) / 3600
    claimed = row["pending_bonus"]
    coins = row["coins"] + gain + claimed
    worth = row["worth"] + gain + claimed
    energy = min(max_energy(lv), row["energy"] + regen(lv) * elapsed)

    with lock:
        db.execute("UPDATE users SET coins=?, worth=?, energy=?, pending_bonus=0, last_ts=? WHERE id=?",
                   (coins, worth, energy, now, user["id"]))
        db.commit()
    return {"id": user["id"], "coins": coins, "energy": energy, "lvls": lv,
            "offline_gain": gain, "claimed": claimed}


@app.post("/api/sync")
def api_sync(body: SyncReq):
    user, _ = check_init_data(body.initData)
    if not (math.isfinite(body.coins) and math.isfinite(body.energy)) or body.coins < 0:
        raise HTTPException(400, "bad numbers")

    row = get_or_create(user["id"], display_name(user))
    lv_old = sanitize_lvls(json.loads(row["lvls"]))
    lv_new = sanitize_lvls(body.lvls)
    for k in lv_old:                       # уровни не могут уменьшаться
        lv_new[k] = max(lv_new[k], lv_old[k])

    now = time.time()
    elapsed = max(0.0, now - row["last_ts"])

    # Сколько максимум можно было заработать с прошлой синхронизации:
    # тапы ограничены и скоростью, и энергией (за тап тратится ровно столько же, сколько даётся),
    # плюс пассивный доход.
    tap_gain = min(MAX_TPS * elapsed * tap_power(lv_new),
                   row["energy"] + regen(lv_new) * elapsed)
    allowed = tap_gain + pph(lv_new) * min(elapsed, OFFLINE_CAP) / 3600 + TOLERANCE

    worth_new = body.coins + spent(lv_new)
    if worth_new - row["worth"] > allowed:
        # подозрительный скачок — откатываем клиента к состоянию сервера
        return {"capped": True, "coins": row["coins"], "energy": row["energy"], "lvls": lv_old}

    claimed = row["pending_bonus"]
    energy = max(0.0, min(max_energy(lv_new), body.energy))
    with lock:
        db.execute(
            "UPDATE users SET coins=?, worth=?, energy=?, lvls=?, pending_bonus=0, last_ts=? WHERE id=?",
            (body.coins + claimed, worth_new + claimed, energy, json.dumps(lv_new), now, user["id"]))
        db.commit()
    return {"capped": False, "claimed": claimed}


@app.post("/api/leaderboard")
def api_leaderboard(body: AuthReq):
    user, _ = check_init_data(body.initData)
    get_or_create(user["id"], display_name(user))
    with lock:
        top = db.execute("SELECT id,name,worth FROM users ORDER BY worth DESC LIMIT 50").fetchall()
        me = db.execute("SELECT worth FROM users WHERE id=?", (user["id"],)).fetchone()
        rank = db.execute("SELECT COUNT(*)+1 FROM users WHERE worth>?", (me["worth"],)).fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return {
        "me": user["id"], "my_rank": rank, "my_score": me["worth"], "total": total,
        "top": [{"id": r["id"], "name": r["name"], "score": r["worth"]} for r in top],
    }


@app.post("/api/friends")
def api_friends(body: AuthReq):
    user, _ = check_init_data(body.initData)
    get_or_create(user["id"], display_name(user))
    with lock:
        rows = db.execute(
            "SELECT name,worth FROM users WHERE referrer_id=? ORDER BY worth DESC LIMIT 100",
            (user["id"],)).fetchall()
        count = db.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (user["id"],)).fetchone()[0]
    return {
        "link": ref_link(user["id"]), "count": count, "earned": count * INVITER_BONUS,
        "inviter_bonus": INVITER_BONUS, "invitee_bonus": INVITEE_BONUS,
        "friends": [{"name": r["name"], "score": r["worth"]} for r in rows],
    }


@app.get("/health")
def health():
    return {"ok": True}
