import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from telethon import TelegramClient, events

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# @ovftank
TOKEN = ""
API_ID = 0
API_HASH = ""
CONFIG_PATH = Path(__file__).parent / "data.json"

DEFAULT_CONFIG = {"pairs": []}


class Member(BaseModel):
    chatid: int
    prefix: str
    receive_topicid: int
    send_topicid: int


class GroupConfig(BaseModel):
    chatid: int
    admins: list[Member] | None = None
    users: list[Member] | None = None


class PairConfig(BaseModel):
    id: str
    name: str
    admin_group: GroupConfig
    user_group: GroupConfig


class ConfigData(BaseModel):
    pairs: list[PairConfig]


_admin_chatid_map = {}
_user_chatid_map = {}


def load_config():
    global _admin_chatid_map, _user_chatid_map

    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    _admin_chatid_map = {}
    _user_chatid_map = {}

    for pair in cfg.get("pairs", []):
        _admin_chatid_map[pair["admin_group"]["chatid"]] = pair
        _user_chatid_map[pair["user_group"]["chatid"]] = pair

    return cfg


def save_config(data: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    logger.info(f"[CONFIG] Saved | path={CONFIG_PATH}")


async def forward_message(client, message, target_chat, prefix, reply_to=None):
    if message.media:
        await client.send_file(
            entity=target_chat,
            file=message.media,
            caption=f"{prefix}: {message.message or ''}",
            reply_to=reply_to,
        )
    else:
        await client.send_message(
            entity=target_chat,
            message=f"{prefix}: {message.message}",
            reply_to=reply_to,
        )


async def run_bot():
    client: TelegramClient = TelegramClient("bot_session", API_ID, API_HASH)
    async with client:
        await client.sign_in(bot_token=TOKEN)
        logger.info("[BOT] Started | session=bot_session")

        @client.on(events.NewMessage())
        async def message_handler(event: events.NewMessage.Event):
            try:
                message = event.message

                if message.message.lower() == "gid":
                    chat_type = ""
                    if event.is_private:
                        chat_type = "PRIVATE"
                    elif event.is_channel:
                        if event.chat:
                            if (
                                hasattr(event.chat, "gigagroup")
                                and event.chat.gigagroup
                            ):
                                chat_type = "GIGAGROUP"
                            elif (
                                hasattr(event.chat, "megagroup")
                                and event.chat.megagroup
                            ):
                                chat_type = "SUPERGROUP"
                            elif (
                                hasattr(event.chat, "broadcast")
                                and event.chat.broadcast
                            ):
                                chat_type = "CHANNEL"
                            else:
                                chat_type = "CHANNEL"
                        else:
                            chat_type = "CHANNEL"
                    elif event.is_group:
                        chat_type = "GROUP"

                    response = f"📌 CHAT ID: `{event.chat_id}`\n📋 TYPE: {chat_type}"

                    if (
                        message.reply_to
                        and hasattr(message.reply_to, "forum_topic")
                        and message.reply_to.forum_topic
                    ):
                        topic_id = message.reply_to.reply_to_msg_id
                        response += f"\n🏷️ TOPIC ID: `{topic_id}`"

                    await event.reply(response)
                    logger.info(f"[CMD] gid | chat_id={event.chat_id} type={chat_type}")
                    return

                chat_id = event.chat_id

                logger.debug(
                    f"[EVENT] NewMessage | chat_id={chat_id} sender_id={message.sender_id}"
                )

                if chat_id in _user_chatid_map:
                    pair = _user_chatid_map[chat_id]
                    if pair["user_group"]["chatid"] == 0:
                        logger.warning(
                            f"[EVENT] Skip | pair={pair['id']} user_group.chatid=0"
                        )
                        return

                    for user in pair["user_group"]["users"]:
                        if user["chatid"] == message.sender_id:
                            if (
                                message.reply_to
                                and hasattr(message.reply_to, "forum_topic")
                                and message.reply_to.forum_topic
                                and message.reply_to.reply_to_msg_id
                                == user["receive_topicid"]
                            ):
                                prefix = user["prefix"]
                                await forward_message(
                                    client,
                                    message,
                                    pair["admin_group"]["chatid"],
                                    prefix,
                                    reply_to=user["send_topicid"],
                                )
                                logger.info(
                                    f"[FORWARD] User→Admin | pair={pair['id']} prefix={prefix}"
                                )
                            break

                elif chat_id in _admin_chatid_map:
                    pair = _admin_chatid_map[chat_id]
                    if pair["admin_group"]["chatid"] == 0:
                        logger.warning(
                            f"[EVENT] Skip | pair={pair['id']} admin_group.chatid=0"
                        )
                        return

                    for admin in pair["admin_group"]["admins"]:
                        if admin["chatid"] == message.sender_id:
                            if (
                                message.reply_to
                                and hasattr(message.reply_to, "forum_topic")
                                and message.reply_to.forum_topic
                                and message.reply_to.reply_to_msg_id
                                == admin["receive_topicid"]
                            ):
                                prefix = admin["prefix"]
                                await forward_message(
                                    client,
                                    message,
                                    pair["user_group"]["chatid"],
                                    prefix,
                                    reply_to=admin["send_topicid"],
                                )
                                logger.info(
                                    f"[FORWARD] Admin→User | pair={pair['id']} prefix={prefix} topic={admin['send_topicid']}"
                                )
                            break

            except Exception as e:
                logger.error(f"[ERROR] message_handler | {type(e).__name__}: {e}")

        await client.disconnected


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(run_bot())

    try:
        yield
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


app = FastAPI(lifespan=lifespan)

templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    cfg = load_config()
    return templates.TemplateResponse("index.html", {"request": request, "config": cfg})


@app.get("/api/config")
async def get_config():
    return load_config()


@app.put("/api/config")
async def update_config(data: ConfigData):
    current = load_config()
    current["pairs"] = [p.model_dump() for p in data.pairs]
    save_config(current)
    logger.info(f"[API] PUT /api/config | pairs={len(current['pairs'])}")
    return current


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=80,
        workers=1,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
