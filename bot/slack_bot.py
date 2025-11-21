import logging
import asyncio
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient
from utils.emoji_mapper import EmojiMapper
from services.database_service import *
from config import *
from bot.discord_bot import send_to_discord, get_file_objs, edit_at_discord, delete_from_discord

slack_client = AsyncWebClient(token=SLACK_BOT_TOKEN)
app = AsyncApp(client=slack_client)

# チャンネルID
CHANNEL_IDS = [SLACK_CHANNEL_ID_1, SLACK_CHANNEL_ID_2, SUB_CHANNEL_ID] + NOTIFY_CHANNEL_IDS
DISCORD_CHANNEL_IDS = [DISCORD_CHANNEL_ID_1, DISCORD_CHANNEL_ID_2, DISCORD_CHANNEL_ID_3]

# 監視するユーザーリスト
monitored_users = set()

async def get_slack_user_name(user_id):
    user_info = await slack_client.users_info(user=user_id)
    profile = user_info["user"]["profile"]
    return profile.get("display_name") or profile.get("real_name") or "Unknown"

async def get_slack_channel_name(channel_id):
    channel_info = await slack_client.conversations_info(channel=channel_id)
    return channel_info["channel"]["name"]

async def get_slack_user(user_id):
    """
    Fetch Slack user's display name and avatar URL.
    
    Returns:
        (display_name: str, avatar_url: str or None)
    """
    try:
        resp = await slack_client.users_info(user=user_id)
        if not resp["ok"]:
            return "Unknown", None
        user = resp["user"]
        profile = user.get("profile", {})

        # Prefer display_name over real_name
        display_name = profile.get("display_name") or profile.get("real_name") or "Unknown"

        # Use largest available avatar image
        avatar_url = (
            profile.get("image_512")
            or profile.get("image_192")
            or profile.get("image_72")
            or None
        )

        return display_name, avatar_url

    except Exception as e:
        print(f"Error fetching Slack user {user_id}: {e}")
        return "Unknown", None

@app.event("message")
async def process_slack_message(event, logger):
    try:
        if event.get("type") == "message":
            subtype = event.get("subtype")

            if subtype == "message_deleted":
                slack_ts = event["deleted_ts"]
                discord_message_id = get_discord_id(slack_ts)
                if discord_message_id is not None:
                    channel = event["channel"]
                    if channel in CHANNEL_IDS:
                        channel_name = await get_slack_channel_name(channel)
                        await delete_from_discord(channel_name, STOD[channel], discord_message_id)
                return

            if subtype == "message_changed":
                slack_ts = event["message"]["ts"]
                discord_message_id = get_discord_id(slack_ts)
                if discord_message_id is not None:
                    channel = event["channel"]
                    if channel in CHANNEL_IDS:
                        user = event["message"]["user"]
                        new_text = event["message"]["text"]
                        if NOFW in new_text:
                            logging.info("[NOFW] detected - skipped editing")
                            return
                        channel_name = await get_slack_channel_name(channel)
                        user_name = await get_slack_user_name(user)
                        await edit_at_discord(new_text, user_name, channel_name, STOD[channel], discord_message_id)
                return

            user = event.get("user")
            if not user:
                return

            message_text = event["text"]
            if NOFW in message_text:
                logging.info("[NOFW] detected - skipped sending")
                return

            channel = event.get("channel")
            if channel in CHANNEL_IDS and user not in monitored_users:
                monitored_users.add(user)
                logging.info(f"Added user {user} to monitored users.")

            if channel in CHANNEL_IDS and user in monitored_users:
                channel_name = await get_slack_channel_name(channel)
                user_name = await get_slack_user_name(user)
                # ファイル添付の確認
                files = event.get("files", [])
                file_objs = None
                if files:
                    file_objs = await get_file_objs(files)
                
                slack_ts = event["ts"]

                # handle_slack_events メソッド内の send_to_discord の呼び出し部分
                await send_to_discord(
                    message_text=message_text,
                    user_name=user_name,
                    channel_name=channel_name,
                    channel_id=STOD[channel],
                    slack_ts=slack_ts,
                    file_objs=file_objs
                )
    except Exception as e:
        logging.error(f"Error handling Slack event: {e}")
        logging.debug(f"Event data: {event}")

@app.event("file_shared")
async def handle_file_shared(event, logger):
    file_id = event['file_id']
    resp = await slack_client.files_info(file=file_id)
    if not resp["ok"]:
        return
    file = resp["file"]
    filename = file["name"]
    username = await get_slack_user_name(file["user"])
    logger.info(f"Slack event: File {filename} shared by {username}")

@app.event("file_created")
async def handle_file_created(event, logger):
    file_id = event['file_id']
    resp = await slack_client.files_info(file=file_id)
    if not resp["ok"]:
        return
    file = resp["file"]
    filename = file["name"]
    username = await get_slack_user_name(file["user"])
    logger.info(f"Slack event: File {filename} created by {username}")

async def start_slack_bot():
    slack_handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await slack_handler.start_async()

