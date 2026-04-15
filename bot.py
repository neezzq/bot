import asyncio
import html
import json
import logging
import math
import os
import sqlite3
import statistics
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
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
COLLECTIONS_PAGE_SIZE = 8
DISCOVERY_MAX_PAGES = 25

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


class MRKTAPIError(Exception):
    pass


@dataclass
class UserFilter:
    user_id: int
    collections: List[str]
    models: List[str]
    min_price: Optional[float]
    max_price: Optional[float]
    is_enabled: bool


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
            json.dumps(sorted(dict.fromkeys(new_collections)), ensure_ascii=False),
            json.dumps(sorted(dict.fromkeys(new_models)), ensure_ascii=False),
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


def to_raw_ton_value(value: float) -> int:
    return int(round(value * 1_000_000_000))


def from_raw_ton_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
        if not value:
            return None
        try:
            num = float(value)
        except ValueError:
            return None
    elif isinstance(value, (int, float)):
        num = float(value)
    else:
        return None

    if abs(num) >= 1_000_000:
        return num / 1_000_000_000
    return num


def build_payload(user_filter: Optional[UserFilter] = None, *, cursor: str = "", count: Optional[int] = None) -> Dict[str, Any]:
    payload = {
        "collectionNames": user_filter.collections if user_filter else [],
        "modelNames": user_filter.models if user_filter else [],
        "backdropNames": [],
        "symbolNames": [],
        "ordering": "Price",
        "lowToHigh": True,
        "maxPrice": None,
        "minPrice": None,
        "mintable": None,
        "number": None,
        "count": min(count or DEFAULT_SAMPLE_LIMIT, 20),
        "cursor": cursor,
        "query": None,
        "promotedFirst": False,
    }
    if user_filter:
        if user_filter.max_price is not None:
            payload["maxPrice"] = to_raw_ton_value(user_filter.max_price)
        if user_filter.min_price is not None:
            payload["minPrice"] = to_raw_ton_value(user_filter.min_price)
    return payload


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


