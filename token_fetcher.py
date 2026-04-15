import asyncio
from urllib.parse import unquote

from pyrogram import Client
from pyrogram.raw.functions.messages import RequestAppWebView
from pyrogram.raw.types import InputBotAppShortName, InputUser
import requests

API_ID = 123456
API_HASH = "your_api_hash"
SESSION_NAME = "mrkt_session"


async def get_auth_token() -> str | None:
    client = Client(SESSION_NAME, API_ID, API_HASH)
    async with client:
        bot_entity = await client.get_users("mrkt")
        peer = await client.resolve_peer("mrkt")

        bot = InputUser(user_id=bot_entity.id, access_hash=bot_entity.raw.access_hash)
        bot_app = InputBotAppShortName(bot_id=bot, short_name="app")

        web_view = await client.invoke(
            RequestAppWebView(
                peer=peer,
                app=bot_app,
                platform="android",
            )
        )

        init_data = unquote(web_view.url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0])
        response = requests.post("https://api.tgmrkt.io/api/v1/auth", json={"data": init_data}, timeout=20)
        response.raise_for_status()
        data = response.json()
        return data.get("token")


if __name__ == "__main__":
    token = asyncio.run(get_auth_token())
    print(token)
