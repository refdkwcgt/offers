import asyncio
from telethon import TelegramClient

API_ID = 31642821
API_HASH = "d454ff8e30a398f18f39119589da0184"
SESSION_NAME = "nft_watcher_session"


async def main():
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"✅ Авторизация успешна: {me.first_name} (@{me.username}) id={me.id}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())