def extract_cursor(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    cursor = data.get("cursor")
    if cursor in (None, ""):
        return None
    return str(cursor)


def extract_price(item: Dict[str, Any]) -> Optional[float]:
    for key in ("price", "salePrice", "amount", "tonPrice", "floorPrice"):
        price = from_raw_ton_value(item.get(key))
        if price is not None:
            return price
    return None


def extract_buy_price(item: Dict[str, Any]) -> Optional[float]:
    for key in ("buyPrice", "offerPrice", "bestOffer", "bestBuyPrice"):
        value = item.get(key)
        if isinstance(value, dict):
            for nested in ("price", "amount", "tonPrice"):
                price = from_raw_ton_value(value.get(nested))
                if price is not None:
                    return price
        else:
            price = from_raw_ton_value(value)
            if price is not None:
                return price
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


def extract_image_url(item: Dict[str, Any]) -> Optional[str]:
    for key in ("imageUrl", "photoUrl", "previewUrl", "thumbnailUrl"):
        val = item.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    sticker = item.get("sticker")
    if isinstance(sticker, dict):
        for key in ("imageUrl", "previewUrl"):
            val = sticker.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
    return None


def gift_matches_filters(item: Dict[str, Any], user_filter: UserFilter) -> bool:
    collections = [x.strip().lower() for x in user_filter.collections if x.strip()]
    models = [x.strip().lower() for x in user_filter.models if x.strip()]

    item_collection = extract_name(item).strip().lower()
    item_model = extract_model(item).strip().lower()

    if collections and item_collection not in collections:
        return False
    if models and item_model not in models:
        return False

    price = extract_price(item)
    if user_filter.min_price is not None and price is not None and price < user_filter.min_price:
        return False
    if user_filter.max_price is not None and price is not None and price > user_filter.max_price:
        return False
    return True


def fetch_raw_page(payload: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.post(
        f"{API_BASE}/gifts/saling",
        headers=mrkt_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code >= 400:
        body_preview = response.text[:1000]
        raise MRKTAPIError(f"{response.status_code} {body_preview}")
    return response.json()


def fetch_gifts(user_filter: UserFilter) -> List[Dict[str, Any]]:
    data = fetch_raw_page(build_payload(user_filter))
    gifts = extract_gifts_from_response(data)
    return [item for item in gifts if gift_matches_filters(item, user_filter)]


def discover_all_collections(max_pages: int = DISCOVERY_MAX_PAGES) -> List[str]:
    cursor = ""
    found: set[str] = set()
    for _ in range(max_pages):
        data = fetch_raw_page(build_payload(None, cursor=cursor, count=20))
        gifts = extract_gifts_from_response(data)
        for item in gifts:
            name = extract_name(item).strip()
            if name:
                found.add(name)
        next_cursor = extract_cursor(data)
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return sorted(found, key=str.lower)


def compute_stats(gifts: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    sell_prices = [extract_price(x) for x in gifts]
    sell_prices = [x for x in sell_prices if x is not None]
    buy_prices = [extract_buy_price(x) for x in gifts]
    buy_prices = [x for x in buy_prices if x is not None]

    return {
        "count": len(sell_prices),
        "min": min(sell_prices) if sell_prices else None,
        "avg_sell": (sum(sell_prices) / len(sell_prices)) if sell_prices else None,
        "avg_buy": (sum(buy_prices) / len(buy_prices)) if buy_prices else None,
        "median": statistics.median(sell_prices) if sell_prices else None,
    }


def fmt_ton(value: Optional[float]) -> str:
    if value is None:
        return "—"
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{text} TON"


def format_stats_text(stats: Dict[str, Optional[float]], title: str = "Статистика") -> str:
    if not stats["count"]:
        return f"<b>{title}</b>\nЛоты не найдены по текущим фильтрам."
    return (
        f"<b>{title}</b>\n"
        f"Лотов в выборке: <b>{stats['count']}</b>\n"
        f"Минимум: <b>{fmt_ton(stats['min'])}</b>\n"
        f"Средняя цена продажи: <b>{fmt_ton(stats['avg_sell'])}</b>\n"
        f"Средняя цена покупки: <b>{fmt_ton(stats['avg_buy'])}</b>\n"
        f"Медиана продажи: <b>{fmt_ton(stats['median'])}</b>"
    )


def pretty_filter_text(user_filter: UserFilter) -> str:
    return (
        "<b>Твои фильтры</b>\n"
        f"Коллекции: <code>{', '.join(user_filter.collections) if user_filter.collections else 'все'}</code>\n"
        f"Модели: <code>{', '.join(user_filter.models) if user_filter.models else 'все'}</code>\n"
        f"Мин. цена: <code>{user_filter.min_price if user_filter.min_price is not None else 'не задана'}</code>\n"
        f"Макс. цена: <code>{user_filter.max_price if user_filter.max_price is not None else 'не задана'}</code>\n"
        f"Уведомления: <b>{'включены' if user_filter.is_enabled else 'выключены'}</b>"
    )


def chunked(items: Sequence[str], size: int) -> List[List[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def collection_selector_keyboard(all_collections: Sequence[str], selected: Sequence[str], page: int = 0) -> InlineKeyboardMarkup:
    pages = chunked(list(all_collections), COLLECTIONS_PAGE_SIZE) or [[]]
    page = max(0, min(page, len(pages) - 1))
    selected_set = {x.lower() for x in selected}

    rows: List[List[InlineKeyboardButton]] = []
    for name in pages[page]:
        mark = "✅ " if name.lower() in selected_set else "▫️ "
        rows.append([
            InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"coll_toggle:{page}:{name}"),
        ])

    rows.append(
        [
            InlineKeyboardButton(text="✅ Все" if not selected else "Выбрать все", callback_data=f"coll_all:{page}"),
            InlineKeyboardButton(text="Очистить", callback_data=f"coll_clear:{page}"),
        ]
    )

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"coll_page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{len(pages)}", callback_data="coll_noop"))
    if page < len(pages) - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"coll_page:{page + 1}"))
    rows.append(nav)

    rows.append([InlineKeyboardButton(text="Закрыть", callback_data="coll_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def safe_edit_text(target: Message, text: str, reply_markup: InlineKeyboardMarkup) -> None:
    try:
        await target.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await target.answer(text, reply_markup=reply_markup)


async def start_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    text = (
        "<b>MRKT Alert Bot</b>\n\n"
        "Бот следит за новыми выставлениями на MRKT и шлёт алерты по твоим фильтрам.\n\n"
        "Команды:\n"
        "/collections — выбрать коллекции кнопками\n"
        "/set_collections Chill Flame, Vice Cream\n"
        "/set_collections all\n"
        "/set_models Albino, Gold\n"
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
        "1. Открой список всех коллекций: <code>/collections</code>\n"
        "2. Выбери несколько нужных коллекций кнопками или нажми «Выбрать все».\n"
        "3. При желании укажи модели: <code>/set_models Albino, Gold</code>\n"
        "4. Ограничь цену: <code>/set_price 1 15</code>\n"
        "5. Сбросить цену: <code>/reset_price</code>\n"
        "6. Получи статистику: <code>/stats</code>"
    )


async def filters_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    await message.answer(pretty_filter_text(get_filter(message.from_user.id)))


async def collections_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    await message.answer("Собираю список всех коллекций MRKT, подожди пару секунд...")
    try:
        all_collections = await asyncio.to_thread(discover_all_collections)
    except Exception as e:
        await message.answer(f"Не удалось получить коллекции: <code>{html.escape(str(e))}</code>")
        return

    user_filter = get_filter(message.from_user.id)
    text = (
        "<b>Выбор коллекций</b>\n"
        "Можно выбрать несколько коллекций.\n"
        "Если фильтр пустой — бот показывает <b>все</b> коллекции.\n\n"
        f"Сейчас выбрано: <code>{', '.join(user_filter.collections) if user_filter.collections else 'все'}</code>"
    )
    await message.answer(text, reply_markup=collection_selector_keyboard(all_collections, user_filter.collections, 0))


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
    if raw.lower() in {"all", "все", "*"}:
        values: List[str] = []
    else:
        values = [x.strip() for x in raw.split(",") if x.strip()]
    update_filter(message.from_user.id, collections=values)
    await message.answer(f"Сохранил коллекции: <code>{', '.join(values) if values else 'все'}</code>")


async def set_models_cmd(message: Message) -> None:
    ensure_user(message.from_user.id)
    raw = (message.text or "").replace("/set_models", "", 1).strip()
    if raw.lower() in {"all", "все", "*"}:
        values: List[str] = []
    else:
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
        gifts = await asyncio.to_thread(fetch_gifts, user_filter)
    except MRKTAPIError as e:
        await message.answer(f"Ошибка MRKT API: <code>{html.escape(str(e))}</code>")
        return
    except Exception as e:
        await message.answer(f"Ошибка запроса к MRKT: <code>{html.escape(str(e))}</code>")
        return

    stats = compute_stats(gifts)
    lines = [format_stats_text(stats)]
    preview = []
    for item in gifts[:5]:
        preview.append(f"• {html.escape(extract_name(item))} / {html.escape(extract_model(item))} — <b>{fmt_ton(extract_price(item))}</b>")
    if preview:
        lines.append("\n<b>Первые лоты:</b>\n" + "\n".join(preview))
    await message.answer("\n\n".join(lines))


async def collection_callback(callback: CallbackQuery) -> None:
    ensure_user(callback.from_user.id)
    user_filter = get_filter(callback.from_user.id)

    try:
        all_collections = await asyncio.to_thread(discover_all_collections)
    except Exception as e:
        await callback.answer(f"Не удалось загрузить коллекции: {e}", show_alert=True)
        return

    data = callback.data or ""
    if data == "coll_noop":
        await callback.answer()
        return
    if data == "coll_close":
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Закрыто")
        return

    page = 0
    if ":" in data:
        try:
            page = int(data.split(":", 2)[1])
        except Exception:
            page = 0

    selected = list(user_filter.collections)
    selected_lower = {x.lower() for x in selected}

    if data.startswith("coll_page:"):
        pass
    elif data.startswith("coll_all:"):
        selected = []
        update_filter(callback.from_user.id, collections=[])
        user_filter = get_filter(callback.from_user.id)
        selected = user_filter.collections
    elif data.startswith("coll_clear:"):
        selected = []
        update_filter(callback.from_user.id, collections=[])
        user_filter = get_filter(callback.from_user.id)
        selected = user_filter.collections
    elif data.startswith("coll_toggle:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            name = parts[2]
            if name.lower() in selected_lower:
                selected = [x for x in selected if x.lower() != name.lower()]
            else:
                selected.append(name)
            update_filter(callback.from_user.id, collections=selected)
            user_filter = get_filter(callback.from_user.id)
            selected = user_filter.collections

    text = (
        "<b>Выбор коллекций</b>\n"
        "Можно выбрать несколько коллекций.\n"
        "Если фильтр пустой — бот показывает <b>все</b> коллекции.\n\n"
        f"Сейчас выбрано: <code>{', '.join(selected) if selected else 'все'}</code>"
    )
    await callback.message.edit_text(text, reply_markup=collection_selector_keyboard(all_collections, selected, page))
    await callback.answer("Сохранено")


async def monitor_loop(bot: Bot) -> None:
    await asyncio.sleep(3)
    while True:
        user_ids = get_enabled_user_ids()
        for user_id in user_ids:
            try:
                user_filter = get_filter(user_id)
                gifts = await asyncio.to_thread(fetch_gifts, user_filter)
                new_items = []
                for item in gifts:
                    listing_key = extract_listing_key(item)
                    if remember_listing(f"{user_id}:{listing_key}"):
                        new_items.append(item)

                for item in new_items[:10]:
                    price = fmt_ton(extract_price(item))
                    text = (
                        "<b>Новое выставление на MRKT</b>\n"
                        f"Подарок: <b>{html.escape(extract_name(item))}</b>\n"
                        f"Модель: <b>{html.escape(extract_model(item))}</b>\n"
                        f"Цена: <b>{price}</b>\n"
                        f"Ссылка: {html.escape(extract_link(item))}"
                    )
                    image_url = extract_image_url(item)
                    if image_url:
                        try:
                            await bot.send_photo(user_id, image_url, caption=text)
                            continue
                        except Exception:
                            logger.exception("send_photo failed, fallback to text")
                    await bot.send_message(user_id, text)
            except Exception as e:
                logger.exception("monitor_loop failed for user %s: %s", user_id, e)
                if user_id in ADMIN_IDS:
                    await bot.send_message(user_id, f"Ошибка мониторинга: <code>{html.escape(str(e))}</code>")
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
    dp.message.register(collections_cmd, Command("collections"))
    dp.message.register(on_cmd, Command("on"))
    dp.message.register(off_cmd, Command("off"))
    dp.message.register(set_collections_cmd, Command("set_collections"))
    dp.message.register(set_models_cmd, Command("set_models"))
    dp.message.register(set_price_cmd, Command("set_price"))
    dp.message.register(reset_price_cmd, Command("reset_price"))
    dp.message.register(stats_cmd, Command("stats"))
    dp.callback_query.register(collection_callback, F.data.startswith("coll_"))

    asyncio.create_task(monitor_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
