import asyncio
import json
import logging
import os
import sqlite3
import statistics
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

API_BASE = "https://api.tgmrkt.io/api/v1"
DB_PATH = os.getenv("DB_PATH", "mrkt_bot.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MRKT_TOKEN = os.getenv("MRKT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "45"))
DEFAULT_SAMPLE_LIMIT = min(int(os.getenv("DEFAULT_SAMPLE_LIMIT", "20")), 20)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


class MRKTAPIError(Exception):
    pass


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
    cur.execute(
        "UPDATE users SET is_enabled = ?, updated_at = ? WHERE user_id = ?",
        (1 if enabled else 0, int(time.time()), user_id),
    )
    conn.commit()
    conn.close()



def update_filter(
    user_id: int,
    *,
    collections: Optional[List[str]] = None,
    models: Optional[List[str]] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    reset_price: bool = False,
) -> None:
    conn = db_connect()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM filters WHERE user_id = ?", (user_id,)).fetchone()
    current = dict(row) if row else {}

    new_collections = collections if collections is not None else json.loads(current.get("collections", "[]"))
    new_models = models if models is not None else json.loads(current.get("models", "[]"))

    if reset_price:
        new_min = None
        new_max = None
    else:
        new_min = min_price if min_price is not None else current.get("min_price")
        new_max = max_price if max_price is not None else current.get("max_price")

    cur.execute(
        """
        UPDATE filters
        SET collections = ?, models = ?, min_price = ?, max_price = ?, updated_at = ?
        WHERE user_id = ?
        """,
        (
            json.dumps(new_collections, ensure_ascii=False),
            json.dumps(new_models, ensure_ascii=False),
            new_min,
            new_max,
            int(time.time()),
            user_id,
        ),
    )
    conn.commit()
    conn.close()



def get_filter(user_id: int) -> UserFilter:
    conn = db_connect()
    cur = conn.cursor()
    row_user = cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    row_filter = cur.execute("SELECT * FROM filters WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()

    if row_user is None or row_filter is None:
        ensure_user(user_id)
        return get_filter(user_id)

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



def build_payload() -> Dict[str, Any]:
    return {
        "collectionNames": [],
        "modelNames": [],
        "backdropNames": [],
        "symbolNames": [],
        "ordering": "Price",
        "lowToHigh": True,
        "maxPrice": None,
        "minPrice": None,
        "mintable": None,
        "number": None,
        "count": DEFAULT_SAMPLE_LIMIT,
        "cursor": "",
        "query": None,
        "promotedFirst": False,
    }



def mrkt_headers() -> Dict[str, str]:
    return {
        "Authorization": MRKT_TOKEN,
        "Referer": "https://cdn.tgmrkt.io/",
        "Origin": "https://cdn.tgmrkt.io",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }



def extract_gifts_from_response(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []

    for key in ("gifts", "items", "data", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            for nested_key in ("gifts", "items", "results"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, list):
                    return [x for x in nested_value if isinstance(x, dict)]
    return []



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
    return "https://t.me/mrkt"



def extract_listing_key(item: Dict[str, Any]) -> str:
    for key in ("id", "giftId", "listingId", "slug"):
        if item.get(key) is not None:
            return f"{key}:{item.get(key)}"
    return json.dumps(item, sort_keys=True, ensure_ascii=False)



def gift_matches_filters(item: Dict[str, Any], user_filter: UserFilter) -> bool:
    collections = [x.strip().lower() for x in user_filter.collections if x.strip()]
    models = [x.strip().lower() for x in user_filter.models if x.strip()]

    haystack = " ".join(
        [
            str(item.get("name", "")),
            str(item.get("title", "")),
            str(item.get("giftName", "")),
            str(item.get("collectionName", "")),
            str(item.get("modelName", "")),
            str(item.get("backdropName", "")),
            str(item.get("symbolName", "")),
        ]
    ).lower()

    if collections and not any(word in haystack for word in collections):
        return False

    if models and not any(word in haystack for word in models):
        return False

    price = extract_price(item)
    if user_filter.min_price is not None and price is not None and price < user_filter.min_price:
        return False
    if user_filter.max_price is not None and price is not None and price > user_filter.max_price:
        return False

    return True



def fetch_gifts(user_filter: UserFilter) -> List[Dict[str, Any]]:
    payload = build_payload()
    response = requests.post(
        f"{API_BASE}/gifts/saling",
        headers=mrkt_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code >= 400:
        body_preview = response.text[:1000]
        raise MRKTAPIError(f"{response.status_code} {body_preview}")

    data = response.json()
    gifts = extract_gifts_from_response(data)
    return [item for item in gifts if gift_matches_filters(item, user_filter)]



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
        return f"<b>{title}</b>\nЛоты не найдены по текущим фильтрам."
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
    text = (
        "<b>MRKT Alert Bot</b>\n\n"
        "Бот следит за новыми выставлениями на MRKT и шлёт алерты по твоим фильтрам.\n\n"
        "Команды:\n"
        "/set_collections часы, кепки\n"
        "/set_models albino, gold\n"
        "/set_price 1 20\n"
        "/reset_price\n"
        "/stats\n"
        "/filters\n"
        "/on и /off\n"
        "/help"
    )
    await message.answer(text)


async def help_cmd(message: Message) -> None:
    await message.answer(
        "<b>Как пользоваться</b>\n"
        "1. Задай ключевые слова по подаркам: <code>/set_collections часы, кепки</code>\n"
        "2. При желании укажи модели: <code>/set_models gold, black</code>\n"
        "3. Ограничь цену: <code>/set_price 1 15</code>\n"
        "4. Сбросить цену: <code>/reset_price</code>\n"
        "5. Получи статистику: <code>/stats</code>\n"
        "6. Оставь бот включённым — он будет слать новые лоты по фильтру.\n\n"
        "Важно: API MRKT фильтруется по внутренним значениям, поэтому бот теперь берёт общий список лотов и фильтрует его локально по словам и цене."
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
    update_filter(message.from_user.id, models=values)
    await message.answer(f"Сохранил модели: <code>{', '.join(values) if values else 'все'}</code>")


async def set_price_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Формат: <code>/set_price 1 20</code>")
        return

    try:
        min_price = float(parts[1].replace(",", "."))
        max_price = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("Цена должна быть числом. Пример: <code>/set_price 1 20</code>")
        return

    if min_price > max_price:
        await message.answer("Минимальная цена не может быть больше максимальной.")
        return

    update_filter(message.from_user.id, min_price=min_price, max_price=max_price)
    await message.answer(f"Диапазон цены сохранён: <b>{min_price}</b>–<b>{max_price}</b> TON")


async def reset_price_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    update_filter(message.from_user.id, reset_price=True)
    await message.answer("Фильтр цены сброшен.")


async def stats_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    user_filter = get_filter(message.from_user.id)
    try:
        gifts = fetch_gifts(user_filter)
    except MRKTAPIError as e:
        await message.answer(f"Ошибка MRKT API: <code>{e}</code>")
        return
    except Exception as e:
        await message.answer(f"Ошибка запроса к MRKT: <code>{e}</code>")
        return

    stats = compute_stats(gifts)
    lines = [format_stats_text(stats)]
    preview = []
    for item in gifts[:5]:
        price = extract_price(item)
        if price is not None:
            preview.append(f"• {extract_name(item)} / {extract_model(item)} — <b>{price:.2f} TON</b>")
        else:
            preview.append(f"• {extract_name(item)} / {extract_model(item)}")
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
                    price_text = f"<b>{price:.2f} TON</b>" if price is not None else "<b>цена не указана</b>"
                    text = (
                        "<b>Новое выставление на MRKT</b>\n"
                        f"Подарок: <b>{extract_name(item)}</b>\n"
                        f"Модель: <b>{extract_model(item)}</b>\n"
                        f"Цена: {price_text}\n"
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
    dp.message.register(reset_price_cmd, Command("reset_price"))
    dp.message.register(stats_cmd, Command("stats"))

    asyncio.create_task(monitor_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
