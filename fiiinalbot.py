# -*- coding: utf-8 -*-
"""
Бот для продажи водянок и доступа в приват за Telegram Stars.
Один файл, aiogram 3.x, база SQLite.

Установка:  pip install -U aiogram   (нужен aiogram 3.x, лучше свежая версия)
Запуск:     python bot.py

База данных: файл shop.db лежит рядом с bot.py (или по пути из переменной окружения DB_PATH).
Состояния диалогов (FSM) тоже хранятся в этой базе, поэтому после перезапуска бота ничего не теряется.

Перед запуском заполните блок НАСТРОЙКИ ниже:
  - BOT_TOKEN (или переменная окружения BOT_TOKEN)
  - ADMIN_IDS: ID админов, первый в списке - главный админ
  - PRIVATE_GROUP_ID: ID закрытой группы привата (число вида -100...),
    бот должен быть в ней админом с правом "Приглашать пользователей по ссылкам"
"""

from __future__ import annotations

import asyncio
import calendar
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

# ======================================================================
# НАСТРОЙКИ
# ======================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8511170748:AAF1htcmjb_lf41yAjrSs19o3KQN9jHOtQ8")
PROVIDER_TOKEN = ""                            # для Telegram Stars остаётся пустым
BOT_USERNAME = "Zurtyxz_PayShop_Bot"          # без @

ADMIN_IDS = [5254779646]                       # первый ID - главный админ

PRIVATE_GROUP_ID = -1003363050666              # ID закрытой группы привата

TZ_OFFSET_HOURS = 3                            # часовой пояс для дат (3 = Москва)
# Файл базы по умолчанию лежит рядом с bot.py (а не в текущей папке запуска), чтобы при запуске из другого
# места не создалась новая пустая база. Свой путь можно задать переменной окружения DB_PATH.
_default_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop.db")
if not os.path.exists(_default_db) and os.path.exists("shop.db"):
    _default_db = os.path.abspath("shop.db")       # база, созданная прежней версией в папке запуска
DB_PATH = os.path.abspath(os.getenv("DB_PATH") or _default_db)
REVIEW_WINDOW_HOURS = 8                        # сколько часов ждём отзыв после выдачи
PENDING_TTL = 2 * 3600                         # срок жизни счёта на оплату, секунд
MAX_TOPUP = 10000                              # максимум одного пополнения баланса, звёзд
PAGE_SIZE = 16                                 # водянок на странице каталога
ORDERS_PAGE_SIZE = 10                          # заказов на странице в админке
# ======================================================================

STAR = "⭐"
esc = html.escape
bot: Bot = None  # type: ignore  # создаётся в main()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("shop")


# ======================================================================
# Время
# ======================================================================
def now_ts() -> int:
    return int(time.time())


def local_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)


def fmt_ts(ts: int) -> str:
    return local_dt(ts).strftime("%d.%m.%Y %H:%M")


def fmt_short(ts: int) -> str:
    return local_dt(ts).strftime("%d.%m %H:%M")


def parse_dt(text: str):
    """Разбирает 'ДД.ММ.ГГГГ ЧЧ:ММ' или 'ДД.ММ.ГГГГ' (время по TZ_OFFSET_HOURS) в UTC-метку."""
    text = text.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return calendar.timegm(dt.timetuple()) - TZ_OFFSET_HOURS * 3600
    return None


def day_start_ts(ts: int) -> int:
    d = local_dt(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp()) - TZ_OFFSET_HOURS * 3600


def month_start_ts(ts: int) -> int:
    d = local_dt(ts).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp()) - TZ_OFFSET_HOURS * 3600


# ======================================================================
# База данных
# ======================================================================
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row


def q1(sql: str, args: tuple = ()):
    return db.execute(sql, args).fetchone()


def qa(sql: str, args: tuple = ()):
    return db.execute(sql, args).fetchall()


def ex(sql: str, args: tuple = ()):
    return db.execute(sql, args)


@contextmanager
def tx():
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',
    first_name TEXT NOT NULL DEFAULT '',
    balance INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    referrer_id INTEGER,
    ref_bonus INTEGER NOT NULL DEFAULT 0,
    joined_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS products(
    number INTEGER PRIMARY KEY,
    price INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    file_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cart(
    user_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    PRIMARY KEY(user_id, number)
);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    total INTEGER NOT NULL,
    from_balance INTEGER NOT NULL DEFAULT 0,
    from_stars INTEGER NOT NULL DEFAULT 0,
    charge_id TEXT,
    status TEXT NOT NULL,
    bonus_id INTEGER,
    invite_link TEXT,
    created_at INTEGER NOT NULL,
    done_at INTEGER,
    issued_by INTEGER,
    review_requested_at INTEGER,
    review_done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE TABLE IF NOT EXISTS order_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    nick TEXT NOT NULL,
    price INTEGER NOT NULL,
    base_price INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
CREATE TABLE IF NOT EXISTS pending(
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    total INTEGER NOT NULL,
    stars INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments(
    charge_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS promos(
    code TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_uses(
    code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    used_at INTEGER NOT NULL,
    PRIMARY KEY(code, user_id)
);
CREATE TABLE IF NOT EXISTS bonuses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    source TEXT,
    used INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    used_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_bonuses_user ON bonuses(user_id);
CREATE TABLE IF NOT EXISTS discounts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    percent INTEGER NOT NULL,
    mode TEXT NOT NULL,
    numbers TEXT NOT NULL DEFAULT '',
    starts_at INTEGER NOT NULL,
    ends_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    description TEXT NOT NULL,
    ref TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, id);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fsm(
    key TEXT PRIMARY KEY,
    state TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    updated_at INTEGER NOT NULL DEFAULT 0
);
"""

DEFAULT_SETTINGS = {
    "bot_enabled": "1",
    "ref_reward": "3",
    "private_price": "0",
    "private_text": "Доступ в закрытую группу.",
    "last_backup": "",
}


def init_db() -> None:
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")      # каждая подтверждённая операция сразу записана на диск
    check = db.execute("PRAGMA quick_check").fetchone()[0]
    if check != "ok":
        raise SystemExit(f"База данных повреждена ({DB_PATH}): {check}. Восстановите её из резервной копии.")
    db.executescript(SCHEMA)
    ex("DELETE FROM pending WHERE created_at < ?", (now_ts() - 7 * 86400,))
    ex("DELETE FROM fsm WHERE updated_at < ?", (now_ts() - 30 * 86400,))
    log.info(
        "База данных: %s (пользователей: %s, заказов: %s)",
        DB_PATH,
        q1("SELECT COUNT(*) AS c FROM users")["c"],
        q1("SELECT COUNT(*) AS c FROM orders")["c"],
    )
    if not get_setting("last_backup"):
        set_setting("last_backup", str(now_ts()))


_db_closed = False


def close_db() -> None:
    """Переносит журнал WAL в основной файл базы и закрывает соединение (при остановке бота)."""
    global _db_closed
    if _db_closed:
        return
    _db_closed = True
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        log.info("База данных сохранена и закрыта.")
    except Exception as err:
        log.warning("Не удалось корректно закрыть базу: %s", err)


def get_setting(key: str) -> str:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    if row is not None:
        return row["value"]
    return DEFAULT_SETTINGS.get(key, "")


def set_setting(key: str, value) -> None:
    ex("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, str(value)))


# ======================================================================
# Пользователи
# ======================================================================
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def user_exists(uid: int) -> bool:
    return q1("SELECT 1 FROM users WHERE id=?", (uid,)) is not None


def ensure_user(u) -> bool:
    """Создаёт или обновляет пользователя. Возвращает True, если пользователь новый."""
    row = q1("SELECT id, username, first_name FROM users WHERE id=?", (u.id,))
    uname = u.username or ""
    fname = u.first_name or ""
    if row is None:
        ex(
            "INSERT INTO users(id, username, first_name, joined_at) VALUES(?,?,?,?)",
            (u.id, uname, fname, now_ts()),
        )
        return True
    if row["username"] != uname or row["first_name"] != fname:
        ex("UPDATE users SET username=?, first_name=? WHERE id=?", (uname, fname, u.id))
    return False


def get_balance(uid: int) -> int:
    row = q1("SELECT balance FROM users WHERE id=?", (uid,))
    return row["balance"] if row else 0


def log_tx(uid: int, kind: str, amount: int, description: str, ref=None) -> None:
    """Записывает операцию по балансу. Вызывать сразу после изменения баланса."""
    ex(
        "INSERT INTO transactions(user_id, kind, amount, balance_after, description, ref, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (uid, kind, amount, get_balance(uid), description, ref, now_ts()),
    )


def user_ref(u) -> str:
    """Упоминание пользователя в HTML."""
    if u is None:
        return "неизвестный пользователь"
    if u["username"]:
        return "@" + esc(u["username"])
    name = esc(u["first_name"] or "без имени")
    return f'<a href="tg://user?id={u["id"]}">{name}</a>'


def user_short(u) -> str:
    """Короткое имя для текста кнопок (без HTML)."""
    if u is None:
        return "?"
    if u["username"]:
        return "@" + u["username"]
    return (u["first_name"] or str(u["id"]))[:20]


def find_user(text: str):
    t = text.strip()
    if t.startswith("@"):
        t = t[1:]
    if not t:
        return None
    if t.isdigit():
        return q1("SELECT * FROM users WHERE id=?", (int(t),))
    return q1("SELECT * FROM users WHERE lower(username)=lower(?)", (t,))


# ======================================================================
# Акции и цены
# ======================================================================
def active_discount(ts: int | None = None):
    ts = ts or now_ts()
    return q1(
        "SELECT * FROM discounts WHERE status='active' AND starts_at<=? AND ends_at>? "
        "ORDER BY percent DESC LIMIT 1",
        (ts, ts),
    )


def disc_numbers(d) -> list:
    return [int(x) for x in d["numbers"].split(",") if x.strip().isdigit()]


def discount_applies(d, number: int) -> bool:
    if d is None:
        return False
    if d["mode"] == "all":
        return True
    nums = set(disc_numbers(d))
    if d["mode"] == "selected":
        return number in nums
    if d["mode"] == "except":
        return number not in nums
    return False


def calc_price(base: int, number: int, d) -> int:
    """Цена со скидкой: округление вниз (в пользу покупателя), минимум 1 звезда."""
    if d is not None and discount_applies(d, number):
        return max(base * (100 - d["percent"]) // 100, 1)
    return base


def price_html(base: int, final: int) -> str:
    if final < base:
        return f"<s>{base}</s> <b>{final}</b> {STAR}"
    return f"<b>{final}</b> {STAR}"


def disc_scope(d) -> str:
    nums = ", ".join(f"№{n}" for n in disc_numbers(d))
    if d["mode"] == "all":
        return "все водянки"
    if d["mode"] == "selected":
        return f"только {nums}"
    return f"все, кроме {nums}"


def disc_state(d, ts: int | None = None) -> str:
    ts = ts or now_ts()
    if d["status"] == "ended":
        return "завершена"
    if ts < d["starts_at"]:
        return "запланирована"
    if ts >= d["ends_at"]:
        return "истекла"
    return "активна"


def disc_line(d) -> str:
    return (
        f"#{d['id']}: -{d['percent']}%, {disc_scope(d)}, "
        f"{fmt_ts(d['starts_at'])} - {fmt_ts(d['ends_at'])}, {disc_state(d)}"
    )


def disc_overlap(start: int, end: int, exclude_id: int = 0) -> bool:
    row = q1(
        "SELECT 1 FROM discounts WHERE status='active' AND id != ? AND starts_at < ? AND ends_at > ?",
        (exclude_id, end, start),
    )
    return row is not None


# ======================================================================
# Интерфейс: кнопки и отправка сообщений
# ======================================================================
def B(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def KB(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def main_kb() -> InlineKeyboardMarkup:
    return KB(
        [B("Каталог", "cat"), B("Поиск по номеру", "search")],
        [B("Корзина", "cart"), B("Личный кабинет", "cab")],
    )


WELCOME = "Добро пожаловать! Здесь можно купить водянки и доступ в приват. Выберите действие."


async def safe_answer(cb: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        await cb.answer(text, show_alert=alert)
    except Exception:
        pass


async def notify_user(uid: int, text: str, markup=None) -> bool:
    try:
        await bot.send_message(uid, text, reply_markup=markup)
        return True
    except Exception as err:
        log.warning("Не удалось отправить сообщение %s: %s", uid, err)
        return False


async def notify_admins(text: str, markup=None) -> None:
    for aid in ADMIN_IDS:
        await notify_user(aid, text, markup)


async def show(cb: CallbackQuery, text: str, markup=None) -> None:
    """Показывает экран: редактирует текстовое сообщение или заменяет медиа-сообщение новым."""
    msg = cb.message
    uid = cb.from_user.id
    is_text = getattr(msg, "text", None) is not None
    if is_text:
        try:
            await bot.edit_message_text(
                text, chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=markup
            )
            return
        except TelegramBadRequest as err:
            if "not modified" in str(err).lower():
                return
        except Exception as err:
            log.warning("edit failed: %s", err)
    else:
        try:
            await bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except Exception:
            pass
    await bot.send_message(uid, text, reply_markup=markup)


async def delete_cb_message(cb: CallbackQuery) -> None:
    try:
        await bot.delete_message(chat_id=cb.message.chat.id, message_id=cb.message.message_id)
    except Exception:
        pass


def extract_media(m: Message):
    """Возвращает (тип, file_id) или None."""
    if m.photo:
        return "photo", m.photo[-1].file_id
    if m.video:
        return "video", m.video.file_id
    if m.animation:
        return "animation", m.animation.file_id
    if m.document:
        return "document", m.document.file_id
    if m.audio:
        return "audio", m.audio.file_id
    if m.voice:
        return "voice", m.voice.file_id
    if m.video_note:
        return "video_note", m.video_note.file_id
    if m.sticker:
        return "sticker", m.sticker.file_id
    return None


async def send_media(chat_id: int, mtype: str, file_id: str, caption=None, markup=None):
    if mtype == "photo":
        return await bot.send_photo(chat_id, file_id, caption=caption, reply_markup=markup)
    if mtype == "video":
        return await bot.send_video(chat_id, file_id, caption=caption, reply_markup=markup)
    if mtype == "animation":
        return await bot.send_animation(chat_id, file_id, caption=caption, reply_markup=markup)
    if mtype == "document":
        return await bot.send_document(chat_id, file_id, caption=caption, reply_markup=markup)
    if mtype == "audio":
        return await bot.send_audio(chat_id, file_id, caption=caption, reply_markup=markup)
    if mtype == "voice":
        return await bot.send_voice(chat_id, file_id, caption=caption, reply_markup=markup)
    if mtype == "video_note":
        return await bot.send_video_note(chat_id, file_id, reply_markup=markup)
    if mtype == "sticker":
        return await bot.send_sticker(chat_id, file_id, reply_markup=markup)
    raise ValueError(f"Неизвестный тип медиа: {mtype}")


_admin_cache: dict = {}


async def admin_contact(admin_id: int | None = None) -> str:
    """Юзернейм админа (получается по ID) или ссылка-упоминание, если юзернейма нет."""
    aid = admin_id or ADMIN_IDS[0]
    cached = _admin_cache.get(aid)
    if cached and time.time() - cached[1] < 3600:
        return cached[0]
    text = f'<a href="tg://user?id={aid}">администратору</a>'
    try:
        chat = await bot.get_chat(aid)
        if chat.username:
            text = "@" + esc(chat.username)
    except Exception as err:
        log.warning("get_chat(%s) failed: %s", aid, err)
    _admin_cache[aid] = (text, time.time())
    return text


# ======================================================================
# Витрины: каталог, карточка, корзина, заказы
# ======================================================================
def catalog_view(page: int, item_prefix: str, page_prefix: str, back_data: str, title: str, extra=None):
    total = q1("SELECT COUNT(*) AS c FROM products")["c"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    rows = qa(
        "SELECT number, price FROM products ORDER BY number LIMIT ? OFFSET ?",
        (PAGE_SIZE, (page - 1) * PAGE_SIZE),
    )
    d = active_discount()
    buttons = [
        B(f"№{r['number']} · {calc_price(r['price'], r['number'], d)}{STAR}", f"{item_prefix}:{r['number']}")
        for r in rows
    ]
    kb_rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    if pages > 1:
        nav = []
        if page > 1:
            nav.append(B("‹ Назад", f"{page_prefix}:{page - 1}"))
        nav.append(B(f"{page}/{pages}", "noop"))
        if page < pages:
            nav.append(B("Вперёд ›", f"{page_prefix}:{page + 1}"))
        kb_rows.append(nav)
    for row in extra or []:
        kb_rows.append(row)
    kb_rows.append([B("Назад", back_data)])
    text = f"<b>{title}</b>\n"
    if total == 0:
        text += "Пока нет доступных водянок."
    else:
        text += f"Всего: {total}. Страница {page} из {pages}.\nВыберите номер водянки."
    if d is not None:
        text += f"\n\nДействует акция: скидка {d['percent']}% до {fmt_ts(d['ends_at'])}."
    return text, KB(*kb_rows)


async def send_card(uid: int, number: int, mode: str = "shop", bonus_id: int = 0) -> None:
    p = q1("SELECT * FROM products WHERE number=?", (number,))
    if p is None:
        await bot.send_message(
            uid, "Водянка с таким номером не найдена.", reply_markup=KB([B("Каталог", "cat")])
        )
        return
    idx = q1("SELECT COUNT(*) AS c FROM products WHERE number<?", (number,))["c"]
    page = idx // PAGE_SIZE + 1
    if mode == "bonus":
        caption = f"<b>Водянка №{number}</b>\nЦена: бесплатно (бонус)"
        markup = KB(
            [B("Выбрать эту водянку", f"bsel:{bonus_id}:{number}")],
            [B("К списку", f"bpg:{bonus_id}:{page}")],
        )
    else:
        final = calc_price(p["price"], number, active_discount())
        caption = f"<b>Водянка №{number}</b>\nЦена: {price_html(p['price'], final)}"
        markup = KB(
            [B("Купить сразу", f"buy:{number}"), B("В корзину", f"ca:{number}")],
            [B("К списку", f"wl:{page}")],
        )
    await send_media(uid, p["media_type"], p["file_id"], caption, markup)


def cart_view(uid: int):
    rows = qa(
        "SELECT p.number, p.price FROM cart c JOIN products p ON p.number=c.number "
        "WHERE c.user_id=? ORDER BY p.number",
        (uid,),
    )
    if not rows:
        return "<b>Корзина</b>\nКорзина пуста.", KB([B("Каталог", "cat")], [B("В меню", "menu")])
    d = active_discount()
    lines = ["<b>Корзина</b>"]
    total = 0
    kb_rows = []
    for r in rows:
        final = calc_price(r["price"], r["number"], d)
        total += final
        lines.append(f"Водянка №{r['number']} - {price_html(r['price'], final)}")
        kb_rows.append([B(f"Убрать №{r['number']}", f"cd:{r['number']}")])
    lines.append(f"\nСтоимость заказа: <b>{total}</b> {STAR}")
    lines.append(f"На вашем балансе: <b>{get_balance(uid)}</b> {STAR}")
    kb_rows.append([B("Оформить заказ", "cbuy")])
    kb_rows.append([B("Очистить корзину", "cclear"), B("Назад", "menu")])
    return "\n".join(lines), KB(*kb_rows)


STATUS_RU = {"active": "ожидает выдачи", "done": "выдан", "cancelled": "отменён"}


def order_text(o, items) -> str:
    """Подробная карточка заказа для админа."""
    u = q1("SELECT * FROM users WHERE id=?", (o["user_id"],))
    lines = [
        f"<b>Заказ #{o['id']}</b> ({STATUS_RU.get(o['status'], o['status'])})",
        f"Покупатель: {user_ref(u)}, ID <code>{o['user_id']}</code>",
        f"Создан: {fmt_ts(o['created_at'])}",
    ]
    if o["kind"] == "private":
        lines.append("Товар: приват")
    else:
        for it in items:
            lines.append(
                f"Водянка №{it['number']}, ник: <code>{esc(it['nick'])}</code>, цена {it['price']} {STAR}"
            )
    if o["bonus_id"]:
        lines.append("Оплата: бонус (бесплатная водянка)")
    else:
        lines.append(
            f"Сумма: {o['total']} {STAR} (с баланса {o['from_balance']}, через Stars {o['from_stars']})"
        )
    if o["charge_id"]:
        lines.append(f"Платёж: <code>{esc(o['charge_id'])}</code>")
    if o["done_at"]:
        lines.append(f"Закрыт: {fmt_ts(o['done_at'])}")
    return "\n".join(lines)


def user_order_text(o, items) -> str:
    lines = [f"<b>Заказ #{o['id']}</b> ({STATUS_RU.get(o['status'], o['status'])})", f"Дата: {fmt_ts(o['created_at'])}"]
    if o["kind"] == "private":
        lines.append("Приват")
    else:
        for it in items:
            lines.append(f"Водянка №{it['number']}, ник: <code>{esc(it['nick'])}</code>")
    lines.append("Оплачено бонусом" if o["bonus_id"] else f"Сумма: {o['total']} {STAR}")
    return "\n".join(lines)


# ======================================================================
# Покупка: расчёт, счёт, оформление
# ======================================================================
def build_quote(kind: str, pairs: list):
    """Возвращает (позиции, итог) или None, если какой-то товар недоступен."""
    if kind == "private":
        return [], int(get_setting("private_price") or 0)
    d = active_discount()
    snap = []
    total = 0
    for number, nick in pairs:
        p = q1("SELECT price FROM products WHERE number=?", (number,))
        if p is None:
            return None
        price = calc_price(p["price"], number, d)
        snap.append({"number": number, "nick": nick, "price": price, "base": p["price"]})
        total += price
    return snap, total


def checkout_text(kind: str, snap: list, total: int, bal: int):
    lines = ["<b>Ваш заказ</b>"]
    if kind == "private":
        lines.append(f"Приват - {total} {STAR}")
    else:
        for it in snap:
            lines.append(
                f"Водянка №{it['number']} (ник: <code>{esc(it['nick'])}</code>) - "
                f"{price_html(it['base'], it['price'])}"
            )
    lines.append("")
    lines.append(f"Стоимость заказа: <b>{total}</b> {STAR}")
    lines.append(f"На вашем балансе: <b>{bal}</b> {STAR}")
    if bal >= total:
        lines.append(f"С баланса будет списано: {total} {STAR}. Останется: {bal - total} {STAR}.")
        return "\n".join(lines), "Оплатить с баланса"
    need = total - bal
    lines.append(
        f"Не хватает: {need} {STAR}. Пополните баланс на {need} {STAR} и сразу оплатите заказ: "
        f"после пополнения с баланса будет списано {total} {STAR}."
    )
    return "\n".join(lines), f"Пополнить на {need} {STAR} и оплатить"


def create_pending(uid: int, kind: str, data: dict, total: int, stars: int) -> str:
    pid = secrets.token_hex(8)
    ex(
        "INSERT INTO pending(id, user_id, kind, data, total, stars, status, created_at) "
        "VALUES(?,?,?,?,?,?, 'new', ?)",
        (pid, uid, kind, json.dumps(data, ensure_ascii=False), total, stars, now_ts()),
    )
    return pid


def private_available() -> bool:
    try:
        price = int(get_setting("private_price") or 0)
    except ValueError:
        price = 0
    return PRIVATE_GROUP_ID != 0 and price >= 1


def finalize_purchase(pid: str, uid: int, stars_paid: int, charge_id):
    """
    Атомарно оформляет заказ. Звёзды, оплаченные через счёт, сначала зачисляются на баланс,
    затем с баланса списывается сумма заказа. Если заказ оформить нельзя, звёзды остаются на балансе.
    """
    ts = now_ts()
    with tx():
        if charge_id:
            try:
                ex(
                    "INSERT INTO payments(charge_id, user_id, amount, created_at) VALUES(?,?,?,?)",
                    (charge_id, uid, stars_paid, ts),
                )
            except sqlite3.IntegrityError:
                return {"status": "dup"}
        p = q1("SELECT * FROM pending WHERE id=?", (pid,))
        if p is None or p["user_id"] != uid:
            if stars_paid > 0:
                ex("UPDATE users SET balance=balance+? WHERE id=?", (stars_paid, uid))
                log_tx(uid, "topup", stars_paid, "Платёж зачислен на баланс", charge_id)
            return {"status": "nopending"}
        if p["status"] != "new":
            if stars_paid > 0:
                ex("UPDATE users SET balance=balance+? WHERE id=?", (stars_paid, uid))
                log_tx(uid, "topup", stars_paid, "Платёж зачислен на баланс", charge_id)
            return {"status": "used"}
        ex("UPDATE pending SET status='paid' WHERE id=?", (pid,))
        data = json.loads(p["data"])
        total = p["total"]
        kind = p["kind"]
        if stars_paid > 0:
            ex("UPDATE users SET balance=balance+? WHERE id=?", (stars_paid, uid))
            log_tx(
                uid,
                "topup",
                stars_paid,
                "Пополнение баланса" if kind == "topup" else "Пополнение при оплате заказа",
                charge_id,
            )
        if kind == "topup":
            return {"status": "topup", "amount": stars_paid}
        bal = q1("SELECT balance FROM users WHERE id=?", (uid,))["balance"]
        if bal < total:
            return {"status": "nobalance"}
        bonus_id = data.get("bonus_id")
        if bonus_id:
            cur = ex(
                "UPDATE bonuses SET used=1, used_at=? WHERE id=? AND user_id=? AND used=0",
                (ts, bonus_id, uid),
            )
            if cur.rowcount != 1:
                return {"status": "bonus_used"}
        if total > 0:
            ex("UPDATE users SET balance=balance-? WHERE id=?", (total, uid))
        from_stars = min(stars_paid, total)
        cur = ex(
            "INSERT INTO orders(user_id, kind, total, from_balance, from_stars, charge_id, status, "
            "bonus_id, created_at) VALUES(?,?,?,?,?,?, 'active', ?, ?)",
            (uid, kind, total, total - from_stars, from_stars, charge_id, bonus_id, ts),
        )
        oid = cur.lastrowid
        if total > 0:
            what = "Оплата привата" if kind == "private" else "Оплата заказа"
            log_tx(uid, "order", -total, f"{what} #{oid}", str(oid))
        for it in data.get("items", []):
            ex(
                "INSERT INTO order_items(order_id, number, nick, price, base_price) VALUES(?,?,?,?,?)",
                (oid, it["number"], it["nick"], it["price"], it["base"]),
            )
            if data.get("from_cart"):
                ex("DELETE FROM cart WHERE user_id=? AND number=?", (uid, it["number"]))
        return {"status": "ok", "order_id": oid, "kind": kind}


async def notify_new_order(order_id: int) -> None:
    o = q1("SELECT * FROM orders WHERE id=?", (order_id,))
    if o is None:
        return
    items = qa("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,))
    await notify_admins(
        "<b>Новый заказ</b>\n\n" + order_text(o, items),
        KB([B("Открыть заказ", f"a_o:{order_id}")]),
    )


async def fulfill_private(order_id: int, by_admin: int | None = None):
    """Создаёт одноразовую ссылку в приват и отправляет покупателю. Возвращает (успех, описание)."""
    o = q1("SELECT * FROM orders WHERE id=?", (order_id,))
    if o is None or o["kind"] != "private" or o["status"] != "active":
        return False, "Заказ не найден или уже закрыт."
    if PRIVATE_GROUP_ID == 0:
        return False, "В настройках не указан PRIVATE_GROUP_ID."
    try:
        link = await bot.create_chat_invite_link(
            chat_id=PRIVATE_GROUP_ID, member_limit=1, name=f"Заказ {order_id}"
        )
    except Exception as err:
        log.error("create_chat_invite_link: %s", err)
        return False, f"Не удалось создать ссылку: {err}"
    url = link.invite_link
    text = (
        "Оплата получена. Ваша одноразовая ссылка в приват:\n"
        f"{url}\n\n"
        "Не передавайте её никому: ссылка одноразовая и сработает только для первого вошедшего.\n"
        "После входа обязательно прочитайте тему с правилами в привате."
    )
    if not await notify_user(o["user_id"], text):
        try:
            await bot.revoke_chat_invite_link(PRIVATE_GROUP_ID, url)
        except Exception:
            pass
        return False, "Не удалось отправить сообщение покупателю (возможно, он заблокировал бота)."
    ex(
        "UPDATE orders SET status='done', done_at=?, issued_by=?, invite_link=? "
        "WHERE id=? AND status='active'",
        (now_ts(), by_admin, url, order_id),
    )
    return True, "Ссылка создана и отправлена покупателю."


async def complete_purchase(pid: str, uid: int, stars_paid: int, charge_id) -> None:
    res = finalize_purchase(pid, uid, stars_paid, charge_id)
    st = res["status"]
    if st == "dup":
        return
    if st == "topup":
        await notify_user(
            uid,
            f"Баланс пополнен на {res['amount']} {STAR}. Сейчас на балансе: "
            f"<b>{get_balance(uid)}</b> {STAR}.",
            KB([B("Каталог", "cat")], [B("Личный кабинет", "cab")]),
        )
        return
    if st in ("nopending", "used"):
        await notify_user(
            uid,
            f"Платёж получен, {stars_paid} {STAR} зачислены на ваш баланс. "
            f"Сейчас на балансе: {get_balance(uid)} {STAR}. "
            f"Если заказ не оформился, оформите его заново.",
            main_kb(),
        )
        if st == "nopending":
            await notify_admins(
                f"Платёж на {stars_paid} {STAR} от пользователя ID <code>{uid}</code> "
                f"без соответствующего заказа. Сумма зачислена на его баланс."
            )
        return
    if st in ("nobalance", "bonus_used"):
        reasons = {
            "nobalance": "для оформления заказа сейчас не хватает звёзд на балансе",
            "bonus_used": "этот бонус уже использован",
        }
        extra = f" {stars_paid} {STAR} зачислены на ваш баланс." if stars_paid > 0 else ""
        extra += f" Сейчас на балансе: {get_balance(uid)} {STAR}."
        await notify_user(uid, f"Заказ не оформлен: {reasons[st]}.{extra}", main_kb())
        return

    order_id = res["order_id"]
    o = q1("SELECT * FROM orders WHERE id=?", (order_id,))
    if o["bonus_id"]:
        paid_note = "Бонус использован."
    else:
        paid_note = (
            f"Списано с баланса: {o['total']} {STAR}. Остаток на балансе: {get_balance(uid)} {STAR}."
        )
    if res["kind"] == "wm":
        await notify_user(
            uid,
            f"Заказ #{order_id} оформлен и оплачен. {paid_note}\n"
            f"Ожидайте выдачи: администратор отправит водянку в этом чате.",
            main_kb(),
        )
        await notify_new_order(order_id)
        return

    # приват
    await notify_user(uid, f"Заказ #{order_id} (приват) оплачен. {paid_note}")
    ok, info = await fulfill_private(order_id)
    buyer = q1("SELECT * FROM users WHERE id=?", (uid,))
    markup = KB([B("Открыть заказ", f"a_o:{order_id}")])
    if ok:
        await notify_admins(
            f"<b>Куплен приват</b>\nЗаказ #{order_id}, покупатель {user_ref(buyer)}, "
            f"ID <code>{uid}</code>.\nСумма: {o['total']} {STAR}. Одноразовая ссылка отправлена покупателю.",
            markup,
        )
    else:
        contact = await admin_contact()
        await notify_user(
            uid,
            f"Оплата получена, но ссылку не удалось создать автоматически. "
            f"Администратор отправит её вручную. При необходимости напишите {contact}.",
            main_kb(),
        )
        await notify_admins(
            f"<b>Куплен приват, но ссылка не отправлена</b>\nЗаказ #{order_id}, покупатель "
            f"{user_ref(buyer)}, ID <code>{uid}</code>.\nПричина: {esc(info)}\n"
            f"Откройте заказ и повторите выдачу.",
            markup,
        )


async def send_stars_invoice(uid: int, pid: str, stars: int, desc: str, title: str = "Оплата заказа") -> None:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Оплатить {stars} {STAR}", pay=True)
    await bot.send_invoice(
        chat_id=uid,
        title=title,
        description=desc[:250],
        payload=pid,
        provider_token=PROVIDER_TOKEN,
        currency="XTR",  # валюта Telegram Stars
        prices=[LabeledPrice(label=title, amount=stars)],
        reply_markup=builder.as_markup(),
    )


# ======================================================================
# Состояния FSM
# ======================================================================
class Buy(StatesGroup):
    nick = State()
    confirm = State()


class BonusPick(StatesGroup):
    nick = State()
    confirm = State()


class Search(StatesGroup):
    number = State()


class PromoInput(StatesGroup):
    code = State()


class AddProd(StatesGroup):
    number = State()
    price = State()
    media = State()


class EditProd(StatesGroup):
    number = State()
    choice = State()
    price = State()
    media = State()


class DelProd(StatesGroup):
    number = State()


class Issue(StatesGroup):
    files = State()


class Bcast(StatesGroup):
    content = State()
    menu = State()
    link_text = State()
    link_url = State()
    start_text = State()


class DiscNew(StatesGroup):
    percent = State()
    mode = State()
    numbers = State()
    start = State()
    end = State()


class DiscExt(StatesGroup):
    end = State()


class PromoNew(StatesGroup):
    code = State()
    kind = State()
    value = State()


class UserBlock(StatesGroup):
    target = State()


class RefPrice(StatesGroup):
    value = State()


class TopUp(StatesGroup):
    amount = State()


class PrivSet(StatesGroup):
    price = State()
    text = State()


# ======================================================================
# Хранилище состояний FSM в той же SQLite-базе (переживает перезапуск бота)
# ======================================================================
class SQLiteStorage(BaseStorage):
    @staticmethod
    def _k(key: StorageKey) -> str:
        return ":".join(
            str(getattr(key, f, None))
            for f in ("bot_id", "chat_id", "user_id", "thread_id", "business_connection_id", "destiny")
        )

    @staticmethod
    def _save(k: str, state, data_json: str) -> None:
        if state is None and data_json == "{}":
            ex("DELETE FROM fsm WHERE key=?", (k,))
        else:
            ex(
                "INSERT OR REPLACE INTO fsm(key, state, data, updated_at) VALUES(?,?,?,?)",
                (k, state, data_json, now_ts()),
            )

    async def set_state(self, key: StorageKey, state=None) -> None:
        value = state.state if isinstance(state, State) else state
        k = self._k(key)
        row = q1("SELECT data FROM fsm WHERE key=?", (k,))
        self._save(k, value, row["data"] if row else "{}")

    async def get_state(self, key: StorageKey):
        row = q1("SELECT state FROM fsm WHERE key=?", (self._k(key),))
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: dict) -> None:
        k = self._k(key)
        row = q1("SELECT state FROM fsm WHERE key=?", (k,))
        self._save(k, row["state"] if row else None, json.dumps(data, ensure_ascii=False, default=str))

    async def get_data(self, key: StorageKey) -> dict:
        row = q1("SELECT data FROM fsm WHERE key=?", (self._k(key),))
        return json.loads(row["data"]) if row else {}

    async def close(self) -> None:
        pass


# ======================================================================
# Доступ: блокировка пользователей и выключение бота
# ======================================================================
class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if isinstance(event, Message):
            if event.chat.type != "private":
                return None
            if event.successful_payment is not None:
                return await handler(event, data)  # платёж обрабатываем всегда
        if user is None:
            return await handler(event, data)
        is_start = isinstance(event, Message) and (event.text or "").startswith("/start")
        if not is_admin(user.id):
            row = q1("SELECT blocked FROM users WHERE id=?", (user.id,))
            if row is not None and row["blocked"]:
                await self.deny(event, user.id, blocked=True)
                return None
            if get_setting("bot_enabled") != "1":
                await self.deny(event, user.id, blocked=False)
                return None
        if not is_start and not isinstance(event, PreCheckoutQuery):
            ensure_user(user)
        return await handler(event, data)

    async def deny(self, event, uid: int, blocked: bool) -> None:
        if blocked:
            contact = await admin_contact()
            text = f"Вы заблокированы. Для уточнения обратитесь в поддержку: {contact}"
        else:
            text = "Бот временно отключён. Попробуйте позже."
        if isinstance(event, PreCheckoutQuery):
            try:
                await bot.answer_pre_checkout_query(
                    event.id, ok=False, error_message="Покупка сейчас недоступна."
                )
            except Exception:
                pass
            return
        if isinstance(event, CallbackQuery):
            await safe_answer(event)
        await notify_user(uid, text)


# ======================================================================
# Роутеры
# ======================================================================
cmd_router = Router()      # команды и платежи: обрабатываются первыми
admin_router = Router()    # админка
user_router = Router()     # покупатели

admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


# ----------------------------------------------------------------------
# Общие: /start, платежи
# ----------------------------------------------------------------------
async def handle_referral(m: Message, ref_id: int, existed: bool) -> None:
    uid = m.from_user.id
    if ref_id == uid:
        await m.answer("Это ваша собственная реферальная ссылка.")
        return
    referrer = q1("SELECT * FROM users WHERE id=?", (ref_id,))
    if referrer is None or referrer["blocked"]:
        return
    me = q1("SELECT * FROM users WHERE id=?", (uid,))
    who = user_ref(me)
    if existed:
        await m.answer(
            "Вы уже заходили в бота раньше, поэтому за ваше приглашение звёзды не начисляются."
        )
        await notify_user(
            ref_id,
            f"Пользователь {who} перешёл по вашей реферальной ссылке, но уже заходил в бота раньше. "
            f"Звёзды не начислены.",
        )
        return
    try:
        reward = int(get_setting("ref_reward") or 0)
    except ValueError:
        reward = 0
    with tx():
        ex("UPDATE users SET referrer_id=?, ref_bonus=? WHERE id=?", (ref_id, reward, uid))
        if reward > 0:
            ex("UPDATE users SET balance=balance+? WHERE id=?", (reward, ref_id))
            log_tx(ref_id, "referral", reward, "Бонус за приглашённого пользователя", str(uid))
    msg = f"По вашей реферальной ссылке в бота зашёл {who}."
    if reward > 0:
        msg += f" На баланс начислено {reward} {STAR}."
    await notify_user(ref_id, msg)


@cmd_router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    existed = user_exists(m.from_user.id)
    ensure_user(m.from_user)
    arg = (command.args or "").strip()
    if arg.startswith("ref") and arg[3:].isdigit():
        await handle_referral(m, int(arg[3:]), existed)
    await m.answer(WELCOME, reply_markup=main_kb())


@cmd_router.message(Command("support"))
async def cmd_support(m: Message):
    contact = await admin_contact()
    await m.answer(f"Техническая поддержка: напишите {contact}")


@cmd_router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    await m.answer(admin_text(), reply_markup=admin_kb())


@cmd_router.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    p = q1("SELECT * FROM pending WHERE id=?", (q.invoice_payload,))
    err = None
    if p is None or p["user_id"] != q.from_user.id:
        err = "Счёт не найден. Оформите заказ заново."
    elif p["status"] != "new" or now_ts() - p["created_at"] > PENDING_TTL:
        err = "Счёт устарел. Оформите заказ заново."
    elif q.total_amount != p["stars"] or q.currency != "XTR":
        err = "Сумма счёта не совпадает. Оформите заказ заново."
    try:
        if err:
            await bot.answer_pre_checkout_query(q.id, ok=False, error_message=err)
        else:
            await bot.answer_pre_checkout_query(q.id, ok=True)
    except Exception as e:
        log.warning("pre_checkout answer failed: %s", e)


@cmd_router.message(F.successful_payment)
async def on_paid(m: Message):
    sp = m.successful_payment
    uid = m.from_user.id
    pid = sp.invoice_payload
    await complete_purchase(pid, uid, int(sp.total_amount), sp.telegram_payment_charge_id)


# ======================================================================
# ПОКУПАТЕЛЬ
# ======================================================================
@user_router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await safe_answer(cb)


@user_router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    await show(cb, WELCOME, main_kb())


@user_router.callback_query(F.data == "bc_start")
async def cb_bc_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    await bot.send_message(cb.from_user.id, WELCOME, reply_markup=main_kb())


# ----------------------------- каталог --------------------------------
@user_router.callback_query(F.data == "cat")
async def cb_cat(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    rows = [[B("Водянки", "wl:1")]]
    if private_available():
        rows.append([B("Приват", "priv")])
    rows.append([B("Поиск по номеру", "search")])
    rows.append([B("Назад", "menu")])
    await show(cb, "<b>Каталог</b>\nВыберите раздел.", KB(*rows))


@user_router.callback_query(F.data.startswith("wl:"))
async def cb_wl(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    page = int(cb.data.split(":")[1])
    text, markup = catalog_view(
        page, "wm", "wl", "cat", "Каталог водянок", extra=[[B("Поиск по номеру", "search")]]
    )
    await show(cb, text, markup)


@user_router.callback_query(F.data.startswith("wm:"))
async def cb_wm(cb: CallbackQuery):
    number = int(cb.data.split(":")[1])
    await safe_answer(cb)
    await delete_cb_message(cb)
    await send_card(cb.from_user.id, number)


@user_router.callback_query(F.data == "search")
async def cb_search(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Search.number)
    await safe_answer(cb)
    await show(cb, "Введите номер водянки цифрами.", KB([B("Отмена", "cat")]))


@user_router.message(Search.number, F.text)
async def msg_search(m: Message, state: FSMContext):
    found = re.search(r"\d+", m.text or "")
    if not found:
        await m.answer("Введите номер цифрами, например 48.", reply_markup=KB([B("Отмена", "cat")]))
        return
    number = int(found.group())
    if q1("SELECT 1 FROM products WHERE number=?", (number,)) is None:
        await m.answer(
            f"Водянка №{number} не найдена. Введите другой номер.", reply_markup=KB([B("Отмена", "cat")])
        )
        return
    await state.clear()
    await send_card(m.from_user.id, number)


@user_router.callback_query(F.data == "priv")
async def cb_priv(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    if not private_available():
        await show(cb, "Приват сейчас недоступен.", KB([B("Назад", "cat")]))
        return
    price = int(get_setting("private_price"))
    text = f"<b>Приват</b>\n{esc(get_setting('private_text'))}\n\nЦена: <b>{price}</b> {STAR}"
    await show(cb, text, KB([B("Купить", "priv_buy")], [B("Назад", "cat")]))


@user_router.callback_query(F.data == "priv_buy")
async def cb_priv_buy(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    if not private_available():
        await show(cb, "Приват сейчас недоступен.", KB([B("Назад", "cat")]))
        return
    await start_checkout(cb.from_user.id, state, "private", [], False)


# ------------------------------ корзина -------------------------------
@user_router.callback_query(F.data.startswith("ca:"))
async def cb_cart_add(cb: CallbackQuery):
    number = int(cb.data.split(":")[1])
    if q1("SELECT 1 FROM products WHERE number=?", (number,)) is None:
        await safe_answer(cb, "Водянка недоступна.", True)
        return
    ex("INSERT OR IGNORE INTO cart(user_id, number) VALUES(?,?)", (cb.from_user.id, number))
    await safe_answer(cb, f"Водянка №{number} в корзине.")


@user_router.callback_query(F.data == "cart")
async def cb_cart(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    text, markup = cart_view(cb.from_user.id)
    await show(cb, text, markup)


@user_router.callback_query(F.data.startswith("cd:"))
async def cb_cart_del(cb: CallbackQuery):
    number = int(cb.data.split(":")[1])
    ex("DELETE FROM cart WHERE user_id=? AND number=?", (cb.from_user.id, number))
    await safe_answer(cb, "Убрано из корзины.")
    text, markup = cart_view(cb.from_user.id)
    await show(cb, text, markup)


@user_router.callback_query(F.data == "cclear")
async def cb_cart_clear(cb: CallbackQuery):
    ex("DELETE FROM cart WHERE user_id=?", (cb.from_user.id,))
    await safe_answer(cb, "Корзина очищена.")
    text, markup = cart_view(cb.from_user.id)
    await show(cb, text, markup)


@user_router.callback_query(F.data == "cbuy")
async def cb_cart_buy(cb: CallbackQuery, state: FSMContext):
    rows = qa(
        "SELECT p.number FROM cart c JOIN products p ON p.number=c.number "
        "WHERE c.user_id=? ORDER BY p.number",
        (cb.from_user.id,),
    )
    if not rows:
        await safe_answer(cb, "Корзина пуста.", True)
        return
    numbers = [r["number"] for r in rows]
    await state.clear()
    await state.set_state(Buy.nick)
    await state.update_data(kind="wm", items=numbers, idx=0, nicks={}, from_cart=True)
    await safe_answer(cb)
    await ask_nick(cb.from_user.id, numbers, 0)


# --------------------------- оформление -------------------------------
async def ask_nick(uid: int, items: list, idx: int) -> None:
    suffix = f" ({idx + 1} из {len(items)})" if len(items) > 1 else ""
    await bot.send_message(
        uid,
        f"Введите ник, который будет в водянке №{items[idx]}{suffix}.",
        reply_markup=KB([B("Отмена", "buy_cancel")]),
    )


@user_router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery, state: FSMContext):
    number = int(cb.data.split(":")[1])
    if q1("SELECT 1 FROM products WHERE number=?", (number,)) is None:
        await safe_answer(cb, "Водянка недоступна.", True)
        return
    await state.clear()
    await state.set_state(Buy.nick)
    await state.update_data(kind="wm", items=[number], idx=0, nicks={}, from_cart=False)
    await safe_answer(cb)
    await ask_nick(cb.from_user.id, [number], 0)


@user_router.callback_query(F.data == "buy_cancel")
async def cb_buy_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb, "Оформление отменено.")
    await show(cb, WELCOME, main_kb())


@user_router.message(Buy.nick, F.text)
async def msg_nick(m: Message, state: FSMContext):
    nick = (m.text or "").strip()
    if not nick or len(nick) > 64 or nick.startswith("/"):
        await m.answer(
            "Ник должен быть от 1 до 64 символов. Введите ник ещё раз.",
            reply_markup=KB([B("Отмена", "buy_cancel")]),
        )
        return
    data = await state.get_data()
    items = data.get("items")
    if not items:
        await state.clear()
        await m.answer("Сессия оформления устарела. Начните заново.", reply_markup=main_kb())
        return
    idx = data["idx"]
    nicks = data["nicks"]
    nicks[str(items[idx])] = nick
    idx += 1
    if idx < len(items):
        await state.update_data(idx=idx, nicks=nicks)
        await ask_nick(m.from_user.id, items, idx)
        return
    pairs = [[n, nicks[str(n)]] for n in items]
    await start_checkout(m.from_user.id, state, "wm", pairs, bool(data.get("from_cart")))


@user_router.message(Buy.nick)
async def msg_nick_other(m: Message):
    await m.answer("Отправьте ник текстом.", reply_markup=KB([B("Отмена", "buy_cancel")]))


async def start_checkout(uid: int, state: FSMContext, kind: str, pairs: list, from_cart: bool) -> None:
    quote = build_quote(kind, pairs)
    if quote is None:
        await state.clear()
        await bot.send_message(
            uid, "Одна из водянок больше недоступна. Начните заново.", reply_markup=main_kb()
        )
        return
    snap, total = quote
    if total < 1:
        await state.clear()
        await bot.send_message(uid, "Покупка сейчас недоступна.", reply_markup=main_kb())
        return
    text, btn_text = checkout_text(kind, snap, total, get_balance(uid))
    await state.set_state(Buy.confirm)
    await state.update_data(kind=kind, pairs=pairs, from_cart=from_cart, shown_total=total)
    await bot.send_message(uid, text, reply_markup=KB([B(btn_text, "pay")], [B("Отмена", "buy_cancel")]))


@user_router.callback_query(Buy.confirm, F.data == "pay")
async def cb_pay(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid = cb.from_user.id
    kind = data.get("kind")
    pairs = data.get("pairs", [])
    quote = build_quote(kind, pairs) if kind else None
    if quote is None:
        await state.clear()
        await safe_answer(cb, "Заказ недоступен. Начните заново.", True)
        return
    snap, total = quote
    if total != data.get("shown_total") or total < 1:
        await safe_answer(cb, "Цена изменилась. Проверьте заказ.", True)
        await start_checkout(uid, state, kind, pairs, bool(data.get("from_cart")))
        return
    if kind == "private" and not private_available():
        await state.clear()
        await safe_answer(cb, "Приват недоступен для покупки.", True)
        return
    bal = get_balance(uid)
    stars = max(total - bal, 0)
    payload = {"items": snap, "from_cart": bool(data.get("from_cart"))}
    pid = create_pending(uid, kind, payload, total, stars)
    await state.clear()
    await safe_answer(cb)
    if stars == 0:
        await complete_purchase(pid, uid, 0, None)
        return
    try:
        desc = (
            f"Стоимость заказа {total} {STAR}, на балансе {bal} {STAR}. Счёт на {stars} {STAR} "
            f"пополнит баланс, затем с него спишется {total} {STAR}."
        )
        await send_stars_invoice(uid, pid, stars, desc)
    except Exception as err:
        log.error("send_invoice failed: %s", err)
        await bot.send_message(
            uid, "Не удалось выставить счёт. Попробуйте позже или напишите администратору.",
            reply_markup=main_kb(),
        )


@user_router.callback_query(F.data == "pay")
async def cb_pay_stale(cb: CallbackQuery):
    await safe_answer(cb, "Оформление устарело. Начните заново.", True)


# --------------------------- личный кабинет ---------------------------
@user_router.callback_query(F.data == "cab")
async def cb_cab(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    uid = cb.from_user.id
    text = f"<b>Личный кабинет</b>\nВаш ID: <code>{uid}</code>\nБаланс: <b>{get_balance(uid)}</b> {STAR}"
    markup = KB(
        [B("Пополнить баланс", "topup"), B("История операций", "th:all:1")],
        [B("Мои заказы", "mo:1"), B("Бонусы", "bon")],
        [B("Реферальная система", "ref"), B("Ввести промокод", "promo")],
        [B("Назад", "menu")],
    )
    await show(cb, text, markup)


# ----------------------- пополнение баланса ---------------------------
async def start_topup(uid: int, amount: int, state: FSMContext) -> None:
    await state.clear()
    bal = get_balance(uid)
    pid = create_pending(uid, "topup", {}, 0, amount)
    desc = (
        f"Пополнение баланса на {amount} {STAR}. Сейчас на балансе {bal} {STAR}, "
        f"после оплаты будет {bal + amount} {STAR}."
    )
    try:
        await send_stars_invoice(uid, pid, amount, desc, title="Пополнение баланса")
    except Exception as err:
        log.error("topup invoice failed: %s", err)
        await bot.send_message(
            uid, "Не удалось выставить счёт. Попробуйте позже.", reply_markup=main_kb()
        )


@user_router.callback_query(F.data == "topup")
async def cb_topup(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(TopUp.amount)
    await safe_answer(cb)
    text = (
        f"<b>Пополнение баланса</b>\nНа балансе: <b>{get_balance(cb.from_user.id)}</b> {STAR}\n\n"
        f"Выберите сумму или отправьте своё число от 1 до {MAX_TOPUP} {STAR}."
    )
    await show(
        cb,
        text,
        KB(
            [B(f"50 {STAR}", "tu:50"), B(f"100 {STAR}", "tu:100")],
            [B(f"250 {STAR}", "tu:250"), B(f"500 {STAR}", "tu:500")],
            [B("Отмена", "cab")],
        ),
    )


@user_router.callback_query(F.data.startswith("tu:"))
async def cb_topup_quick(cb: CallbackQuery, state: FSMContext):
    amount = int(cb.data.split(":")[1])
    if not 1 <= amount <= MAX_TOPUP:
        await safe_answer(cb, "Недопустимая сумма.", True)
        return
    await safe_answer(cb)
    await start_topup(cb.from_user.id, amount, state)


@user_router.message(TopUp.amount, F.text)
async def msg_topup(m: Message, state: FSMContext):
    amount = parse_int(m.text, 1, MAX_TOPUP)
    if amount is None:
        await m.answer(
            f"Введите целое число от 1 до {MAX_TOPUP}.", reply_markup=KB([B("Отмена", "cab")])
        )
        return
    await start_topup(m.from_user.id, amount, state)


@user_router.message(TopUp.amount)
async def msg_topup_other(m: Message):
    await m.answer("Отправьте сумму числом.", reply_markup=KB([B("Отмена", "cab")]))


# ------------------------- история операций ---------------------------
TX_FILTERS = {"all": "", "in": " AND amount > 0", "out": " AND amount < 0"}
TX_TITLES = {"all": "все операции", "in": "пополнения и начисления", "out": "траты"}


def history_view(uid: int, flt: str, page: int):
    if flt not in TX_FILTERS:
        flt = "all"
    per = 10
    cond = TX_FILTERS[flt]
    total = q1(f"SELECT COUNT(*) AS c FROM transactions WHERE user_id=?{cond}", (uid,))["c"]
    pages = max(1, (total + per - 1) // per)
    page = max(1, min(page, pages))
    rows = qa(
        f"SELECT * FROM transactions WHERE user_id=?{cond} ORDER BY id DESC LIMIT ? OFFSET ?",
        (uid, per, (page - 1) * per),
    )
    sums = q1(
        "SELECT COALESCE(SUM(CASE WHEN amount>0 THEN amount END),0) AS i, "
        "COALESCE(SUM(CASE WHEN amount<0 THEN -amount END),0) AS o "
        "FROM transactions WHERE user_id=?",
        (uid,),
    )
    lines = [
        "<b>История операций</b>",
        f"Баланс: <b>{get_balance(uid)}</b> {STAR}",
        f"Всего поступило: {sums['i']} {STAR}, потрачено: {sums['o']} {STAR}",
        f"Показано: {TX_TITLES[flt]}",
        "",
    ]
    if not rows:
        lines.append("Операций пока нет.")
    for r in rows:
        sign = "+" if r["amount"] > 0 else "-"
        lines.append(
            f"{fmt_short(r['created_at'])}  <b>{sign}{abs(r['amount'])}</b> {STAR}  "
            f"{esc(r['description'])} (баланс: {r['balance_after']})"
        )
    kb_rows = [
        [
            B(("• " if flt == key else "") + name, f"th:{key}:1")
            for key, name in (("all", "Все"), ("in", "Пополнения"), ("out", "Траты"))
        ]
    ]
    if pages > 1:
        nav = []
        if page > 1:
            nav.append(B("‹ Назад", f"th:{flt}:{page - 1}"))
        nav.append(B(f"{page}/{pages}", "noop"))
        if page < pages:
            nav.append(B("Вперёд ›", f"th:{flt}:{page + 1}"))
        kb_rows.append(nav)
    kb_rows.append([B("Пополнить баланс", "topup")])
    kb_rows.append([B("Назад", "cab")])
    return "\n".join(lines), KB(*kb_rows)


@user_router.callback_query(F.data.startswith("th:"))
async def cb_history(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    _, flt, page = cb.data.split(":")
    text, markup = history_view(cb.from_user.id, flt, int(page))
    await show(cb, text, markup)


@user_router.callback_query(F.data.startswith("mo:"))
async def cb_my_orders(cb: CallbackQuery):
    await safe_answer(cb)
    uid = cb.from_user.id
    per = 8
    page = int(cb.data.split(":")[1])
    total = q1("SELECT COUNT(*) AS c FROM orders WHERE user_id=?", (uid,))["c"]
    pages = max(1, (total + per - 1) // per)
    page = max(1, min(page, pages))
    rows = qa(
        "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (uid, per, (page - 1) * per),
    )
    kb_rows = []
    for o in rows:
        what = "приват" if o["kind"] == "private" else "водянки"
        kb_rows.append([B(f"#{o['id']} · {what} · {STATUS_RU.get(o['status'], o['status'])}", f"mo_i:{o['id']}")])
    if pages > 1:
        nav = []
        if page > 1:
            nav.append(B("‹ Назад", f"mo:{page - 1}"))
        nav.append(B(f"{page}/{pages}", "noop"))
        if page < pages:
            nav.append(B("Вперёд ›", f"mo:{page + 1}"))
        kb_rows.append(nav)
    kb_rows.append([B("Назад", "cab")])
    text = "<b>Мои заказы</b>\n" + ("Выберите заказ." if rows else "У вас пока нет заказов.")
    await show(cb, text, KB(*kb_rows))


@user_router.callback_query(F.data.startswith("mo_i:"))
async def cb_my_order(cb: CallbackQuery):
    await safe_answer(cb)
    oid = int(cb.data.split(":")[1])
    o = q1("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, cb.from_user.id))
    if o is None:
        await show(cb, "Заказ не найден.", KB([B("Назад", "mo:1")]))
        return
    items = qa("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (oid,))
    await show(cb, user_order_text(o, items), KB([B("К заказам", "mo:1")]))


@user_router.callback_query(F.data == "ref")
async def cb_ref(cb: CallbackQuery):
    await safe_answer(cb)
    uid = cb.from_user.id
    stat = q1(
        "SELECT COUNT(*) AS c, COALESCE(SUM(ref_bonus),0) AS s FROM users WHERE referrer_id=?", (uid,)
    )
    reward = get_setting("ref_reward")
    link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}"
    text = (
        "<b>Реферальная система</b>\n"
        f"За каждого приглашённого пользователя вы получаете {reward} {STAR} на баланс. "
        "Пользователя можно пригласить только один раз, и только если он ещё не заходил в бота.\n\n"
        f"Приглашено: <b>{stat['c']}</b>\n"
        f"Заработано: <b>{stat['s']}</b> {STAR}\n\n"
        f"Ваша ссылка:\n<code>{link}</code>"
    )
    await show(cb, text, KB([B("Назад", "cab")]))


@user_router.callback_query(F.data == "promo")
async def cb_promo(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(PromoInput.code)
    await safe_answer(cb)
    await show(cb, "Введите промокод.", KB([B("Отмена", "cab")]))


@user_router.message(PromoInput.code, F.text)
async def msg_promo(m: Message, state: FSMContext):
    code = (m.text or "").strip().upper()
    uid = m.from_user.id
    promo = q1("SELECT * FROM promos WHERE code=?", (code,))
    if promo is None:
        await m.answer(
            "Промокод не найден. Проверьте его и введите ещё раз.", reply_markup=KB([B("Отмена", "cab")])
        )
        return
    try:
        with tx():
            ex("INSERT INTO promo_uses(code, user_id, used_at) VALUES(?,?,?)", (code, uid, now_ts()))
            if promo["kind"] == "stars":
                ex("UPDATE users SET balance=balance+? WHERE id=?", (promo["value"], uid))
                log_tx(uid, "promo", promo["value"], f"Промокод {code}", code)
            else:
                ex(
                    "INSERT INTO bonuses(user_id, source, created_at) VALUES(?,?,?)",
                    (uid, code, now_ts()),
                )
    except sqlite3.IntegrityError:
        await state.clear()
        await m.answer("Вы уже активировали этот промокод.", reply_markup=KB([B("В кабинет", "cab")]))
        return
    await state.clear()
    if promo["kind"] == "stars":
        text = f"Промокод активирован. На баланс начислено {promo['value']} {STAR}."
    else:
        text = (
            "Промокод активирован. Вам начислена бесплатная водянка. Она находится в разделе "
            "«Бонусы» личного кабинета. Бонус можно использовать один раз."
        )
    await m.answer(text, reply_markup=KB([B("В кабинет", "cab")]))


@user_router.message(PromoInput.code)
async def msg_promo_other(m: Message):
    await m.answer("Отправьте промокод текстом.", reply_markup=KB([B("Отмена", "cab")]))


# ------------------------------ бонусы --------------------------------
@user_router.callback_query(F.data == "bon")
async def cb_bonuses(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    rows = qa("SELECT * FROM bonuses WHERE user_id=? AND used=0 ORDER BY id", (cb.from_user.id,))
    if not rows:
        await show(cb, "<b>Бонусы</b>\nУ вас нет доступных бонусов.", KB([B("Назад", "cab")]))
        return
    kb_rows = [
        [B(f"Бесплатная водянка (промокод {r['source']})", f"bu:{r['id']}")] for r in rows[:20]
    ]
    kb_rows.append([B("Назад", "cab")])
    await show(
        cb,
        "<b>Бонусы</b>\nВыберите бонус, чтобы получить бесплатную водянку. Каждый бонус можно использовать один раз.",
        KB(*kb_rows),
    )


def bonus_ok(bonus_id: int, uid: int):
    return q1("SELECT * FROM bonuses WHERE id=? AND user_id=? AND used=0", (bonus_id, uid))


@user_router.callback_query(F.data.startswith("bu:"))
async def cb_bonus_use(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    bid = int(cb.data.split(":")[1])
    if bonus_ok(bid, cb.from_user.id) is None:
        await safe_answer(cb, "Бонус недоступен.", True)
        return
    await safe_answer(cb)
    text, markup = catalog_view(1, f"bpk:{bid}", f"bpg:{bid}", "bon", "Выбор бесплатной водянки")
    await show(cb, text, markup)


@user_router.callback_query(F.data.startswith("bpg:"))
async def cb_bonus_page(cb: CallbackQuery):
    _, bid, page = cb.data.split(":")
    bid, page = int(bid), int(page)
    if bonus_ok(bid, cb.from_user.id) is None:
        await safe_answer(cb, "Бонус недоступен.", True)
        return
    await safe_answer(cb)
    text, markup = catalog_view(page, f"bpk:{bid}", f"bpg:{bid}", "bon", "Выбор бесплатной водянки")
    await show(cb, text, markup)


@user_router.callback_query(F.data.startswith("bpk:"))
async def cb_bonus_card(cb: CallbackQuery):
    _, bid, number = cb.data.split(":")
    bid, number = int(bid), int(number)
    if bonus_ok(bid, cb.from_user.id) is None:
        await safe_answer(cb, "Бонус недоступен.", True)
        return
    await safe_answer(cb)
    await delete_cb_message(cb)
    await send_card(cb.from_user.id, number, mode="bonus", bonus_id=bid)


@user_router.callback_query(F.data.startswith("bsel:"))
async def cb_bonus_select(cb: CallbackQuery, state: FSMContext):
    _, bid, number = cb.data.split(":")
    bid, number = int(bid), int(number)
    if bonus_ok(bid, cb.from_user.id) is None:
        await safe_answer(cb, "Бонус недоступен.", True)
        return
    if q1("SELECT 1 FROM products WHERE number=?", (number,)) is None:
        await safe_answer(cb, "Водянка недоступна.", True)
        return
    await state.clear()
    await state.set_state(BonusPick.nick)
    await state.update_data(bonus_id=bid, number=number)
    await safe_answer(cb)
    await bot.send_message(
        cb.from_user.id,
        f"Введите ник, который будет в водянке №{number}.",
        reply_markup=KB([B("Отмена", "bonus_cancel")]),
    )


@user_router.callback_query(F.data == "bonus_cancel")
async def cb_bonus_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    await show(cb, "Выбор отменён.", KB([B("К бонусам", "bon")]))


@user_router.message(BonusPick.nick, F.text)
async def msg_bonus_nick(m: Message, state: FSMContext):
    nick = (m.text or "").strip()
    if not nick or len(nick) > 64 or nick.startswith("/"):
        await m.answer(
            "Ник должен быть от 1 до 64 символов. Введите ник ещё раз.",
            reply_markup=KB([B("Отмена", "bonus_cancel")]),
        )
        return
    data = await state.get_data()
    if "number" not in data:
        await state.clear()
        await m.answer("Сессия устарела. Начните заново.", reply_markup=main_kb())
        return
    await state.update_data(nick=nick)
    await state.set_state(BonusPick.confirm)
    await m.answer(
        f"Использовать бонус: водянка №{data['number']}, ник <code>{esc(nick)}</code>.\n"
        f"Бонус будет израсходован.",
        reply_markup=KB([B("Подтвердить", "bconf")], [B("Отмена", "bonus_cancel")]),
    )


@user_router.message(BonusPick.nick)
async def msg_bonus_nick_other(m: Message):
    await m.answer("Отправьте ник текстом.", reply_markup=KB([B("Отмена", "bonus_cancel")]))


@user_router.callback_query(BonusPick.confirm, F.data == "bconf")
async def cb_bonus_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid = cb.from_user.id
    bid, number, nick = data.get("bonus_id"), data.get("number"), data.get("nick")
    p = q1("SELECT price FROM products WHERE number=?", (number,))
    if bid is None or p is None or bonus_ok(bid, uid) is None:
        await state.clear()
        await safe_answer(cb, "Бонус или водянка недоступны.", True)
        return
    snap = [{"number": number, "nick": nick, "price": 0, "base": p["price"]}]
    pid = create_pending(uid, "wm", {"items": snap, "from_cart": False, "bonus_id": bid}, 0, 0)
    await state.clear()
    await safe_answer(cb)
    await complete_purchase(pid, uid, 0, None)


# ------------------ отзывы и прочие сообщения (последний обработчик) ---
@user_router.message(StateFilter(None))
async def msg_fallback(m: Message):
    uid = m.from_user.id
    text = m.text or m.caption or ""
    if text.startswith("/"):
        return
    since = now_ts() - REVIEW_WINDOW_HOURS * 3600
    o = q1(
        "SELECT * FROM orders WHERE user_id=? AND review_requested_at IS NOT NULL AND review_done=0 "
        "AND review_requested_at>? ORDER BY review_requested_at DESC LIMIT 1",
        (uid, since),
    )
    if o is not None:
        if f"@{BOT_USERNAME}".lower() not in text.lower():
            await m.answer(
                f"Чтобы мы приняли отзыв, в его тексте должен быть юзернейм бота @{BOT_USERNAME}. "
                f"Отправьте отзыв заново одним сообщением."
            )
            return
        try:
            await bot.forward_message(
                chat_id=ADMIN_IDS[0], from_chat_id=m.chat.id, message_id=m.message_id
            )
        except Exception as err:
            log.error("Не удалось переслать отзыв: %s", err)
            await m.answer("Не удалось отправить отзыв. Попробуйте ещё раз чуть позже.")
            return
        ex("UPDATE orders SET review_done=1 WHERE id=?", (o["id"],))
        await m.answer("Спасибо за отзыв!", reply_markup=main_kb())
        return
    if m.text and not is_admin(uid):
        await m.answer("Выберите действие в меню.", reply_markup=main_kb())


# ======================================================================
# АДМИН-ПАНЕЛЬ
# ======================================================================
issuing_orders: set = set()   # заказы, которые выдаются прямо сейчас
album_buffers: dict = {}      # (id админа, media_group_id) -> id сообщений альбома
ALBUM_WAIT = 1.5              # сколько секунд ждём остальные части альбома
background_tasks: set = set()


def admin_text() -> str:
    enabled = get_setting("bot_enabled") == "1"
    active = q1("SELECT COUNT(*) AS c FROM orders WHERE status='active'")["c"]
    return (
        "<b>Админ-панель</b>\n"
        f"Бот для пользователей: {'включён' if enabled else 'выключен'}\n"
        f"Активных заказов: {active}"
    )


def admin_kb() -> InlineKeyboardMarkup:
    enabled = get_setting("bot_enabled") == "1"
    return KB(
        [B("Активные заказы", "a_ord:1"), B("История заказов", "a_hist:1")],
        [B("Товары", "a_prod"), B("Приват", "a_priv")],
        [B("Акции", "a_disc"), B("Промокоды", "a_promo")],
        [B("Рассылка", "a_bc"), B("Статистика", "a_stat")],
        [B("Блокировка пользователей", "a_usr"), B("Цена за реферала", "a_ref")],
        [
            B("Бот: включён (выключить)" if enabled else "Бот: выключен (включить)", "a_tog"),
            B("Резервная копия", "a_bk"),
        ],
    )


def back_admin() -> InlineKeyboardMarkup:
    return KB([B("В админ-панель", "a_menu")])


def parse_int(text: str, low: int = 1, high: int = 10 ** 9):
    t = (text or "").strip()
    if not t.isdigit():
        return None
    v = int(t)
    return v if low <= v <= high else None


@admin_router.callback_query(F.data == "a_menu")
async def a_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    await show(cb, admin_text(), admin_kb())


@admin_router.callback_query(F.data == "a_tog")
async def a_toggle(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    enabled = get_setting("bot_enabled") == "1"
    set_setting("bot_enabled", "0" if enabled else "1")
    await safe_answer(cb, "Бот выключен для пользователей." if enabled else "Бот включён для всех.")
    await show(cb, admin_text(), admin_kb())


@admin_router.callback_query(F.data == "a_bk")
async def a_backup(cb: CallbackQuery):
    await safe_answer(cb, "Отправляю копию базы.")
    await send_backup("Резервная копия по запросу")


# ----------------------------- заказы ---------------------------------
def orders_view(status: str, page: int, prefix: str, title: str):
    total = q1("SELECT COUNT(*) AS c FROM orders WHERE status=?", (status,))["c"]
    pages = max(1, (total + ORDERS_PAGE_SIZE - 1) // ORDERS_PAGE_SIZE)
    page = max(1, min(page, pages))
    order = "ASC" if status == "active" else "DESC"
    rows = qa(
        f"SELECT * FROM orders WHERE status=? ORDER BY id {order} LIMIT ? OFFSET ?",
        (status, ORDERS_PAGE_SIZE, (page - 1) * ORDERS_PAGE_SIZE),
    )
    kb_rows = []
    for o in rows:
        u = q1("SELECT * FROM users WHERE id=?", (o["user_id"],))
        if o["kind"] == "private":
            what = "приват"
        else:
            cnt = q1("SELECT COUNT(*) AS c FROM order_items WHERE order_id=?", (o["id"],))["c"]
            what = f"{cnt} шт."
        kb_rows.append([B(f"#{o['id']} · {user_short(u)} · {what} · {o['total']}{STAR}", f"a_o:{o['id']}")])
    if pages > 1:
        nav = []
        if page > 1:
            nav.append(B("‹ Назад", f"{prefix}:{page - 1}"))
        nav.append(B(f"{page}/{pages}", "noop"))
        if page < pages:
            nav.append(B("Вперёд ›", f"{prefix}:{page + 1}"))
        kb_rows.append(nav)
    kb_rows.append([B("В админ-панель", "a_menu")])
    text = f"<b>{title}</b>\nВсего: {total}. Страница {page} из {pages}."
    if total == 0:
        text = f"<b>{title}</b>\nПока пусто."
    return text, KB(*kb_rows)


@admin_router.callback_query(F.data.startswith("a_ord:"))
async def a_orders(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    text, markup = orders_view("active", int(cb.data.split(":")[1]), "a_ord", "Активные заказы")
    await show(cb, text, markup)


@admin_router.callback_query(F.data.startswith("a_hist:"))
async def a_history(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    text, markup = orders_view("done", int(cb.data.split(":")[1]), "a_hist", "История выполненных заказов")
    await show(cb, text, markup)


@admin_router.callback_query(F.data.startswith("a_o:"))
async def a_order(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    oid = int(cb.data.split(":")[1])
    o = q1("SELECT * FROM orders WHERE id=?", (oid,))
    if o is None:
        await show(cb, "Заказ не найден.", back_admin())
        return
    items = qa("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (oid,))
    if o["status"] == "active":
        give_text = "Создать и отправить ссылку" if o["kind"] == "private" else "Выдать"
        rows = [
            [B(give_text, f"a_give:{oid}"), B("Отменить", f"a_cx:{oid}")],
            [B("К активным заказам", "a_ord:1")],
        ]
    elif o["status"] == "done":
        rows = [[B("К истории", "a_hist:1")]]
    else:
        rows = [[B("К активным заказам", "a_ord:1")]]
    await show(cb, order_text(o, items), KB(*rows))


@admin_router.callback_query(F.data.startswith("a_cx:"))
async def a_cancel1(cb: CallbackQuery):
    await safe_answer(cb)
    oid = int(cb.data.split(":")[1])
    await show(
        cb,
        f"Отменить заказ #{oid}? Покупатель получит уведомление об отмене.",
        KB([B("Да, отменить", f"a_cx2:{oid}")], [B("Нет, назад", f"a_o:{oid}")]),
    )


@admin_router.callback_query(F.data.startswith("a_cx2:"))
async def a_cancel2(cb: CallbackQuery):
    await safe_answer(cb)
    oid = int(cb.data.split(":")[1])
    await show(
        cb,
        f"Подтвердите ещё раз: отменить заказ #{oid}? Это действие нельзя отменить, "
        f"звёзды автоматически не возвращаются.",
        KB([B("Подтвердить отмену", f"a_cx3:{oid}")], [B("Нет, назад", f"a_o:{oid}")]),
    )


@admin_router.callback_query(F.data.startswith("a_cx3:"))
async def a_cancel3(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    if oid in issuing_orders:
        await safe_answer(cb, "Заказ сейчас выдаётся, подождите.", True)
        return
    cur = ex(
        "UPDATE orders SET status='cancelled', done_at=?, issued_by=? WHERE id=? AND status='active'",
        (now_ts(), cb.from_user.id, oid),
    )
    if cur.rowcount != 1:
        await safe_answer(cb, "Заказ уже закрыт.", True)
        await show(cb, "Заказ уже закрыт или не найден.", KB([B("К активным заказам", "a_ord:1")]))
        return
    await safe_answer(cb)
    o = q1("SELECT * FROM orders WHERE id=?", (oid,))
    contact = await admin_contact(cb.from_user.id)
    await notify_user(
        o["user_id"],
        f"Ваш заказ #{oid} отменён. Для уточнения напишите администратору: {contact}",
        main_kb(),
    )
    await show(cb, f"Заказ #{oid} отменён, покупатель уведомлён.", KB([B("К активным заказам", "a_ord:1")]))


@admin_router.callback_query(F.data.startswith("a_give:"))
async def a_give(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    oid = int(cb.data.split(":")[1])
    o = q1("SELECT * FROM orders WHERE id=?", (oid,))
    if o is None or o["status"] != "active":
        await safe_answer(cb, "Заказ уже закрыт.", True)
        return
    await safe_answer(cb)
    if o["kind"] == "private":
        ok, info = await fulfill_private(oid, cb.from_user.id)
        await show(cb, info, KB([B("К активным заказам", "a_ord:1")]))
        return
    if oid in issuing_orders:
        await show(cb, "Этот заказ сейчас выдаёт другой администратор.", KB([B("К активным заказам", "a_ord:1")]))
        return
    need = q1("SELECT COUNT(*) AS c FROM order_items WHERE order_id=?", (oid,))["c"]
    await state.set_state(Issue.files)
    await state.update_data(order_id=oid)
    await show(
        cb,
        f"Выдача заказа #{oid} (позиций: {need}). Отправьте боту все файлы одним сообщением (альбомом). "
        f"Бот перешлёт их покупателю, сообщит о выдаче и попросит оставить отзыв.",
        KB([B("Отмена выдачи", f"a_o:{oid}")]),
    )


@admin_router.message(Issue.files)
async def msg_issue(m: Message, state: FSMContext):
    if extract_media(m) is None:
        await m.answer("Отправьте фото, видео, GIF или файл (можно альбомом).")
        return
    if m.media_group_id:
        # Альбом приходит отдельными сообщениями: первое ждёт остальные, остальные просто добавляются.
        key = (m.from_user.id, m.media_group_id)
        if key in album_buffers:
            album_buffers[key].append(m.message_id)
            return
        album_buffers[key] = [m.message_id]
        await asyncio.sleep(ALBUM_WAIT)
        msg_ids = sorted(album_buffers.pop(key))
    else:
        msg_ids = [m.message_id]
    data = await state.get_data()
    order_id = data.get("order_id")
    if order_id is None:
        return  # выдачу отменили, пока шёл альбом
    await state.clear()
    await deliver_order(order_id, m.chat.id, msg_ids, m)


async def copy_to_buyer(buyer: int, src_chat: int, msg_ids: list):
    """Копирует сообщения админа покупателю (без пометки «переслано», с подписями).
    Возвращает (сколько доставлено, ошибка или None)."""
    if len(msg_ids) > 1 and hasattr(bot, "copy_messages"):
        try:
            res = await bot.copy_messages(chat_id=buyer, from_chat_id=src_chat, message_ids=msg_ids)
            n = len(res)
            return n, (None if n == len(msg_ids) else RuntimeError(f"скопировано {n} из {len(msg_ids)}"))
        except TelegramForbiddenError as err:
            return 0, err
        except Exception as err:
            log.warning("copy_messages не сработал, копирую по одному: %s", err)
    delivered = 0
    for mid in msg_ids:
        for _ in range(2):
            try:
                await bot.copy_message(chat_id=buyer, from_chat_id=src_chat, message_id=mid)
                delivered += 1
                break
            except TelegramRetryAfter as err:
                await asyncio.sleep(err.retry_after + 1)
            except Exception as err:
                return delivered, err
        else:
            return delivered, RuntimeError("Telegram просит подождать, попробуйте чуть позже")
    return delivered, None


async def deliver_order(order_id: int, src_chat: int, msg_ids: list, m: Message) -> None:
    admin_id = m.from_user.id
    back = KB([B("К активным заказам", "a_ord:1")])
    if order_id in issuing_orders:
        await m.answer("Этот заказ сейчас выдаёт другой администратор.", reply_markup=back)
        return
    o = q1("SELECT * FROM orders WHERE id=?", (order_id,))
    if o is None or o["status"] != "active" or o["kind"] != "wm":
        await m.answer("Заказ уже закрыт или не найден.", reply_markup=back_admin())
        return
    buyer = o["user_id"]
    issuing_orders.add(order_id)
    try:
        delivered, err = await copy_to_buyer(buyer, src_chat, msg_ids)
        if err is None:
            ts = now_ts()
            cur = ex(
                "UPDATE orders SET status='done', done_at=?, issued_by=?, review_requested_at=? "
                "WHERE id=? AND status='active'",
                (ts, admin_id, ts, order_id),
            )
    finally:
        issuing_orders.discard(order_id)
    if err is not None:
        log.error("deliver_order #%s: доставлено %s из %s: %s", order_id, delivered, len(msg_ids), err)
        if isinstance(err, TelegramForbiddenError):
            text = "Не удалось отправить: покупатель заблокировал бота. Заказ остался активным."
        elif delivered:
            text = (
                f"Доставлено {delivered} из {len(msg_ids)} сообщений, затем ошибка: {esc(str(err))}. "
                f"Заказ остался активным. Выдайте его снова и отправьте только недостающие файлы, "
                f"чтобы покупатель не получил дубли."
            )
        else:
            text = f"Ошибка отправки: {esc(str(err))}. Заказ остался активным, попробуйте выдать снова."
        await m.answer(text, reply_markup=back)
        return
    if cur.rowcount != 1:
        await m.answer(
            "Файлы покупателю отправлены, но заказ к этому моменту уже был закрыт (отмена или другая выдача). "
            "Проверьте заказ.",
            reply_markup=back,
        )
        return
    await notify_user(
        buyer,
        f"Ваш заказ #{order_id} выдан. Спасибо за покупку!\n\n"
        f"Оставьте, пожалуйста, отзыв одним сообщением (текст, фото или видео). "
        f"В тексте обязательно укажите @{BOT_USERNAME}. "
        f"Отзыв нужно отправить в течение {REVIEW_WINDOW_HOURS} ч.",
        main_kb(),
    )
    await m.answer(f"Заказ #{order_id} выдан покупателю и перенесён в выполненные.", reply_markup=back)


# ------------------------------ товары --------------------------------
@admin_router.callback_query(F.data == "a_prod")
async def a_prod(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    total = q1("SELECT COUNT(*) AS c FROM products")["c"]
    await show(
        cb,
        f"<b>Товары</b>\nВсего водянок: {total}",
        KB(
            [B("Добавить водянку", "a_padd"), B("Редактировать", "a_pedit")],
            [B("Удалить водянку", "a_pdel"), B("Список товаров", "a_plist:1")],
            [B("В админ-панель", "a_menu")],
        ),
    )


@admin_router.callback_query(F.data.startswith("a_plist:"))
async def a_prod_list(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    page = int(cb.data.split(":")[1])
    per = 30
    total = q1("SELECT COUNT(*) AS c FROM products")["c"]
    pages = max(1, (total + per - 1) // per)
    page = max(1, min(page, pages))
    rows = qa("SELECT number, price FROM products ORDER BY number LIMIT ? OFFSET ?", (per, (page - 1) * per))
    lines = [f"<b>Список товаров</b> (стр. {page}/{pages})"]
    if not rows:
        lines.append("Пока пусто.")
    for r in rows:
        lines.append(f"Водянка №{r['number']} - {r['price']} {STAR}")
    nav = []
    if page > 1:
        nav.append(B("‹ Назад", f"a_plist:{page - 1}"))
    if page < pages:
        nav.append(B("Вперёд ›", f"a_plist:{page + 1}"))
    kb_rows = [nav] if nav else []
    kb_rows.append([B("К товарам", "a_prod")])
    await show(cb, "\n".join(lines), KB(*kb_rows))


PROD_MEDIA_TYPES = ("photo", "video", "animation", "document")


@admin_router.callback_query(F.data == "a_padd")
async def a_prod_add(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddProd.number)
    await safe_answer(cb)
    await show(cb, "Введите номер новой водянки (число).", KB([B("Отмена", "a_prod")]))


@admin_router.message(AddProd.number, F.text)
async def a_prod_add_number(m: Message, state: FSMContext):
    number = parse_int(m.text)
    if number is None:
        await m.answer("Введите номер числом.", reply_markup=KB([B("Отмена", "a_prod")]))
        return
    if q1("SELECT 1 FROM products WHERE number=?", (number,)) is not None:
        await m.answer(
            f"Водянка №{number} уже есть. Введите другой номер.", reply_markup=KB([B("Отмена", "a_prod")])
        )
        return
    await state.update_data(number=number)
    await state.set_state(AddProd.price)
    await m.answer("Введите цену в звёздах (целое число).", reply_markup=KB([B("Отмена", "a_prod")]))


@admin_router.message(AddProd.price, F.text)
async def a_prod_add_price(m: Message, state: FSMContext):
    price = parse_int(m.text, 1, 100000)
    if price is None:
        await m.answer("Введите цену целым числом от 1.", reply_markup=KB([B("Отмена", "a_prod")]))
        return
    await state.update_data(price=price)
    await state.set_state(AddProd.media)
    await m.answer("Отправьте фото, видео или GIF водянки.", reply_markup=KB([B("Отмена", "a_prod")]))


@admin_router.message(AddProd.media)
async def a_prod_add_media(m: Message, state: FSMContext):
    media = extract_media(m)
    if media is None or media[0] not in PROD_MEDIA_TYPES:
        await m.answer("Нужно фото, видео, GIF или файл.", reply_markup=KB([B("Отмена", "a_prod")]))
        return
    data = await state.get_data()
    if "number" not in data or "price" not in data:
        await state.clear()
        await m.answer("Сессия устарела. Начните заново.", reply_markup=KB([B("К товарам", "a_prod")]))
        return
    try:
        ex(
            "INSERT INTO products(number, price, media_type, file_id, created_at) VALUES(?,?,?,?,?)",
            (data["number"], data["price"], media[0], media[1], now_ts()),
        )
    except sqlite3.IntegrityError:
        await state.clear()
        await m.answer("Такая водянка уже существует.", reply_markup=KB([B("К товарам", "a_prod")]))
        return
    await state.clear()
    await m.answer(
        f"Водянка №{data['number']} добавлена. Цена: {data['price']} {STAR}.",
        reply_markup=KB([B("Добавить ещё", "a_padd")], [B("К товарам", "a_prod")]),
    )


@admin_router.callback_query(F.data == "a_pedit")
async def a_prod_edit(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(EditProd.number)
    await safe_answer(cb)
    await show(cb, "Введите номер водянки, которую нужно изменить.", KB([B("Отмена", "a_prod")]))


@admin_router.message(EditProd.number, F.text)
async def a_prod_edit_number(m: Message, state: FSMContext):
    number = parse_int(m.text)
    p = q1("SELECT * FROM products WHERE number=?", (number,)) if number is not None else None
    if p is None:
        await m.answer("Водянка с таким номером не найдена. Введите номер ещё раз.", reply_markup=KB([B("Отмена", "a_prod")]))
        return
    await state.update_data(number=number)
    await state.set_state(EditProd.choice)
    await m.answer(
        f"Водянка №{number}, цена {p['price']} {STAR}. Что изменить?",
        reply_markup=KB(
            [B("Изменить цену", "a_pe_price"), B("Изменить медиа", "a_pe_media")],
            [B("Отмена", "a_prod")],
        ),
    )


@admin_router.callback_query(EditProd.choice, F.data == "a_pe_price")
async def a_prod_edit_choose_price(cb: CallbackQuery, state: FSMContext):
    await state.set_state(EditProd.price)
    await safe_answer(cb)
    await show(cb, "Введите новую цену в звёздах.", KB([B("Отмена", "a_prod")]))


@admin_router.callback_query(EditProd.choice, F.data == "a_pe_media")
async def a_prod_edit_choose_media(cb: CallbackQuery, state: FSMContext):
    await state.set_state(EditProd.media)
    await safe_answer(cb)
    await show(cb, "Отправьте новое фото, видео или GIF.", KB([B("Отмена", "a_prod")]))


@admin_router.message(EditProd.price, F.text)
async def a_prod_edit_price(m: Message, state: FSMContext):
    price = parse_int(m.text, 1, 100000)
    if price is None:
        await m.answer("Введите цену целым числом от 1.", reply_markup=KB([B("Отмена", "a_prod")]))
        return
    data = await state.get_data()
    ex("UPDATE products SET price=? WHERE number=?", (price, data.get("number")))
    await state.clear()
    await m.answer(
        f"Цена водянки №{data.get('number')} изменена: {price} {STAR}.", reply_markup=KB([B("К товарам", "a_prod")])
    )


@admin_router.message(EditProd.media)
async def a_prod_edit_media(m: Message, state: FSMContext):
    media = extract_media(m)
    if media is None or media[0] not in PROD_MEDIA_TYPES:
        await m.answer("Нужно фото, видео, GIF или файл.", reply_markup=KB([B("Отмена", "a_prod")]))
        return
    data = await state.get_data()
    ex("UPDATE products SET media_type=?, file_id=? WHERE number=?", (media[0], media[1], data.get("number")))
    await state.clear()
    await m.answer(
        f"Медиа водянки №{data.get('number')} обновлено.", reply_markup=KB([B("К товарам", "a_prod")])
    )


@admin_router.callback_query(F.data == "a_pdel")
async def a_prod_delete(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(DelProd.number)
    await safe_answer(cb)
    await show(cb, "Введите номер водянки, которую нужно удалить.", KB([B("Отмена", "a_prod")]))


@admin_router.message(DelProd.number, F.text)
async def a_prod_delete_number(m: Message, state: FSMContext):
    number = parse_int(m.text)
    if number is None or q1("SELECT 1 FROM products WHERE number=?", (number,)) is None:
        await m.answer("Водянка с таким номером не найдена. Введите номер ещё раз.", reply_markup=KB([B("Отмена", "a_prod")]))
        return
    await state.clear()
    await m.answer(
        f"Удалить водянку №{number}? Она исчезнет из каталога и корзин. Старые заказы сохранятся.",
        reply_markup=KB([B("Да, удалить", f"a_pdel_ok:{number}")], [B("Нет", "a_prod")]),
    )


@admin_router.callback_query(F.data.startswith("a_pdel_ok:"))
async def a_prod_delete_ok(cb: CallbackQuery):
    number = int(cb.data.split(":")[1])
    with tx():
        ex("DELETE FROM products WHERE number=?", (number,))
        ex("DELETE FROM cart WHERE number=?", (number,))
    await safe_answer(cb)
    await show(cb, f"Водянка №{number} удалена.", KB([B("К товарам", "a_prod")]))


# ------------------------------ приват --------------------------------
@admin_router.callback_query(F.data == "a_priv")
async def a_priv(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    price = int(get_setting("private_price") or 0)
    group = "указан" if PRIVATE_GROUP_ID != 0 else "НЕ указан (PRIVATE_GROUP_ID в начале файла)"
    sale = "в продаже" if private_available() else "не в продаже (нужны группа и цена от 1)"
    text = (
        "<b>Приват</b>\n"
        f"Цена: {price} {STAR}\n"
        f"Описание: {esc(get_setting('private_text'))}\n"
        f"Группа: {group}\n"
        f"Статус: {sale}"
    )
    await show(
        cb,
        text,
        KB(
            [B("Изменить цену", "a_priv_price"), B("Изменить описание", "a_priv_text")],
            [B("Проверить доступ бота в группе", "a_priv_check")],
            [B("В админ-панель", "a_menu")],
        ),
    )


@admin_router.callback_query(F.data == "a_priv_price")
async def a_priv_price(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(PrivSet.price)
    await safe_answer(cb)
    await show(cb, "Введите цену привата в звёздах (целое число от 1).", KB([B("Отмена", "a_priv")]))


@admin_router.message(PrivSet.price, F.text)
async def a_priv_price_set(m: Message, state: FSMContext):
    price = parse_int(m.text, 1, 100000)
    if price is None:
        await m.answer("Введите цену целым числом от 1.", reply_markup=KB([B("Отмена", "a_priv")]))
        return
    set_setting("private_price", price)
    await state.clear()
    await m.answer(f"Цена привата: {price} {STAR}.", reply_markup=KB([B("К привату", "a_priv")]))


@admin_router.callback_query(F.data == "a_priv_text")
async def a_priv_text(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(PrivSet.text)
    await safe_answer(cb)
    await show(cb, "Отправьте новое описание привата (обычный текст).", KB([B("Отмена", "a_priv")]))


@admin_router.message(PrivSet.text, F.text)
async def a_priv_text_set(m: Message, state: FSMContext):
    text = (m.text or "").strip()
    if not text or len(text) > 1500:
        await m.answer("Описание должно быть от 1 до 1500 символов.", reply_markup=KB([B("Отмена", "a_priv")]))
        return
    set_setting("private_text", text)
    await state.clear()
    await m.answer("Описание привата обновлено.", reply_markup=KB([B("К привату", "a_priv")]))


@admin_router.callback_query(F.data == "a_priv_check")
async def a_priv_check(cb: CallbackQuery):
    await safe_answer(cb)
    if PRIVATE_GROUP_ID == 0:
        await show(cb, "PRIVATE_GROUP_ID не указан в начале файла.", KB([B("К привату", "a_priv")]))
        return
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(PRIVATE_GROUP_ID, me.id)
        status = str(getattr(member.status, "value", member.status))
        can_invite = getattr(member, "can_invite_users", None)
        if status == "administrator":
            text = f"Бот в группе: администратор. Право приглашать по ссылкам: {'да' if can_invite else 'нет'}."
        elif status == "creator":
            text = "Бот в группе: владелец."
        else:
            text = f"Бот в группе со статусом «{esc(status)}». Нужны права администратора."
    except Exception as err:
        text = f"Не удалось проверить группу: {esc(str(err))}"
    await show(cb, text, KB([B("К привату", "a_priv")]))


# ------------------------------- акции --------------------------------
@admin_router.callback_query(F.data == "a_disc")
async def a_disc(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    d = active_discount()
    cur = f"Сейчас идёт акция #{d['id']}: -{d['percent']}% до {fmt_ts(d['ends_at'])}." if d else "Сейчас акций нет."
    await show(
        cb,
        f"<b>Акции</b>\n{cur}",
        KB(
            [B("Создать акцию", "a_dnew"), B("Список акций", "a_dlist")],
            [B("Продлить акцию", "a_dext"), B("Завершить досрочно", "a_dend")],
            [B("В админ-панель", "a_menu")],
        ),
    )


@admin_router.callback_query(F.data == "a_dlist")
async def a_disc_list(cb: CallbackQuery):
    await safe_answer(cb)
    rows = qa("SELECT * FROM discounts ORDER BY id DESC LIMIT 20")
    text = "<b>Список акций</b>\n" + ("\n".join(disc_line(d) for d in rows) if rows else "Акций пока нет.")
    await show(cb, text, KB([B("К акциям", "a_disc")]))


@admin_router.callback_query(F.data == "a_dnew")
async def a_disc_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(DiscNew.percent)
    await safe_answer(cb)
    await show(cb, "Введите размер скидки в процентах (от 1 до 99).", KB([B("Отмена", "a_disc")]))


@admin_router.message(DiscNew.percent, F.text)
async def a_disc_new_percent(m: Message, state: FSMContext):
    percent = parse_int(m.text, 1, 99)
    if percent is None:
        await m.answer("Введите целое число от 1 до 99.", reply_markup=KB([B("Отмена", "a_disc")]))
        return
    await state.update_data(percent=percent)
    await state.set_state(DiscNew.mode)
    await m.answer(
        "На какие водянки действует скидка?",
        reply_markup=KB(
            [B("На все водянки", "a_dm:all")],
            [B("Только на выбранные", "a_dm:selected")],
            [B("На все, кроме выбранных", "a_dm:except")],
            [B("Отмена", "a_disc")],
        ),
    )


DATE_HINT = f"в формате ДД.ММ.ГГГГ ЧЧ:ММ (время UTC+{TZ_OFFSET_HOURS})"


@admin_router.callback_query(DiscNew.mode, F.data.startswith("a_dm:"))
async def a_disc_new_mode(cb: CallbackQuery, state: FSMContext):
    mode = cb.data.split(":")[1]
    await safe_answer(cb)
    if mode == "all":
        await state.update_data(mode="all", numbers=[])
        await state.set_state(DiscNew.start)
        await show(
            cb,
            f"Введите дату и время начала акции {DATE_HINT} или слово «сейчас».",
            KB([B("Отмена", "a_disc")]),
        )
        return
    await state.update_data(mode=mode)
    await state.set_state(DiscNew.numbers)
    await show(
        cb,
        "Введите номера водянок через запятую или пробел (например: 12, 15, 48).",
        KB([B("Отмена", "a_disc")]),
    )


@admin_router.message(DiscNew.numbers, F.text)
async def a_disc_new_numbers(m: Message, state: FSMContext):
    nums = sorted({int(x) for x in re.findall(r"\d+", m.text or "")})
    if not nums:
        await m.answer("Не нашёл номеров. Введите номера цифрами.", reply_markup=KB([B("Отмена", "a_disc")]))
        return
    missing = [n for n in nums if q1("SELECT 1 FROM products WHERE number=?", (n,)) is None]
    if missing:
        await m.answer(
            "Нет водянок с номерами: " + ", ".join(str(n) for n in missing) + ". Введите список ещё раз.",
            reply_markup=KB([B("Отмена", "a_disc")]),
        )
        return
    await state.update_data(numbers=nums)
    await state.set_state(DiscNew.start)
    await m.answer(
        f"Введите дату и время начала акции {DATE_HINT} или слово «сейчас».",
        reply_markup=KB([B("Отмена", "a_disc")]),
    )


@admin_router.message(DiscNew.start, F.text)
async def a_disc_new_start(m: Message, state: FSMContext):
    text = (m.text or "").strip()
    if text.lower() in ("сейчас", "now"):
        start = now_ts()
    else:
        start = parse_dt(text)
        if start is None:
            await m.answer(f"Не понял дату. Введите {DATE_HINT} или «сейчас».", reply_markup=KB([B("Отмена", "a_disc")]))
            return
        if start < now_ts() - 120:
            await m.answer("Дата начала в прошлом. Введите будущую дату или «сейчас».", reply_markup=KB([B("Отмена", "a_disc")]))
            return
    await state.update_data(start=start)
    await state.set_state(DiscNew.end)
    await m.answer(f"Введите дату и время окончания акции {DATE_HINT}.", reply_markup=KB([B("Отмена", "a_disc")]))


@admin_router.message(DiscNew.end, F.text)
async def a_disc_new_end(m: Message, state: FSMContext):
    end = parse_dt(m.text or "")
    data = await state.get_data()
    start = data.get("start")
    if end is None or start is None:
        await m.answer(f"Не понял дату. Введите {DATE_HINT}.", reply_markup=KB([B("Отмена", "a_disc")]))
        return
    if end <= start or end <= now_ts():
        await m.answer("Дата окончания должна быть позже начала и в будущем.", reply_markup=KB([B("Отмена", "a_disc")]))
        return
    if disc_overlap(start, end):
        await m.answer(
            "В этот период уже есть другая акция. Акции не должны пересекаться. Введите другую дату окончания.",
            reply_markup=KB([B("Отмена", "a_disc")]),
        )
        return
    cur = ex(
        "INSERT INTO discounts(percent, mode, numbers, starts_at, ends_at, status, created_at) "
        "VALUES(?,?,?,?,?, 'active', ?)",
        (
            data["percent"],
            data["mode"],
            ",".join(str(n) for n in data.get("numbers", [])),
            start,
            end,
            now_ts(),
        ),
    )
    await state.clear()
    d = q1("SELECT * FROM discounts WHERE id=?", (cur.lastrowid,))
    await m.answer("Акция создана.\n" + disc_line(d), reply_markup=KB([B("К акциям", "a_disc")]))


def open_discounts():
    return qa("SELECT * FROM discounts WHERE status='active' AND ends_at>? ORDER BY starts_at", (now_ts(),))


@admin_router.callback_query(F.data == "a_dext")
async def a_disc_extend(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    rows = open_discounts()
    if not rows:
        await show(cb, "Нет акций, которые можно продлить.", KB([B("К акциям", "a_disc")]))
        return
    kb_rows = [[B(f"#{d['id']}: -{d['percent']}% до {fmt_ts(d['ends_at'])}", f"a_dx:{d['id']}")] for d in rows]
    kb_rows.append([B("К акциям", "a_disc")])
    await show(cb, "Выберите акцию, которую нужно продлить.", KB(*kb_rows))


@admin_router.callback_query(F.data.startswith("a_dx:"))
async def a_disc_extend_pick(cb: CallbackQuery, state: FSMContext):
    did = int(cb.data.split(":")[1])
    d = q1("SELECT * FROM discounts WHERE id=? AND status='active'", (did,))
    if d is None:
        await safe_answer(cb, "Акция не найдена.", True)
        return
    await state.clear()
    await state.set_state(DiscExt.end)
    await state.update_data(did=did)
    await safe_answer(cb)
    await show(
        cb,
        f"Акция #{did}, сейчас до {fmt_ts(d['ends_at'])}.\nВведите новую дату окончания {DATE_HINT}.",
        KB([B("Отмена", "a_disc")]),
    )


@admin_router.message(DiscExt.end, F.text)
async def a_disc_extend_set(m: Message, state: FSMContext):
    end = parse_dt(m.text or "")
    data = await state.get_data()
    d = q1("SELECT * FROM discounts WHERE id=? AND status='active'", (data.get("did"),))
    if d is None:
        await state.clear()
        await m.answer("Акция не найдена.", reply_markup=KB([B("К акциям", "a_disc")]))
        return
    if end is None:
        await m.answer(f"Не понял дату. Введите {DATE_HINT}.", reply_markup=KB([B("Отмена", "a_disc")]))
        return
    if end <= max(d["starts_at"], now_ts()):
        await m.answer("Дата окончания должна быть в будущем и позже начала акции.", reply_markup=KB([B("Отмена", "a_disc")]))
        return
    if disc_overlap(d["starts_at"], end, d["id"]):
        await m.answer("Новый период пересекается с другой акцией. Введите другую дату.", reply_markup=KB([B("Отмена", "a_disc")]))
        return
    ex("UPDATE discounts SET ends_at=? WHERE id=?", (end, d["id"]))
    await state.clear()
    d = q1("SELECT * FROM discounts WHERE id=?", (d["id"],))
    await m.answer("Акция продлена.\n" + disc_line(d), reply_markup=KB([B("К акциям", "a_disc")]))


@admin_router.callback_query(F.data == "a_dend")
async def a_disc_end(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    rows = open_discounts()
    if not rows:
        await show(cb, "Нет акций, которые можно завершить.", KB([B("К акциям", "a_disc")]))
        return
    kb_rows = [[B(f"#{d['id']}: -{d['percent']}% ({disc_state(d)})", f"a_de:{d['id']}")] for d in rows]
    kb_rows.append([B("К акциям", "a_disc")])
    await show(cb, "Выберите акцию, которую нужно завершить досрочно.", KB(*kb_rows))


@admin_router.callback_query(F.data.startswith("a_de:"))
async def a_disc_end_ask(cb: CallbackQuery):
    did = int(cb.data.split(":")[1])
    d = q1("SELECT * FROM discounts WHERE id=? AND status='active'", (did,))
    await safe_answer(cb)
    if d is None:
        await show(cb, "Акция не найдена.", KB([B("К акциям", "a_disc")]))
        return
    await show(
        cb,
        "Завершить досрочно?\n" + disc_line(d),
        KB([B("Да, завершить", f"a_de_ok:{did}")], [B("Нет", "a_disc")]),
    )


@admin_router.callback_query(F.data.startswith("a_de_ok:"))
async def a_disc_end_ok(cb: CallbackQuery):
    did = int(cb.data.split(":")[1])
    ex("UPDATE discounts SET status='ended', ends_at=MIN(ends_at, ?) WHERE id=?", (now_ts(), did))
    await safe_answer(cb)
    await show(cb, f"Акция #{did} завершена.", KB([B("К акциям", "a_disc")]))


# ----------------------------- промокоды ------------------------------
@admin_router.callback_query(F.data == "a_promo")
async def a_promo(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    total = q1("SELECT COUNT(*) AS c FROM promos")["c"]
    await show(
        cb,
        f"<b>Промокоды</b>\nВсего: {total}",
        KB(
            [B("Создать промокод", "a_prm_new"), B("Список промокодов", "a_prm_list")],
            [B("В админ-панель", "a_menu")],
        ),
    )


@admin_router.callback_query(F.data == "a_prm_list")
async def a_promo_list(cb: CallbackQuery):
    await safe_answer(cb)
    rows = qa("SELECT * FROM promos ORDER BY created_at DESC LIMIT 30")
    lines = ["<b>Промокоды</b>"]
    kb_rows = []
    for r in rows:
        uses = q1("SELECT COUNT(*) AS c FROM promo_uses WHERE code=?", (r["code"],))["c"]
        what = f"{r['value']} {STAR} на баланс" if r["kind"] == "stars" else "бесплатная водянка"
        lines.append(f"<code>{esc(r['code'])}</code>: {what}, активаций: {uses}")
        kb_rows.append([B(f"Удалить {r['code']}", f"a_prm_del:{r['code']}")])
    if not rows:
        lines.append("Пока пусто.")
    kb_rows.append([B("К промокодам", "a_promo")])
    await show(cb, "\n".join(lines), KB(*kb_rows))


@admin_router.callback_query(F.data.startswith("a_prm_del:"))
async def a_promo_delete(cb: CallbackQuery):
    code = cb.data.split(":", 1)[1]
    with tx():
        ex("DELETE FROM promos WHERE code=?", (code,))
        ex("DELETE FROM promo_uses WHERE code=?", (code,))
    await safe_answer(cb, "Промокод удалён.")
    rows = qa("SELECT * FROM promos ORDER BY created_at DESC LIMIT 30")
    lines = ["<b>Промокоды</b>"]
    kb_rows = []
    for r in rows:
        uses = q1("SELECT COUNT(*) AS c FROM promo_uses WHERE code=?", (r["code"],))["c"]
        what = f"{r['value']} {STAR} на баланс" if r["kind"] == "stars" else "бесплатная водянка"
        lines.append(f"<code>{esc(r['code'])}</code>: {what}, активаций: {uses}")
        kb_rows.append([B(f"Удалить {r['code']}", f"a_prm_del:{r['code']}")])
    if not rows:
        lines.append("Пока пусто.")
    kb_rows.append([B("К промокодам", "a_promo")])
    await show(cb, "\n".join(lines), KB(*kb_rows))


@admin_router.callback_query(F.data == "a_prm_new")
async def a_promo_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(PromoNew.code)
    await safe_answer(cb)
    await show(
        cb,
        "Введите текст промокода (латиница, цифры, - и _, от 3 до 32 символов).",
        KB([B("Отмена", "a_promo")]),
    )


@admin_router.message(PromoNew.code, F.text)
async def a_promo_new_code(m: Message, state: FSMContext):
    code = (m.text or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_\-]{3,32}", code):
        await m.answer(
            "Допустимы латиница, цифры, - и _, от 3 до 32 символов. Введите ещё раз.",
            reply_markup=KB([B("Отмена", "a_promo")]),
        )
        return
    if q1("SELECT 1 FROM promos WHERE code=?", (code,)) is not None:
        await m.answer("Такой промокод уже есть. Введите другой.", reply_markup=KB([B("Отмена", "a_promo")]))
        return
    await state.update_data(code=code)
    await state.set_state(PromoNew.kind)
    await m.answer(
        f"Промокод <code>{esc(code)}</code>. Что он даёт?",
        reply_markup=KB(
            [B("Звёзды на баланс", "a_pk:stars")],
            [B("Бесплатная водянка", "a_pk:free")],
            [B("Отмена", "a_promo")],
        ),
    )


@admin_router.callback_query(PromoNew.kind, F.data.startswith("a_pk:"))
async def a_promo_new_kind(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[1]
    data = await state.get_data()
    code = data.get("code")
    await safe_answer(cb)
    if not code:
        await state.clear()
        await show(cb, "Сессия устарела. Начните заново.", KB([B("К промокодам", "a_promo")]))
        return
    if kind == "free":
        ex("INSERT OR REPLACE INTO promos(code, kind, value, created_at) VALUES(?,?,?,?)", (code, "free", 1, now_ts()))
        await state.clear()
        await show(
            cb,
            f"Промокод <code>{esc(code)}</code> создан: бесплатная водянка на выбор.",
            KB([B("К промокодам", "a_promo")]),
        )
        return
    await state.update_data(kind="stars")
    await state.set_state(PromoNew.value)
    await show(cb, "Введите, сколько звёзд начислять на баланс.", KB([B("Отмена", "a_promo")]))


@admin_router.message(PromoNew.value, F.text)
async def a_promo_new_value(m: Message, state: FSMContext):
    value = parse_int(m.text, 1, 100000)
    if value is None:
        await m.answer("Введите целое число от 1.", reply_markup=KB([B("Отмена", "a_promo")]))
        return
    data = await state.get_data()
    code = data.get("code")
    if not code:
        await state.clear()
        await m.answer("Сессия устарела. Начните заново.", reply_markup=KB([B("К промокодам", "a_promo")]))
        return
    ex("INSERT OR REPLACE INTO promos(code, kind, value, created_at) VALUES(?,?,?,?)", (code, "stars", value, now_ts()))
    await state.clear()
    await m.answer(
        f"Промокод <code>{esc(code)}</code> создан: {value} {STAR} на баланс.",
        reply_markup=KB([B("К промокодам", "a_promo")]),
    )


# ------------------------------ рассылка ------------------------------
def bc_markup(data: dict):
    rows = []
    link = data.get("link")
    if link:
        rows.append([InlineKeyboardButton(text=link[0], url=link[1])])
    if data.get("start_text"):
        rows.append([B(data["start_text"], "bc_start")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def send_bc_menu(uid: int, data: dict) -> None:
    parts = []
    if data.get("link"):
        parts.append(f"кнопка-ссылка «{esc(data['link'][0])}»")
    if data.get("start_text"):
        parts.append(f"кнопка «{esc(data['start_text'])}» (запускает /start)")
    now = ", ".join(parts) if parts else "нет"
    await bot.send_message(
        uid,
        f"Сообщение для рассылки принято.\nКнопки под сообщением: {now}.\n"
        f"Добавьте кнопки при необходимости и перейдите к предпросмотру.",
        reply_markup=KB(
            [B("Добавить кнопку-ссылку", "a_bcl")],
            [B("Добавить кнопку «Начать покупку»", "a_bcs")],
            [B("Убрать кнопки", "a_bcr")],
            [B("Предпросмотр", "a_bcp")],
            [B("Отмена", "a_bcx")],
        ),
    )


@admin_router.callback_query(F.data == "a_bc")
async def a_bc(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Bcast.content)
    await safe_answer(cb)
    await show(
        cb,
        "Отправьте сообщение для рассылки. Это может быть текст, фото, видео, файл и любой другой тип.",
        KB([B("Отмена", "a_bcx")]),
    )


@admin_router.callback_query(F.data == "a_bcx")
async def a_bc_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb, "Рассылка отменена.")
    await show(cb, admin_text(), admin_kb())


@admin_router.message(Bcast.content)
async def a_bc_content(m: Message, state: FSMContext):
    await state.update_data(src_chat=m.chat.id, src_msg=m.message_id, link=None, start_text=None)
    await state.set_state(Bcast.menu)
    await send_bc_menu(m.from_user.id, await state.get_data())


@admin_router.callback_query(Bcast.menu, F.data == "a_bcl")
async def a_bc_link(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Bcast.link_text)
    await safe_answer(cb)
    await show(cb, "Введите текст кнопки-ссылки.", KB([B("Отмена", "a_bcx")]))


@admin_router.message(Bcast.link_text, F.text)
async def a_bc_link_text(m: Message, state: FSMContext):
    text = (m.text or "").strip()
    if not text or len(text) > 60:
        await m.answer("Текст кнопки должен быть от 1 до 60 символов.", reply_markup=KB([B("Отмена", "a_bcx")]))
        return
    await state.update_data(link_text=text)
    await state.set_state(Bcast.link_url)
    await m.answer("Введите ссылку (должна начинаться с https://).", reply_markup=KB([B("Отмена", "a_bcx")]))


@admin_router.message(Bcast.link_url, F.text)
async def a_bc_link_url(m: Message, state: FSMContext):
    url = (m.text or "").strip()
    if not re.fullmatch(r"https://[^\s]+\.[^\s]+", url) or len(url) > 512:
        await m.answer("Нужна ссылка, начинающаяся с https://. Введите ещё раз.", reply_markup=KB([B("Отмена", "a_bcx")]))
        return
    data = await state.get_data()
    await state.update_data(link=[data.get("link_text", "Открыть"), url])
    await state.set_state(Bcast.menu)
    await send_bc_menu(m.from_user.id, await state.get_data())


@admin_router.callback_query(Bcast.menu, F.data == "a_bcs")
async def a_bc_start_btn(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Bcast.start_text)
    await safe_answer(cb)
    await show(
        cb,
        "Введите текст кнопки или отправьте «-», чтобы использовать текст «Начать покупку».",
        KB([B("Отмена", "a_bcx")]),
    )


@admin_router.message(Bcast.start_text, F.text)
async def a_bc_start_text(m: Message, state: FSMContext):
    text = (m.text or "").strip()
    if text == "-":
        text = "Начать покупку"
    if not text or len(text) > 60:
        await m.answer("Текст кнопки должен быть от 1 до 60 символов.", reply_markup=KB([B("Отмена", "a_bcx")]))
        return
    await state.update_data(start_text=text)
    await state.set_state(Bcast.menu)
    await send_bc_menu(m.from_user.id, await state.get_data())


@admin_router.callback_query(Bcast.menu, F.data == "a_bcr")
async def a_bc_reset(cb: CallbackQuery, state: FSMContext):
    await state.update_data(link=None, start_text=None)
    await safe_answer(cb, "Кнопки убраны.")
    await send_bc_menu(cb.from_user.id, await state.get_data())


@admin_router.callback_query(Bcast.menu, F.data == "a_bcm")
async def a_bc_menu_again(cb: CallbackQuery, state: FSMContext):
    await safe_answer(cb)
    await send_bc_menu(cb.from_user.id, await state.get_data())


@admin_router.callback_query(Bcast.menu, F.data == "a_bcp")
async def a_bc_preview(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await safe_answer(cb)
    try:
        await bot.copy_message(
            cb.from_user.id, data["src_chat"], data["src_msg"], reply_markup=bc_markup(data)
        )
    except Exception as err:
        await bot.send_message(cb.from_user.id, f"Не удалось показать предпросмотр: {esc(str(err))}")
        return
    count = q1("SELECT COUNT(*) AS c FROM users WHERE blocked=0")["c"]
    await bot.send_message(
        cb.from_user.id,
        f"Так сообщение увидят пользователи. Получателей: {count}. Отправить?",
        reply_markup=KB(
            [B("Отправить всем", "a_bcgo")],
            [B("Назад к кнопкам", "a_bcm")],
            [B("Отмена", "a_bcx")],
        ),
    )


async def run_broadcast(admin_id: int, src_chat: int, src_msg: int, markup) -> None:
    ids = [r["id"] for r in qa("SELECT id FROM users WHERE blocked=0")]
    ok = 0
    fail = 0
    for uid in ids:
        sent = False
        for _ in range(2):
            try:
                await bot.copy_message(uid, src_chat, src_msg, reply_markup=markup)
                sent = True
                break
            except TelegramRetryAfter as err:
                await asyncio.sleep(err.retry_after + 1)
            except Exception:
                break
        if sent:
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.05)
    await notify_user(admin_id, f"Рассылка завершена. Доставлено: {ok}, не доставлено: {fail}.")


@admin_router.callback_query(Bcast.menu, F.data == "a_bcgo")
async def a_bc_go(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await safe_answer(cb)
    count = q1("SELECT COUNT(*) AS c FROM users WHERE blocked=0")["c"]
    task = asyncio.create_task(
        run_broadcast(cb.from_user.id, data["src_chat"], data["src_msg"], bc_markup(data))
    )
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    await bot.send_message(
        cb.from_user.id,
        f"Рассылка запущена. Получателей: {count}. Когда она закончится, я напишу итог.",
        reply_markup=back_admin(),
    )


# ----------------------------- статистика -----------------------------
def stats_text() -> str:
    t = now_ts()
    d0 = day_start_ts(t)
    m0 = month_start_ts(t)

    def revenue(since: int):
        r = q1("SELECT COALESCE(SUM(total),0) AS s, COUNT(*) AS c FROM orders WHERE created_at>=?", (since,))
        return r["s"], r["c"]

    all_s, all_c = revenue(0)
    mon_s, mon_c = revenue(m0)
    day_s, day_c = revenue(d0)
    users_total = q1("SELECT COUNT(*) AS c FROM users")["c"]
    users_blocked = q1("SELECT COUNT(*) AS c FROM users WHERE blocked=1")["c"]
    users_today = q1("SELECT COUNT(*) AS c FROM users WHERE joined_at>=?", (d0,))["c"]
    by_status = {r["status"]: r["c"] for r in qa("SELECT status, COUNT(*) AS c FROM orders GROUP BY status")}
    private_sales = q1("SELECT COUNT(*) AS c FROM orders WHERE kind='private'")["c"]
    top = qa(
        "SELECT number, COUNT(*) AS c FROM order_items GROUP BY number ORDER BY c DESC, number LIMIT 10"
    )
    ref = q1("SELECT COUNT(*) AS c, COALESCE(SUM(ref_bonus),0) AS s FROM users WHERE referrer_id IS NOT NULL")
    top_ref = qa(
        "SELECT referrer_id, COUNT(*) AS c FROM users WHERE referrer_id IS NOT NULL "
        "GROUP BY referrer_id ORDER BY c DESC LIMIT 5"
    )
    lines = [
        "<b>Статистика</b>",
        "",
        "<b>Выручка (оплаченные заказы)</b>",
        f"За всё время: {all_s} {STAR} ({all_c} зак.)",
        f"За месяц: {mon_s} {STAR} ({mon_c} зак.)",
        f"За сегодня: {day_s} {STAR} ({day_c} зак.)",
        "",
        "<b>Пользователи</b>",
        f"Всего: {users_total}, заблокировано: {users_blocked}, новых сегодня: {users_today}",
        "",
        "<b>Заказы</b>",
        f"Активных: {by_status.get('active', 0)}, выполненных: {by_status.get('done', 0)}, "
        f"отменённых: {by_status.get('cancelled', 0)}",
        f"Продаж привата: {private_sales}",
        "",
        "<b>Топ водянок</b>",
    ]
    if top:
        for i, r in enumerate(top, 1):
            lines.append(f"{i}. Водянка №{r['number']} - {r['c']} раз")
    else:
        lines.append("Пока нет продаж.")
    lines += ["", "<b>Рефералы</b>", f"Приглашено: {ref['c']}, начислено: {ref['s']} {STAR}"]
    for r in top_ref:
        u = q1("SELECT * FROM users WHERE id=?", (r["referrer_id"],))
        lines.append(f"{user_ref(u)} - {r['c']}")
    return "\n".join(lines)


@admin_router.callback_query(F.data == "a_stat")
async def a_stats(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    await show(cb, stats_text(), back_admin())


# ------------------------ блокировка пользователей --------------------
@admin_router.callback_query(F.data == "a_usr")
async def a_users(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_answer(cb)
    blocked = q1("SELECT COUNT(*) AS c FROM users WHERE blocked=1")["c"]
    await show(
        cb,
        f"<b>Блокировка пользователей</b>\nЗаблокировано сейчас: {blocked}",
        KB(
            [B("Заблокировать", "a_ub"), B("Разблокировать", "a_uu")],
            [B("Список заблокированных", "a_ul")],
            [B("В админ-панель", "a_menu")],
        ),
    )


@admin_router.callback_query(F.data.in_({"a_ub", "a_uu"}))
async def a_user_ask(cb: CallbackQuery, state: FSMContext):
    action = "block" if cb.data == "a_ub" else "unblock"
    await state.clear()
    await state.set_state(UserBlock.target)
    await state.update_data(action=action)
    await safe_answer(cb)
    word = "заблокировать" if action == "block" else "разблокировать"
    await show(
        cb,
        f"Отправьте ID или @username пользователя, которого нужно {word}. "
        f"Пользователь должен хотя бы раз запускать бота.",
        KB([B("Отмена", "a_usr")]),
    )


@admin_router.message(UserBlock.target, F.text)
async def a_user_apply(m: Message, state: FSMContext):
    u = find_user(m.text or "")
    if u is None:
        await m.answer("Пользователь не найден. Проверьте ID или @username.", reply_markup=KB([B("Отмена", "a_usr")]))
        return
    data = await state.get_data()
    action = data.get("action")
    if is_admin(u["id"]):
        await m.answer("Администраторов блокировать нельзя.", reply_markup=KB([B("К пользователям", "a_usr")]))
        await state.clear()
        return
    ex("UPDATE users SET blocked=? WHERE id=?", (1 if action == "block" else 0, u["id"]))
    await state.clear()
    verb = "заблокирован" if action == "block" else "разблокирован"
    await m.answer(
        f"Пользователь {user_ref(u)} (ID <code>{u['id']}</code>) {verb}.",
        reply_markup=KB([B("К пользователям", "a_usr")]),
    )


@admin_router.callback_query(F.data == "a_ul")
async def a_user_list(cb: CallbackQuery):
    await safe_answer(cb)
    rows = qa("SELECT * FROM users WHERE blocked=1 ORDER BY id LIMIT 50")
    lines = ["<b>Заблокированные пользователи</b>"]
    if not rows:
        lines.append("Список пуст.")
    for u in rows:
        lines.append(f"{user_ref(u)}, ID <code>{u['id']}</code>")
    await show(cb, "\n".join(lines), KB([B("К пользователям", "a_usr")]))


# ----------------------------- цена за реферала -----------------------
@admin_router.callback_query(F.data == "a_ref")
async def a_ref(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(RefPrice.value)
    await safe_answer(cb)
    await show(
        cb,
        f"<b>Цена за реферала</b>\nСейчас: {get_setting('ref_reward')} {STAR} за каждого приглашённого.\n"
        f"Отправьте новое число (0 отключает начисление).",
        KB([B("Отмена", "a_menu")]),
    )


@admin_router.message(RefPrice.value, F.text)
async def a_ref_set(m: Message, state: FSMContext):
    value = parse_int(m.text, 0, 100000)
    if value is None:
        await m.answer("Введите целое число от 0.", reply_markup=KB([B("Отмена", "a_menu")]))
        return
    set_setting("ref_reward", value)
    await state.clear()
    await m.answer(f"Цена за реферала: {value} {STAR}.", reply_markup=back_admin())


# ======================================================================
# Резервные копии, ошибки, запуск
# ======================================================================
async def send_backup(reason: str = "Резервная копия базы") -> None:
    path = os.path.join(tempfile.gettempdir(), f"shop_backup_{int(time.time())}.db")
    dst = sqlite3.connect(path)
    try:
        db.backup(dst)
    finally:
        dst.close()
    try:
        for aid in ADMIN_IDS:
            try:
                await bot.send_document(
                    aid,
                    FSInputFile(path, filename="shop_backup.db"),
                    caption=f"{reason}: {fmt_ts(now_ts())}",
                )
            except Exception as err:
                log.warning("Не удалось отправить бэкап %s: %s", aid, err)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def backup_loop() -> None:
    while True:
        try:
            last = int(get_setting("last_backup") or 0)
            if now_ts() - last >= 86400:
                await send_backup("Ежедневная резервная копия")
                set_setting("last_backup", now_ts())
        except Exception as err:
            log.error("backup_loop: %s", err)
        await asyncio.sleep(3600)


_last_error_sent = 0.0
IGNORED_ERRORS = (
    "message is not modified",
    "query is too old",
    "query ID is invalid",
    "message to delete not found",
    "bot was blocked by the user",
)


async def on_error(event: ErrorEvent):
    global _last_error_sent
    exc = event.exception
    log.error("Ошибка в обработчике: %s", exc, exc_info=exc)
    if any(s in str(exc) for s in IGNORED_ERRORS):
        return True
    if time.time() - _last_error_sent > 10:
        _last_error_sent = time.time()
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-3000:]
        await notify_admins(f"<b>Ошибка в боте</b>\n<pre>{esc(tb)}</pre>")
    return True


def create_bot() -> Bot:
    try:
        from aiogram.client.default import DefaultBotProperties
    except ImportError:  # aiogram старше 3.7
        return Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    return Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def main() -> None:
    global bot
    if not BOT_TOKEN or "ВСТАВЬТЕ" in BOT_TOKEN:
        raise SystemExit("Укажите токен бота в BOT_TOKEN (в начале файла или в переменной окружения).")
    init_db()
    bot = create_bot()
    dp = Dispatcher(storage=SQLiteStorage())

    access = AccessMiddleware()
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.pre_checkout_query.outer_middleware(access)
    dp.errors.register(on_error)

    dp.include_router(cmd_router)
    dp.include_router(admin_router)
    dp.include_router(user_router)

    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="support", description="Техническая поддержка"),
            ]
        )
        for aid in ADMIN_IDS:
            try:
                await bot.set_my_commands(
                    [
                        BotCommand(command="start", description="Главное меню"),
                        BotCommand(command="support", description="Техническая поддержка"),
                        BotCommand(command="admin", description="Админ-панель"),
                    ],
                    scope=BotCommandScopeChat(chat_id=aid),
                )
            except Exception as err:
                log.warning("Команды для админа %s не установлены: %s", aid, err)
    except Exception as err:
        log.warning("Не удалось установить команды: %s", err)

    task = asyncio.create_task(backup_loop())
    background_tasks.add(task)
    log.info("Бот запущен")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except SystemExit as err:
        if err.code:
            log.error("%s", err.code)   # раньше сообщение об ошибке запуска терялось молча
    finally:
        close_db()
