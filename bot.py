import asyncio
import json
import logging
import os
import sqlite3
import statistics
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://api.tgmrkt.io/api/v1"
DB_PATH = os.getenv("DB_PATH", "mrkt_bot.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MRKT_TOKEN = os.getenv("MRKT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "45"))
DEFAULT_SAMPLE_LIMIT = int(os.getenv("DEFAULT_SAMPLE_LIMIT", "20"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            is_enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS filters (
            user_id INTEGER PRIMARY KEY,
            collections TEXT NOT NULL DEFAULT '[]',
            models TEXT NOT NULL DEFAULT '[]',
            max_price REAL,
            min_price REAL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_listings (
            listing_key TEXT PRIMARY KEY,
            seen_at INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


@dataclass
class UserFilter:
    user_id: int
    collections: List[str]
    models: List[str]
    min_price: Optional[float]
    max_price: Optional[float]
    is_enabled: bool


def ensure_user(user_id: int) -> None:
    now = int(time.time())
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, created_at, updated_at) VALUES (?, ?, ?)",
        (user_id, now, now),
    )
    cur.execute(
        "INSERT OR IGNORE INTO filters (user_id, updated_at) VALUES (?, ?)",
        (user_id, now),
    )
    cur.execute("UPDATE users SET updated_at = ? WHERE user_id = ?", (now, user_id))
    conn.commit()
    conn.close()



def set_enabled(user_id: int, enabled: bool) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_enabled = ?, updated_at = ? WHERE user_id = ?", (1 if enabled else 0, int(time.time()), user_id))
    conn.commit()
    conn.close()



def update_filter(user_id: int, *, collections=None, models=None, min_price=None, max_price=None) -> None:
    conn = db_connect()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM filters WHERE user_id = ?", (user_id,)).fetchone()
    current = dict(row) if row else {}
    new_collections = json.dumps(collections if collections is not None else json.loads(current.get("collections", "[]")))
    new_models = json.dumps(models if models is not None else json.loads(current.get("models", "[]")))
    new_min = min_price if min_price is not None or min_price is None else current.get("min_price")
    new_max = max_price if max_price is not None or max_price is None else current.get("max_price")
    cur.execute(
        """
        UPDATE filters
        SET collections = ?, models = ?, min_price = ?, max_price = ?, updated_at = ?
        WHERE user_id = ?
        """,
        (new_collections, new_models, new_min, new_max, int(time.time()), user_id),
    )
    conn.commit()
    conn.close()



def get_filter(user_id: int) -> UserFilter:
    conn = db_connect()
    cur = conn.cursor()
    row_user = cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    row_filter = cur.execute("SELECT * FROM filters WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return UserFilter(
        user_id=user_id,
        collections=json.loads(row_filter["collections"]),
        models=json.loads(row_filter["models"]),
        min_price=row_filter["min_price"],
        max_price=row_filter["max_price"],
        is_enabled=bool(row_user["is_enabled"]),
    )



def get_enabled_user_ids() -> List[int]:
    conn = db_connect()
    cur = conn.cursor()
    rows = cur.execute("SELECT user_id FROM users WHERE is_enabled = 1").fetchall()
    conn.close()
    return [r[0] for r in rows]



def remember_listing(listing_key: str) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO seen_listings (listing_key, seen_at) VALUES (?, ?)", (listing_key, int(time.time())))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()



def build_payload(user_filter: UserFilter, cursor: str = "") -> Dict[str, Any]:
    return {
        "collectionNames": user_filter.collections,
        "modelNames": user_filter.models,
        "backdropNames": [],
        "symbolNames": [],
        "ordering": None,
        "lowToHigh": True,
        "maxPrice": user_filter.max_price,
        "minPrice": user_filter.min_price,
        "mintable": None,
        "number": None,
        "count": DEFAULT_SAMPLE_LIMIT,
        "cursor": cursor,
        "query": None,
        "promotedFirst": False,
    }



def mrkt_headers() -> Dict[str, str]:
    return {
        "Authorization": MRKT_TOKEN,
        "Referer": "https://cdn.tgmrkt.io/",
        "Content-Type": "application/json",
    }



def fetch_gifts(user_filter: UserFilter) -> List[Dict[str, Any]]:
    payload = build_payload(user_filter)
    response = requests.post(
        f"{API_BASE}/gifts/saling",
        headers=mrkt_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data.get("gifts", [])



def extract_price(item: Dict[str, Any]) -> Optional[float]:
    for key in ("price", "salePrice", "amount", "tonPrice"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.replace(",", ".").strip())
            except Exception:
                pass
    return None



def extract_name(item: Dict[str, Any]) -> str:
    for key in ("collectionName", "giftName", "name", "title"):
        val = item.get(key)
        if val:
            return str(val)
    return "Unknown"



def extract_model(item: Dict[str, Any]) -> str:
    for key in ("modelName", "model", "variant"):
        val = item.get(key)
        if val:
            return str(val)
    return "—"



def extract_link(item: Dict[str, Any]) -> str:
    for key in ("url", "shareUrl", "link"):
        val = item.get(key)
        if val:
            return str(val)
    slug = item.get("slug") or item.get("id") or item.get("giftId")
    return f"https://t.me/mrkt" if not slug else f"https://t.me/mrkt"



def extract_listing_key(item: Dict[str, Any]) -> str:
    for key in ("id", "giftId", "listingId", "slug"):
        if item.get(key) is not None:
            return f"{key}:{item.get(key)}"
    return json.dumps(item, sort_keys=True, ensure_ascii=False)



def compute_stats(gifts: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    prices = [extract_price(x) for x in gifts]
    prices = [x for x in prices if x is not None]
    if not prices:
        return {"count": 0, "min": None, "avg": None, "median": None}
    return {
        "count": len(prices),
        "min": min(prices),
        "avg": sum(prices) / len(prices),
        "median": statistics.median(prices),
    }



def format_stats_text(stats: Dict[str, Optional[float]], title: str = "Статистика") -> str:
    if not stats["count"]:
        return f"<b>{title}</b>\nЛоты не найдены."
    return (
        f"<b>{title}</b>\n"
        f"Лотов в выборке: <b>{stats['count']}</b>\n"
        f"Минимум: <b>{stats['min']:.2f} TON</b>\n"
        f"Средняя цена выставления: <b>{stats['avg']:.2f} TON</b>\n"
        f"Медиана: <b>{stats['median']:.2f} TON</b>"
    )



def pretty_filter_text(user_filter: UserFilter) -> str:
    return (
        "<b>Твои фильтры</b>\n"
        f"Подарки: <code>{', '.join(user_filter.collections) if user_filter.collections else 'все'}</code>\n"
        f"Модели: <code>{', '.join(user_filter.models) if user_filter.models else 'все'}</code>\n"
        f"Мин. цена: <code>{user_filter.min_price if user_filter.min_price is not None else 'не задана'}</code>\n"
        f"Макс. цена: <code>{user_filter.max_price if user_filter.max_price is not None else 'не задана'}</code>\n"
        f"Уведомления: <b>{'включены' if user_filter.is_enabled else 'выключены'}</b>"
    )


async def start_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="Вкл уведомления", callback_data="noop")
    text = (
        "<b>MRKT Alert Bot</b>\n\n"
        "Бот следит за новыми выставлениями на MRKT и шлёт алерты по твоим фильтрам.\n\n"
        "Команды:\n"
        "/set_collections часы, кепки\n"
        "/set_models albino, gold\n"
        "/set_price 1 20\n"
        "/stats\n"
        "/filters\n"
        "/on и /off\n"
        "/help"
    )
    await message.answer(text)


async def help_cmd(message: Message) -> None:
    await message.answer(
        "<b>Как пользоваться</b>\n"
        "1. Задай типы подарков: <code>/set_collections часы, кепки</code>\n"
        "2. При желании укажи модели: <code>/set_models gold, black</code>\n"
        "3. Ограничь цену: <code>/set_price 1 15</code>\n"
        "4. Получи статистику: <code>/stats</code>\n"
        "5. Оставь бот включённым — он будет слать новые лоты по фильтру.\n\n"
        "Важно: сейчас статистика строится по текущим выставленным лотам, а не по подтверждённой истории сделок."
    )


async def filters_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    await message.answer(pretty_filter_text(get_filter(message.from_user.id)))


async def on_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    set_enabled(message.from_user.id, True)
    await message.answer("Уведомления включены.")


async def off_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    set_enabled(message.from_user.id, False)
    await message.answer("Уведомления выключены.")


async def set_collections_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    raw = (message.text or "").replace("/set_collections", "", 1).strip()
    values = [x.strip() for x in raw.split(",") if x.strip()]
    update_filter(message.from_user.id, collections=values)
    await message.answer(f"Сохранил подарки: <code>{', '.join(values) if values else 'все'}</code>")


async def set_models_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    raw = (message.text or "").replace("/set_models", "", 1).strip()
    values = [x.strip() for x in raw.split(",") if x.strip()]
    current = get_filter(message.from_user.id)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE filters SET models = ?, updated_at = ? WHERE user_id = ?",
        (json.dumps(values), int(time.time()), message.from_user.id),
    )
    conn.commit()
    conn.close()
    await message.answer(f"Сохранил модели: <code>{', '.join(values) if values else 'все'}</code>")


async def set_price_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Формат: <code>/set_price 1 20</code>")
        return
    min_price = float(parts[1])
    max_price = float(parts[2])
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE filters SET min_price = ?, max_price = ?, updated_at = ? WHERE user_id = ?",
        (min_price, max_price, int(time.time()), message.from_user.id),
    )
    conn.commit()
    conn.close()
    await message.answer(f"Диапазон цены сохранён: <b>{min_price}</b>–<b>{max_price}</b> TON")


async def stats_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    user_filter = get_filter(message.from_user.id)
    try:
        gifts = fetch_gifts(user_filter)
    except Exception as e:
        await message.answer(f"Ошибка запроса к MRKT: <code>{e}</code>")
        return
    stats = compute_stats(gifts)
    lines = [format_stats_text(stats)]
    preview = []
    for item in gifts[:5]:
        price = extract_price(item)
        preview.append(f"• {extract_name(item)} / {extract_model(item)} — <b>{price:.2f} TON</b>" if price is not None else f"• {extract_name(item)} / {extract_model(item)}")
    if preview:
        lines.append("\n<b>Первые лоты:</b>\n" + "\n".join(preview))
    await message.answer("\n\n".join(lines))


async def monitor_loop(bot: Bot) -> None:
    await asyncio.sleep(3)
    while True:
        user_ids = get_enabled_user_ids()
        for user_id in user_ids:
            try:
                user_filter = get_filter(user_id)
                gifts = fetch_gifts(user_filter)
                new_items = []
                for item in gifts:
                    listing_key = extract_listing_key(item)
                    if remember_listing(f"{user_id}:{listing_key}"):
                        new_items.append(item)
                for item in new_items[:10]:
                    price = extract_price(item)
                    text = (
                        "<b>Новое выставление на MRKT</b>\n"
                        f"Подарок: <b>{extract_name(item)}</b>\n"
                        f"Модель: <b>{extract_model(item)}</b>\n"
                        f"Цена: <b>{price:.2f} TON</b>\n"
                        f"Ссылка: {extract_link(item)}"
                    )
                    await bot.send_message(user_id, text)
            except Exception as e:
                logger.exception("monitor_loop failed for user %s: %s", user_id, e)
                if user_id in ADMIN_IDS:
                    await bot.send_message(user_id, f"Ошибка мониторинга: <code>{e}</code>")
        await asyncio.sleep(POLL_INTERVAL)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")
    if not MRKT_TOKEN:
        raise RuntimeError("MRKT_TOKEN is empty")

    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.message.register(start_cmd, CommandStart())
    dp.message.register(help_cmd, Command("help"))
    dp.message.register(filters_cmd, Command("filters"))
    dp.message.register(on_cmd, Command("on"))
    dp.message.register(off_cmd, Command("off"))
    dp.message.register(set_collections_cmd, Command("set_collections"))
    dp.message.register(set_models_cmd, Command("set_models"))
    dp.message.register(set_price_cmd, Command("set_price"))
    dp.message.register(stats_cmd, Command("stats"))

    asyncio.create_task(monitor_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
