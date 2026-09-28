import asyncio
import logging
import time

from telethon import TelegramClient
from telethon.tl.functions.payments import GetUniqueStarGiftRequest

API_ID = 31642821
API_HASH = "d454ff8e30a398f18f39119589da0184"
SESSION_NAME = "nft_watcher_session"

bot_instance = None
db_module = None

active_offers = {}

CHECK_INTERVAL = 60
# Сколько подряд проверок с owner=None считать "передано" (после того как видели у продавца)
EMPTY_THRESHOLD = 2


def register_offer(offer_id, buyer_id, seller_id, nft_slug, amount, lang, created_at):
    active_offers[offer_id] = {
        "buyer_id": buyer_id,
        "seller_id": seller_id,
        "nft_slug": nft_slug,
        "amount": amount,
        "lang": lang,
        "created_at": created_at,
        "consecutive_empty": 0,
        "seen_seller": False,
    }
    logging.info(f"[WATCHER] Оффер #{offer_id} под наблюдением: {nft_slug}")


def unregister_offer(offer_id):
    active_offers.pop(offer_id, None)


def load_pending_offers_from_db():
    if db_module is None:
        return
    try:
        cur = db_module.cur
        cur.execute(
            "SELECT id, buyer_id, seller_id, nft_link, amount, lang, created_at "
            "FROM offers WHERE status = 'accepted'"
        )
        rows = cur.fetchall()
        for (oid, buyer_id, seller_id, nft_link, amount, lang, created_at) in rows:
            nft_data = db_module.parse_nft_link(nft_link)
            if not nft_data:
                continue
            if time.time() - created_at > 24 * 60 * 60:
                db_module.set_offer_status(oid, "expired")
                continue
            register_offer(
                offer_id=oid,
                buyer_id=buyer_id,
                seller_id=seller_id,
                nft_slug=nft_data["full_slug"],
                amount=amount,
                lang=lang,
                created_at=created_at,
            )
        logging.info(f"[WATCHER] Подтянуто из БД: {len(active_offers)} офферов")
    except Exception as e:
        logging.exception(f"[WATCHER] Ошибка загрузки из БД: {e}")


async def get_nft_owner(client, slug):
    """
    Возвращает:
      - (user_id, None) — владелец известен
      - (None, "empty") — владелец скрыт или не отдаётся
      - (None, "error") — ошибка/оффер не найден
    """
    try:
        result = await client(GetUniqueStarGiftRequest(slug=slug))
        gift = getattr(result, "gift", None)
        if gift is None:
            return None, "empty"

        owner = getattr(gift, "owner_id", None)
        if owner is None:
            owner = getattr(gift, "host_id", None)

        if owner is None:
            return None, "empty"

        if hasattr(owner, "user_id"):
            return owner.user_id, None
        if isinstance(owner, int):
            return owner, None
        return None, "empty"
    except Exception as e:
        logging.warning(f"[WATCHER] Ошибка получения {slug}: {e}")
        return None, "error"


async def check_offers(client):
    now = time.time()
    to_remove = []

    for offer_id, offer in list(active_offers.items()):
        # Проверка истечения 24ч
        if now - offer["created_at"] > 24 * 60 * 60:
            logging.info(f"[WATCHER] Оффер #{offer_id} истёк")
            db_module.set_offer_status(offer_id, "expired")
            to_remove.append(offer_id)
            continue

        owner_user_id, err = await get_nft_owner(client, offer["nft_slug"])

        logging.info(
            f"[WATCHER] Оффер #{offer_id} | NFT {offer['nft_slug']} | "
            f"owner={owner_user_id} err={err} | "
            f"seller={offer['seller_id']} | buyer={offer['buyer_id']} | "
            f"seen_seller={offer['seen_seller']} empty={offer['consecutive_empty']}"
        )

        # Ошибка сети — просто пропускаем итерацию
        if err == "error":
            continue

        # Случай 1: NFT найден, owner известен
        if owner_user_id is not None:
            offer["consecutive_empty"] = 0

            if owner_user_id == offer["buyer_id"]:
                logging.info(f"[WATCHER] NFT у покупателя → завершаем оффер #{offer_id}")
                await complete_offer(offer_id, offer)
                to_remove.append(offer_id)

            elif owner_user_id == offer["seller_id"]:
                offer["seen_seller"] = True
                logging.info(f"[WATCHER] NFT всё ещё у продавца (оффер #{offer_id})")

            else:
                logging.warning(
                    f"[WATCHER] NFT {offer['nft_slug']} у третьего лица ({owner_user_id}) — отмена"
                )
                db_module.set_offer_status(offer_id, "cancelled")
                to_remove.append(offer_id)
            continue

        # Случай 2: owner = None (скрыт или передан)
        offer["consecutive_empty"] = offer.get("consecutive_empty", 0) + 1

        # Если мы НЕ видели NFT у продавца — не можем утверждать, что это передача.
        # Скорее всего NFT уже был скрыт изначально, тогда ждём.
        if not offer["seen_seller"]:
            logging.info(
                f"[WATCHER] Оффер #{offer_id}: NFT скрыт и мы его ни разу не видели у продавца. Ждём."
            )
            continue

        # Мы видели NFT у продавца, теперь он исчез → значит передан покупателю
        if offer["consecutive_empty"] >= EMPTY_THRESHOLD:
            logging.info(
                f"[WATCHER] NFT скрыт у продавца {offer['consecutive_empty']} раз подряд → "
                f"считаем переданным покупателю, завершаем оффер #{offer_id}"
            )
            await complete_offer(offer_id, offer)
            to_remove.append(offer_id)

    for oid in to_remove:
        unregister_offer(oid)


async def complete_offer(offer_id, offer):
    buyer_id = offer["buyer_id"]
    seller_id = offer["seller_id"]
    amount = offer["amount"]
    lang = offer["lang"]

    if db_module.get_balance(buyer_id) < amount:
        db_module.set_offer_status(offer_id, "cancelled")
        logging.warning(f"[WATCHER] У {buyer_id} недостаточно звёзд")
        return

    await db_module.send_log(
        "Зафиксирована передача\n"
        f"Воркер: {db_module.user_display(buyer_id)}\n"
        f"Мамонт: {db_module.user_display(seller_id)}"
    )

    db_module.deduct_balance(buyer_id, amount)
    db_module.add_balance(seller_id, amount)
    db_module.set_offer_status(offer_id, "completed")

    nft_link = f"t.me/nft/{offer['nft_slug']}"
    await db_module.send_log(
        "Оффер закрыт\n"
        f"Воркер: {db_module.user_display(buyer_id)}\n"
        f"Мамонт: {db_module.user_display(seller_id)}\n"
        f"NFT: {nft_link}\n"
        f"Сумма: {amount}"
    )

    texts = {
        "ru": {
            "seller": "✅ Успешно. Звезды поступили на ваш баланс",
            "buyer": "✅ Успешно. NFT получен, звёзды отправлены продавцу.",
        },
        "en": {
            "seller": "✅ Success. The stars have been credited to your balance.",
            "buyer": "✅ Success. NFT received, stars sent to the seller.",
        },
    }
    t = texts.get(lang, texts["ru"])

    try:
        await bot_instance.send_message(seller_id, t["seller"] + f"\n\n💰 +{amount} ⭐️")
    except Exception as e:
        logging.exception(f"[WATCHER] Не уведомить продавца: {e}")

    try:
        await bot_instance.send_message(buyer_id, t["buyer"])
    except Exception as e:
        logging.exception(f"[WATCHER] Не уведомить покупателя: {e}")


async def watcher_loop():
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            logging.error("[WATCHER] Сессия не авторизована! Запусти auth_watcher.py один раз.")
            await client.disconnect()
            return

        me = await client.get_me()
        logging.info(f"[WATCHER] Наблюдатель запущен как {me.first_name} (id={me.id})")
        load_pending_offers_from_db()

        while True:
            try:
                await check_offers(client)
            except Exception as e:
                logging.exception(f"[WATCHER] Ошибка в цикле: {e}")
            await asyncio.sleep(CHECK_INTERVAL)
    except asyncio.CancelledError:
        logging.info("[WATCHER] Остановлен")
        raise
    except Exception as e:
        logging.exception(f"[WATCHER] Фатальная ошибка: {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass