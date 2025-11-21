from fnmatch import translate
import discord
from discord.ext import commands
from discord import app_commands
from utils.logger import log_event
from utils.formatter import format_message
from utils.embed_utils import create_error_embed, create_notification_embed
from config import *
import logging
from slack_sdk.web.async_client import AsyncWebClient
from utils.emoji_mapper import EmojiMapper
from datetime import datetime, timedelta, time
import asyncio
import aiohttp
import io
import os
import chardet
from services.news_service import NewsService
from services.database_service import *
import pytz
import psutil
from typing import Literal
import json
from datetime import datetime, date
from xml.etree import ElementTree
import re
from typing import Optional, List

SCHEDULE_FILE = "data/schedules.json" # スケジュールデータを保存するファイル
FAVORITES_FILE = "data/favorites.json" # お気に入り論文を保存するファイル

# スケジュールデータを保存するディレクトリの作成
os.makedirs("data", exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True

# Botクラスを拡張
class LabBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_time = datetime.now()

# Botインスタンスの作成を修正
bot = LabBot(command_prefix="!", intents=intents)

slack_client = AsyncWebClient(token=SLACK_BOT_TOKEN)

# メッセージ転送履歴を追跡するためのキャッシュ
message_cache = {}

async def get_slack_user_name(user_id):
    user_info = await slack_client.users_info(user=user_id)
    if not user_info["ok"]:
        return None
    profile = user_info["user"]["profile"]
    return profile.get("display_name") or profile.get("real_name") or "Unnamed"

async def get_slack_channel_name(channel_id):
    channel_info = await slack_client.conversations_info(channel=channel_id)
    if not channel_info["ok"]:
        return None
    return channel_info["channel"]["name"]

slack_bold_re   = re.compile(r"\*(.+?)\*")
# slack_italic_re = re.compile(r"_(.+?)_")
slack_strike_re = re.compile(r"~(.+?)~")

def stod_format(text: str) -> str:
    text = slack_bold_re.sub(r"**\1**", text)
    # text = slack_italic_re.sub(r"*\1*", text)
    text = slack_strike_re.sub(r"~~\1~~", text)
    return text

discord_bold_re   = re.compile(r"_\*(.+?)\*_")
discord_italic_re = re.compile(r"(?<!\*)\*(.+?)\*(?!\*)")  # avoid bold conflicts
discord_strike_re = re.compile(r"~~(.+?)~~")

def dtos_format(text: str) -> str:
    text = discord_italic_re.sub(r"_\1_", text)    # * to _
    text = discord_bold_re.sub(r"*\1*", text)      # ** to *
    text = discord_strike_re.sub(r"~\1~", text)    # ~~ to ~
    return text

slack_link_pattern = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")

def stod_links(text: str) -> str:
    def repl(match):
        url, label = match.groups()
        if label.startswith("http://") or label.startswith("https://"):
            return url  # just the raw link
        return f"[{label}]({url})"
    return slack_link_pattern.sub(repl, text)

discord_md_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")

def dtos_links(text: str) -> str:
    return discord_md_pattern.sub(r"<\2|\1>", text)

async def stod_mentions(text):
    # special mentions
    text = text.replace("&gt;", ">")
    text = text.replace("<!channel>", "@everyone")
    text = text.replace("<!here>", "@here")

    # mapping
    for s, d in STOD_MAP:
        text = text.replace(s, d)
    for s, d in DOUBLE_MAP:
        text = text.replace(s, d)

    # Replace <@U12345> with display names
    user_pattern = re.compile(r"<@(U[A-Z0-9]+)>")
    channel_pattern = re.compile(r"<#(C[A-Z0-9]+)(?:|[^>]*)?>")

    async def user_replacer(match):
        user_id = match.group(1)
        name = await get_slack_user_name(user_id)
        return f"*@{name}*" if name else "*@Unknown*"

    async def channel_replacer(match):
        channel_id = match.group(1)
        name = await get_slack_channel_name(channel_id)
        return f"*#{name}*" if name else "*#Unknown*"

    async def async_sub(pattern: re.Pattern, repl, text):
        matches = list(pattern.finditer(text))
        if not matches:
            return text

        # Build the new string manually
        result = []
        last_end = 0
        for match in matches:
            result.append(text[last_end:match.start()])
            replacement = await repl(match)
            result.append(replacement)
            last_end = match.end()
        result.append(text[last_end:])
        return ''.join(result)

    text = await async_sub(user_pattern, user_replacer, text)
    return await async_sub(channel_pattern, channel_replacer, text)

def dtos_mentions(message):
    text = message.content

    # special mentions
    text = text.replace("@everyone", "<!channel>")
    text = text.replace("@here", "<!here>")

    # mapping
    for d, s in DTOS_MAP:
        text = text.replace(d, s)
    for s, d in DOUBLE_MAP:
        text = text.replace(d, s)

    # User mentions
    for user in message.mentions:
        text = text.replace(f"<@{user.id}>", f"**@{user.display_name}**")

    # Channel mentions
    for channel in message.channel_mentions:
        text = text.replace(f"<#{channel.id}>", f"**#{channel.name}**")

    # Role mentions
    for role in message.role_mentions:
        text = text.replace(f"<@&{role.id}>", f"**@{role.name}**")

    return text

async def stod_all(text):
    text = await stod_mentions(text)
    return stod_format(stod_links(text))

def dtos_all(message):
    text = dtos_mentions(message)
    return dtos_format(dtos_links(text))

# チャンネルチェックデコレータ
def arxiv_channel_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.channel_id != DISCORD_ARXIV_CHANNEL_ID:
            await interaction.response.send_message(
                "このコマンドは arXiv チャンネルでのみ使用できます。",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)

# お気に入り論文の読み込み関数を修正
def load_favorites():
    try:
        if os.path.exists(FAVORITES_FILE):
            with open(FAVORITES_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
                if content:
                    return json.loads(content)
        # ファイルが存在しないか空の場合は空の辞書を返す
        return {}
    except json.JSONDecodeError as e:
        logging.error(f"JSONデコードエラー: {e}")
        return {}
    except Exception as e:
        logging.error(f"ファイル読み込みエラー: {e}")
        return {}

# お気に入り論文の保存関数を修正
def save_favorites(favorites):
    try:
        # データディレクトリが存在しない場合は作成
        os.makedirs(os.path.dirname(FAVORITES_FILE), exist_ok=True)
        with open(FAVORITES_FILE, 'w', encoding='utf-8') as f:
            json.dump(favorites, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"ファイル保存エラー: {e}")

@bot.event
async def on_ready():
    print(f"{bot.user} is now running!")
    try:
        # コマンドの同期
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")

        # ニュース投稿タスクの開始
        bot.loop.create_task(schedule_news())
        
        # ステータス更新タスクの開始
        bot.loop.create_task(update_bot_status())
        
        # サーバー情報をログに記録
        logging.info(f"Connected to {len(bot.guilds)} servers")
        for guild in bot.guilds:
            logging.info(f"Server: {guild.name} (ID: {guild.id})")
            logging.info(f"Members: {guild.member_count}")
            logging.info(f"Channels: {len(guild.channels)}")
            
    except Exception as e:
        print(f"Failed to sync commands: {e}")

# ステータス更新用の関数を追加
async def update_bot_status():
    """定期的にボットのステータスを更新"""
    while True:
        try:
            # 接続時間を計算
            uptime = datetime.now() - bot.start_time
            hours = uptime.total_seconds() // 3600
            minutes = (uptime.total_seconds() % 3600) // 60

            # システム情報を取得
            memory_usage = psutil.Process().memory_info().rss / 1024 / 1024  # MB
            cpu_percent = psutil.Process().cpu_percent()
            
            # ネットワーク情報を取得
            net_io = psutil.net_io_counters()
            network_speed = (net_io.bytes_sent + net_io.bytes_recv) / 1024 / 1024  # MB
            
            # ステータス文字列を作成
            status_details = f"CPU: {cpu_percent:.1f}% | MEM: {memory_usage:.1f}MB"
            status_state = f"NET: {network_speed:.1f}MB/s"
            
            # ステータスを更新
            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name=f"稼働時間: {int(hours)}時間{int(minutes)}分",
                details=status_details,
                state=status_state
            )
            await bot.change_presence(
                status=discord.Status.online,
                activity=activity
            )
            await asyncio.sleep(60)  # 1分ごとに更新

        except Exception as e:
            logging.error(f"ステータス更新エラー: {e}")
            await asyncio.sleep(60)

async def schedule_news():
    """毎朝9時にニュースを投稿するスケジューラー"""
    try:
        news_service = NewsService(bot)
        japan_tz = pytz.timezone('Asia/Tokyo')

        while True:
            now = datetime.now(japan_tz)
            target_time = time(hour=9, minute=0)  # datetime.timeを使用

            # 次の実行時刻を計算
            if now.time() >= target_time:
                tomorrow = now.date() + timedelta(days=1)
                next_run = datetime.combine(tomorrow, target_time)
            else:
                next_run = datetime.combine(now.date(), target_time)

            next_run = japan_tz.localize(next_run)
            delay = (next_run - now).total_seconds()

            await asyncio.sleep(delay)
            await news_service.post_news()
    except Exception as e:
        logging.error(f"ニュース配信スケジューラーでエラーが発生: {e}")

@bot.event
async def on_message(message: discord.Message):
    # Botからのメッセージは完全に無視
    if message.author.bot:
        return
    if NOFW in message.content:
        logging.info("[NOFW] detected - skipped sending")
        return
    
    # コマンド処理を優先
    await bot.process_commands(message)

    if message.channel.id == DISCORD_CHANNEL_ID_1 or message.channel.id == DISCORD_CHANNEL_ID_2:
        channel_id = DTOS[message.channel.id]
        try:
            if message.reference and message.type is not discord.MessageType.reply:
                ref = message.reference
                while ref:
                    channel = bot.get_channel(ref.channel_id)
                    original = await channel.fetch_message(ref.message_id)
                    if original.type is discord.MessageType.reply:
                        break
                    ref = original.reference
                file_ids = None
                if original.attachments:
                    file_ids = await get_file_ids(original.attachments)
                await send_to_slack(original, message.author, channel_id, file_ids=file_ids, fw_from=original.author, fw_id=message.id)
            else:
                file_ids = None
                if message.attachments:
                    file_ids = await get_file_ids(message.attachments)
                await send_to_slack(message, message.author, channel_id, file_ids=file_ids)

            logging.info(f"Message and files forwarded from Discord user {message.author.name}")

        except Exception as e:
            logging.error(f"Failed to send message or files to Slack: {e}")
        return

    # 通常のメッセージ処理（スラッシュコマンドではない場合のみ）
    # elif not message.content.startswith('/'):
    #     log_event(f"Discord メッセージ受信: {message.content}")
    #     try:
    #         formatted_message = format_message(message.content)
    #         await message.channel.send(f"受信メッセージのフォーマット: {formatted_message}")
    #     except Exception as e:
    #         embed = create_error_embed("メッセージ処理エラー", str(e))
    #         await message.channel.send(embed=embed)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.author.bot:
        return
    if NOFW in after.content:
        logging.info("[NOFW] detected - skipped editing")
        return
    channel_id = SLACK_CHANNEL_ID_1
    if after.channel.id == DISCORD_CHANNEL_ID_1 or after.channel.id == DISCORD_CHANNEL_ID_2:
        if after.channel.id == DISCORD_CHANNEL_ID_2:
            channel_id = SLACK_CHANNEL_ID_2
        slack_ts = get_slack_ts(str(after.id))
        try:
            # テキストメッセージの転送
            if slack_ts is not None:
                await update_to_slack(after, after.author, channel_id, slack_ts)
                logging.info(f"Message edited from Discord user {after.author.name}")
        except Exception as e:
            logging.error(f"Failed to edit message to Slack: {e}")
        return

@bot.event
async def on_message_delete(message: discord.Message):
    channel_id = SLACK_CHANNEL_ID_1
    if message.channel.id == DISCORD_CHANNEL_ID_1 or message.channel.id == DISCORD_CHANNEL_ID_2:
        if message.channel.id == DISCORD_CHANNEL_ID_2:
            channel_id = SLACK_CHANNEL_ID_2
        slack_ts = get_slack_ts(str(message.id))
        try:
            # テキストメッセージの転送
            if slack_ts is not None:
                await delete_from_slack(message, channel_id, slack_ts)
                logging.info(f"Message deleted from Discord user {message.author.name}")
        except Exception as e:
            logging.error(f"Failed to delete message from Slack: {e}")
        return

@bot.tree.command(name="notify")
async def notify(interaction: discord.Interaction, user: discord.Member, *, content: str):
    channel = bot.get_channel(DISCORD_CHANNEL_ID_1)
    if channel:
        embed = create_notification_embed("通知", content, category="High")
        await channel.send(f"{user.mention}", embed=embed)
        await interaction.response.send_message(f"通知を送信しました: {content}", ephemeral=True)
    else:
        await interaction.response.send_message("通知チャンネルが見つかりません。", ephemeral=True)

# 管理者権限チェック用のデコレータを作成
def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        # 管理者権限またはロールを持っているかチェック
        has_role = any(role.id == DISCORD_ROLE_ID for role in interaction.user.roles)
        if not (interaction.user.guild_permissions.administrator or has_role):
            await interaction.response.send_message(
                "このコマンドは管理者権限または必要なロールが必要です。",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)

# logコマンドに管理者権限チェックを追加
# チャンネル制限用デコレータを追加
def log_channel_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.channel_id != DISCORD_LOG_CHANNEL_ID:
            await interaction.response.send_message(
                f"このコマンドは <#{DISCORD_LOG_CHANNEL_ID}> チャンネルでのみ使用できます。",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)

# logコマンドを修正
@bot.tree.command(name="log")
@is_admin()
@log_channel_only()
async def log(interaction: discord.Interaction):
    """
    最新のログを表示します（管理者のみ）
    """
    try:
        # ファイルのエンコーディングを自動検出
        with open("logs.txt", 'rb') as f:
            raw_data = f.read()
            detected = chardet.detect(raw_data)
            encoding = detected['encoding']

        # 検出されたエンコーディングでファイルを読み込み
        with open("logs.txt", "r", encoding=encoding) as f:
            logs = f.readlines()

        # 最新の10行を取得
        recent_logs = ''.join(logs[-10:])

        # 文字列が空でないことを確認
        if not recent_logs.strip():
            await interaction.response.send_message(
                "ログが空です。",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"最新のログ (エンコーディング: {encoding}):\n```\n{recent_logs}\n```",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"ログの読み取りに失敗しました。エラー: {str(e)}\n"
            f"エンコーディング: {encoding if 'encoding' in locals() else '不明'}",
            ephemeral=True
        )
        logging.error(f"ログ読み取りエラー: {e}")

@bot.tree.command(
    name="log_delete",
    description="ログファイルの内容を削除します（管理者のみ）"
)
@is_admin()
@log_channel_only()
async def log_delete(interaction: discord.Interaction):
    """ログファイルの内容を削除します（管理者のみ）"""
    try:
        # ファイルを空にする
        with open("logs.txt", "w", encoding='utf-8') as f:
            f.write("")

        embed = discord.Embed(
            title="✅ ログ削除完了",
            description="ログファイルの内容を削除しました。",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        embed.add_field(
            name="実行者",
            value=f"{interaction.user.name} ({interaction.user.id})",
            inline=False
        )
        
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )
        
        # ログにも記録
        logging.info(f"ログファイルが {interaction.user.name} によって削除されました")
        
    except Exception as e:
        error_embed = discord.Embed(
            title="❌ エラー",
            description=f"ログファイルの削除中にエラーが発生しました：{str(e)}",
            color=discord.Color.red()
        )
        await interaction.response.send_message(
            embed=error_embed,
            ephemeral=True
        )
        logging.error(f"ログ削除エラー: {e}")

@bot.tree.command(
    name="news",
    description="最新のテックニュースを取得します"
)
async def news(interaction: discord.Interaction, default: bool = False):
    try:
        if interaction.channel_id != DISCORD_NEWS_CHANNEL_ID:
            await interaction.response.send_message(
                f"このコマンドは <#{DISCORD_NEWS_CHANNEL_ID}> チャンネルでのみ使用できます。",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        news_service = NewsService(bot)
        # ニュース取得中にデフォルトのニュースを表示
        if default:
            default_article = {
                "title": "【重要】Github、のフォローのお願い",
                "description": "個人開発した内容やAIの最新ニュースなどを発信中！是非フォローしてね！",
                "url": "https://github.com/paraccoli",
                "urlToImage": "https://ujise.com/wp-content/uploads/2022/08/GitHub-Logo.png",
                "source": {"name": "研究室Bot News"}
            }
            embed = news_service.create_news_embed(default_article)
            await interaction.followup.send(
                content="🌟 今日のピックアップニュース",
                embed=embed,
                ephemeral=True
            )
            return

        try:
            articles = await asyncio.wait_for(
                news_service.fetch_news(),
                timeout=15.0
            )
            
            if not articles:
                # デフォルトのニュース情報を作成
                default_article = {
                    "title": "【重要】Github、のフォローのお願い",
                    "description": "個人開発した内容やAIの最新ニュースなどを発信中！是非フォローしてね！",
                    "url": "https://github.com/paraccoli",
                    "urlToImage": "https://ujise.com/wp-content/uploads/2022/08/GitHub-Logo.png",
                    "source": {"name": "研究室Bot News"}
                }
                embed = news_service.create_news_embed(default_article)
                await interaction.followup.send(
                    content="🌟 今日のピックアップニュース",
                    embed=embed,
                    ephemeral=True
                )
                return

            for i, article in enumerate(articles[:5]):
                if embed := news_service.create_news_embed(article):
                    prefix = "🌟 今日のテックニュース" if i == 0 else ""
                    await interaction.followup.send(
                        content=prefix,
                        embed=embed,
                        ephemeral=True
                    )
                await asyncio.sleep(0.5)

        except asyncio.TimeoutError:
            await interaction.followup.send(
                "ニュースの取得がタイムアウトしました。",
                ephemeral=True
            )
        except Exception as e:
            logging.error(f"ニュース取得エラー: {str(e)}")
            await interaction.followup.send(
                "ニュースの取得中にエラーが発生しました。",
                ephemeral=True
            )

    except Exception as e:
        logging.error(f"コマンド実行エラー: {str(e)}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "エラーが発生しました。",
                ephemeral=True
            )


# arXiv関連のコマンドグループ
@bot.tree.command(
    name="arxiv_search",
    description="arXivから論文を検索します"
)
@arxiv_channel_only()
async def arxiv_search(interaction: discord.Interaction, query: str):
    try:
        url = f'http://export.arxiv.org/api/query?search_query=all:{query}&start=0&max_results=5'
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    content = await response.text()
                    root = ElementTree.fromstring(content)
                    entries = root.findall('{http://www.w3.org/2005/Atom}entry')

                    if not entries:
                        await interaction.response.send_message("論文が見つかりませんでした。", ephemeral=True)
                        return

                    embed = discord.Embed(
                        title=f"検索結果 (キーワード: {query})",
                        description="IDをコピーするには、IDの行を選択してコピーしてください。",
                        color=discord.Color.blue()
                    )
                    
                    for entry in entries:
                        title = entry.find('{http://www.w3.org/2005/Atom}title').text
                        link = entry.find('{http://www.w3.org/2005/Atom}id').text
                        paper_id = link.split('/')[-1]
                        
                        # タイトルとキーワードを組み合わせて表示
                        keywords = [kw.strip() for kw in query.split(',')]
                        keyword_text = " | ".join([f"🔑={kw}" for kw in keywords])
                        
                        embed.add_field(
                            name=f"📄 論文情報",
                            value=(
                                f"**タイトル**: {title}\n"
                                f"**キーワード**: {keyword_text}\n"
                                f"**ID**: `{paper_id}`\n"
                                f"**リンク**: [arXiv]({link})"
                            ),
                            inline=False
                        )
                    
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message("APIの呼び出しに失敗しました。", ephemeral=True)
    except Exception as e:
        logging.error(f"arXiv検索エラー: {e}")
        await interaction.response.send_message("検索中にエラーが発生しました。", ephemeral=True)

@bot.tree.command(
    name="arxiv_save",
    description="論文をお気に入りに保存します"
)
@arxiv_channel_only()
async def arxiv_save(interaction: discord.Interaction, paper_id: str):
    try:
        favorites = load_favorites()
        user_id = str(interaction.user.id)
        
        if user_id not in favorites:
            favorites[user_id] = []
        
        # 既に保存済みかチェック
        if paper_id in [paper['id'] for paper in favorites[user_id]]:
            await interaction.response.send_message("この論文は既に保存されています。", ephemeral=True)
            return
        
        url = f'http://export.arxiv.org/api/query?id_list={paper_id}'
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    content = await response.text()
                    root = ElementTree.fromstring(content)
                    entry = root.find('{http://www.w3.org/2005/Atom}entry')
                    
                    if entry:
                        title = entry.find('{http://www.w3.org/2005/Atom}title').text
                        # 新しい論文を追加
                        favorites[user_id].append({
                            'id': paper_id,
                            'title': title,
                            'saved_at': datetime.now().isoformat()
                        })
                        # 変更を保存
                        save_favorites(favorites)
                        
                        await interaction.response.send_message(
                            f"論文を保存しました:\nID: {paper_id}\nTitle: {title}",
                            ephemeral=True
                        )
                    else:
                        await interaction.response.send_message("論文が見つかりませんでした。", ephemeral=True)
                else:
                    await interaction.response.send_message("APIの呼び出しに失敗しました。", ephemeral=True)
    except Exception as e:
        logging.error(f"論文保存エラー: {e}")
        await interaction.response.send_message("保存中にエラーが発生しました。", ephemeral=True)


@bot.tree.command(
    name="arxiv_list",
    description="保存した論文の一覧を表示します"
)
@arxiv_channel_only()
async def arxiv_list(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    favorites = load_favorites()
    target_user = user or interaction.user
    user_id = str(target_user.id)
    
    if user_id not in favorites or not favorites[user_id]:
        await interaction.response.send_message(
            f"{target_user.display_name}の保存済み論文はありません。",
            ephemeral=True
        )
        return
    
    embed = discord.Embed(
        title=f"{target_user.display_name}の保存済み論文",
        color=discord.Color.blue()
    )
    
    for paper in favorites[user_id]:
        embed.add_field(
            name=f"ID: {paper['id']}",
            value=f"Title: {paper['title']}\nSaved: {paper['saved_at']}",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(
    name="arxiv_delete",
    description="保存した論文を削除します"
)
@arxiv_channel_only()
async def arxiv_delete(interaction: discord.Interaction, paper_id: str):
    favorites = load_favorites()
    user_id = str(interaction.user.id)
    
    if user_id not in favorites or not any(p['id'] == paper_id for p in favorites[user_id]):
        await interaction.response.send_message("指定された論文は保存されていません。", ephemeral=True)
        return
    favorites[user_id] = [p for p in favorites[user_id] if p['id'] != paper_id]
    save_favorites(favorites)
    await interaction.response.send_message("論文を削除しました。", ephemeral=True)


@bot.tree.command(
    name="help",
    description="Botの機能と使い方を表示します"
)
async def help(interaction: discord.Interaction):
    """Botの機能と使い方を表示します。"""
    try:
        embed = discord.Embed(
            title="🤖 研究室Bot ヘルプ",
            description="研究室用の高機能コミュニケーションBotです。\nSlack連携や論文管理、ニュース配信など様々な機能を提供します。",
            color=discord.Color.blue()
        )


        # 基本コマンド
        embed.add_field(
            name="📝 基本コマンド",
            value=(
                "```\n"
                "/help - このヘルプを表示\n"
                "/news [default:True/False] - 最新のテックニュースを表示\n"
                "   - default:True で研究室Bot開発者の情報を表示\n"
                "/notify [@ユーザー] [内容] - 指定したユーザーに通知を送信\n"
                "```"
            ),
            inline=False
        )

        # 論文管理機能
        embed.add_field(
            name="📚 論文管理機能",
            value=(
                "```\n"
                "/arxiv_search [クエリ] - arXivから論文を検索\n"
                "   - 検索結果にキーワードと簡単コピー用IDを表示\n"
                "/arxiv_save [論文ID] - 論文をお気に入りに保存\n"
                "/arxiv_list [ユーザー] - 保存した論文の一覧を表示\n"
                "/arxiv_delete [論文ID] - 保存した論文を削除\n"
                "```\n"
                f"※ これらのコマンドは <#{DISCORD_ARXIV_CHANNEL_ID}> チャンネルでのみ使用可能です。"
            ),
            inline=False
        )

        # 管理者用コマンド
        embed.add_field(
            name="👑 管理者用コマンド",
            value=(
                "```\n"
                "/log - 最新のログを表示\n"
                "/log_delete - ログファイルの内容を削除\n"
                "```\n"
                f"※ これらのコマンドは <#{DISCORD_LOG_CHANNEL_ID}> チャンネルでのみ使用可能です。"
            ),
            inline=False
        )

        # 統計・管理機能
        embed.add_field(
            name="📊 統計・管理",
            value=(
                "```\n"
                "/stats - システムとBotの統計情報を表示\n"
                "/schedule add [日付] [内容] [カテゴリ] - 予定を追加\n"
                "   - カテゴリ: ミーティング/セミナー/締切/その他\n"
                "/schedule show - 予定一覧を表示\n"
                "/schedule delete [日付] - 予定を削除\n"
                "```"
            ),
            inline=False
        )

        # 自動機能
        embed.add_field(
            name="🔄 自動機能",
            value=(
                "• Slack ⇔ Discord メッセージ双方向連携\n"
                "• ファイル転送対応（画像・文書など）\n"
                "• リアクション同期（絵文字反応の共有）\n"
                "• 毎朝9時の自動ニュース配信\n"
                "• ボットステータスの自動更新（CPU/メモリ/ネットワーク）"
            ),
            inline=False
        )

        # チャンネル制限
        embed.add_field(
            name="📢 チャンネル制限",
            value=(
                f"• `/news`: <#{DISCORD_NEWS_CHANNEL_ID}> のみ\n"
                f"• `/arxiv_*`: <#{DISCORD_ARXIV_CHANNEL_ID}> のみ\n"
                f"• `/log`, `/log_delete`: <#{DISCORD_LOG_CHANNEL_ID}> のみ\n"
                f"• Slack連携: <#{DISCORD_CHANNEL_ID_1}> のみ"
            ),
            inline=False
        )

        # ファイル制限
        embed.add_field(
            name="📎 ファイル転送制限",
            value=(
                f"• 最大サイズ: {MAX_FILE_SIZE // (1024 * 1024)}MB"
            ),
            inline=False
        )

        # フッター
        embed.set_footer(
            text=f"Bot稼働時間: {int((datetime.now() - bot.start_time).total_seconds() // 3600)}時間"
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    except Exception as e:
        logging.error(f"ヘルプ表示エラー: {e}")
        await interaction.response.send_message(
            "ヘルプの表示中にエラーが発生しました。",
            ephemeral=True
        )

@bot.tree.command(
    name="stats",
    description="サーバーの統計情報を表示します"
)
async def stats(interaction: discord.Interaction):
    """サーバーとBotの統計情報を表示します"""
    try:
        embed = discord.Embed(
            title="📊 システム統計情報",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )

        # システムリソース情報
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        cpu_freq = psutil.cpu_freq()
        
        embed.add_field(
            name="💻 システム情報",
            value=(
                f"CPU使用率: {psutil.cpu_percent()}%\n"
                f"CPU周波数: {cpu_freq.current:.1f}MHz\n"
                f"メモリ使用率: {memory.percent}%\n"
                f"ディスク使用率: {disk.percent}%"
            ),
            inline=False
        )

        # ネットワーク情報
        net_io = psutil.net_io_counters()
        net_speed = (net_io.bytes_sent + net_io.bytes_recv) / 1024 / 1024
        
        embed.add_field(
            name="🌐 ネットワーク",
            value=(
                f"送信: {net_io.bytes_sent / 1024 / 1024:.1f}MB\n"
                f"受信: {net_io.bytes_recv / 1024 / 1024:.1f}MB\n"
                f"現在の速度: {net_speed:.1f}MB/s"
            ),
            inline=True
        )

        # Bot統計
        uptime = datetime.now() - bot.start_time
        embed.add_field(
            name="🤖 Bot統計",
            value=(
                f"稼働時間: {int(uptime.total_seconds() // 3600)}時間\n"
                f"監視メッセージ: {len(message_cache)}件\n"
                f"メモリ使用量: {psutil.Process().memory_info().rss / 1024 / 1024:.1f}MB"
            ),
            inline=True
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    except Exception as e:
        logging.error(f"統計表示エラー: {e}")
        await interaction.response.send_message(
            "統計情報の取得中にエラーが発生しました。",
            ephemeral=True
        )

@bot.tree.command(
    name="schedule",
    description="研究室のスケジュールを管理します"
)
async def schedule(
    interaction: discord.Interaction,
    action: Literal["add", "show", "delete"],
    date: str = None,
    event: str = None,
    category: Literal["ミーティング", "セミナー", "締切", "その他"] = "その他"
):
    try:
        # スケジュールデータの読み込み
        if os.path.exists(SCHEDULE_FILE):
            with open(SCHEDULE_FILE, 'r', encoding='utf-8') as f:
                schedules = json.load(f)
        else:
            schedules = {}

        if action == "add":
            if not date or not event:
                await interaction.response.send_message(
                    "日付と予定の内容を指定してください。",
                    ephemeral=True
                )
                return

            try:
                # 文字列を日付オブジェクトに変換
                event_date = datetime.strptime(date, "%Y-%m-%d").date()
                today = datetime.now().date()  # 現在の日付を取得
                
                # 過去の日付かどうかをチェック
                if event_date < today:
                    await interaction.response.send_message(
                        "過去の日付は指定できません。",
                        ephemeral=True
                    )
                    return
            except ValueError:
                await interaction.response.send_message(
                    "日付の形式が正しくありません。YYYY-MM-DD形式で指定してください。",
                    ephemeral=True
                )
                return

            if date not in schedules:
                schedules[date] = []
            
            schedules[date].append({
                "event": event,
                "category": category,
                "created_by": str(interaction.user),
                "created_at": datetime.now().isoformat()
            })

            embed = discord.Embed(
                title="📅 予定を追加しました",
                description=f"日付: {date}\n予定: {event}\nカテゴリ: {category}",
                color=discord.Color.green()
            )

        elif action == "show":
            embed = discord.Embed(
                title="📅 スケジュール一覧",
                color=discord.Color.blue()
            )

            if not schedules:
                embed.description = "予定はありません。"
            else:
                for date in sorted(schedules.keys()):
                    events = schedules[date]
                    if events:
                        event_text = "\n".join(
                            f"• [{e['category']}] {e['event']}" for e in events
                        )
                        embed.add_field(
                            name=f"📌 {date}",
                            value=event_text,
                            inline=False
                        )

        elif action == "delete":
            if not date:
                await interaction.response.send_message(
                    "削除する予定の日付を指定してください。",
                    ephemeral=True
                )
                return

            if date in schedules:
                del schedules[date]
                embed = discord.Embed(
                    title="🗑️ 予定を削除しました",
                    description=f"日付: {date}の予定を全て削除しました。",
                    color=discord.Color.red()
                )
            else:
                embed = discord.Embed(
                    title="❌ エラー",
                    description=f"日付: {date}の予定は見つかりませんでした。",
                    color=discord.Color.red()
                )

        # スケジュールデータの保存
        with open(SCHEDULE_FILE, 'w', encoding='utf-8') as f:
            json.dump(schedules, f, ensure_ascii=False, indent=2)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    except Exception as e:
        logging.error(f"スケジュール管理エラー: {e}")
        await interaction.response.send_message(
            "スケジュールの管理中にエラーが発生しました。",
            ephemeral=True
        )

        # async with aiohttp.ClientSession() as session:
        #     async with session.get(file_url) as resp:
        #         if resp.status == 200:
        #             file_content = await resp.read()
        #             file_size = len(file_content)
        #             if file_size > MAX_FILE_SIZE:
        #                 logging.error(f"ファイルサイズが大きすぎます: {file_size} bytes")
        #                 return
        #             return file_content
        #         else:

async def get_file_objs(files):
    logging.info(f"Downloading files from Slack...")
    
    file_objs = []
    for file in files:
        try:
            # ファイルサイズと種類のチェック
            file_size = file.get("size", 0)
            if file_size > MAX_FILE_SIZE:
                logging.error(f"ファイルサイズが大きすぎます: {file_size} bytes")
                continue
            # ファイルURLと認証情報を取得
            file_url = file["url_private_download"]
            headers = {"Authorization": f"Bearer {SLACK_USER_TOKEN}"}
            # ファイルをダウンロード
            file_content = await file_download(file_url, headers=headers)
            if file_content is None:
                continue
            file_objs.append((file["name"], file_content))

        except Exception as e:
            logging.error(f"File download error: {e}")
            continue
    
    return file_objs
    

async def send_to_discord(message_text, user_name: str, channel_name: str, channel_id, slack_ts, file_objs=None):
    logging.info(f"Sending to Discord from {user_name} in {channel_name}")
    channel = bot.get_channel(channel_id)

    if channel:
        content = f"**{user_name}**"
        if not channel_name.startswith("42_"):
            content += f' - *#{channel_name.replace('_', '\\_')}*'
        content += f":\n{await stod_all(message_text)}"
        if file_objs:
            files = [discord.File(io.BytesIO(file_content), filename=filename) for filename, file_content in file_objs]
            message = await channel.send(content, files=files)
            logging.info("Files sent: " + ", ".join(file[0] for file in file_objs))
        else:
            message = await channel.send(content)
        logging.info("Message sent to Discord successfully")
        save_mapping(slack_ts=slack_ts, discord_id=message.id)
    else:
        logging.error("Discord通知チャンネルが見つかりません")

async def edit_at_discord(message_text, user_name, channel_name, channel_id, discord_id):

    logging.info(f"Editing message at Discord from {user_name} in {channel_name}")
    channel = bot.get_channel(channel_id)

    if channel:
        try:
            message = await channel.fetch_message(discord_id)
            content = f"**{user_name}**"
            if not channel_name.startswith("42_"):
                content += f' - *#{channel_name.replace('_', '\\_')}*'
            content += f":\n{await stod_all(message_text)}"
            await message.edit(content=content)
        except discord.NotFound:
            logging.error(f"Error: Message with ID {discord_id} not found.")
        except discord.Forbidden:
            logging.error("Error: The bot does not have permissions to edit this message.")
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
        else:
            logging.info("Message edited at Discord successfully")
    else:
        logging.error("Discord通知チャンネルが見つかりません")

async def delete_from_discord(channel_name, channel_id, discord_id):
    logging.info(f"Deleting message from Discord from {channel_name}")
    channel = bot.get_channel(channel_id)

    if channel:
        try:
            message = await channel.fetch_message(discord_id)
            await message.delete()
        except discord.NotFound:
            logging.error(f"Error: Message with ID {discord_id} not found.")
        except discord.Forbidden:
            logging.error("Error: The bot does not have permissions to delete this message.")
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
        else:
            logging.info("Message deleted from Discord successfully")
    else:
        logging.error("Discord通知チャンネルが見つかりません")

async def send_to_slack(message, author, channel_id, file_ids=None, fw_from=None, fw_id=None):
    """
    メッセージの重複送信を防ぐためのキャッシュチェック付きSlack送信
    """
    message_id = str(message.id) if not fw_from else str(fw_id)
    if message_id in message_cache:
        return

    message_cache[message_id] = datetime.now()

    try:
        if file_ids:
            # Step 3: complete upload and share in channel
            if fw_from:
                comment = f"File shared by *@{author.display_name}* [_forwarded from *@{fw_from.display_name}*_]"
            else:
                comment = f"File shared by *@{author.display_name}*"
            await slack_client.files_completeUploadExternal(
                files=file_ids,
                channel_id=channel_id,
                initial_comment=comment
            )
        
        text = ''
        if fw_from:
            text = f"[_*@{fw_from.display_name}* から転送_]\n"
        text += dtos_all(message)
        response = await slack_client.chat_postMessage(
            channel=channel_id,
            username=author.display_name,
            text=text,
        )

        if response["ok"]:
            slack_ts = response["ts"]
            save_mapping(slack_ts=slack_ts, discord_id=message_id)

            # Slackのタイムスタンプをキャッシュに保存
            message_cache[message_id] = slack_ts

            # 古いキャッシュエントリの削除
            current_time = datetime.now()
            message_cache.update({k: v for k, v in message_cache.items() 
                                if current_time - datetime.fromtimestamp(float(v)) < timedelta(minutes=5)})

    except Exception as e:
        logging.error(f"Error sending message to Slack: {e}")

async def update_to_slack(message, user, channel_id, slack_ts):
    """
    メッセージの重複送信を防ぐためのキャッシュチェック付きSlack送信
    """

    try:
        response = await slack_client.chat_update(
            channel=channel_id,
            ts=slack_ts,
            text=dtos_all(message),
        )
        
    except Exception as e:
        logging.error(f"Error editing message to Slack: {e}")

async def delete_from_slack(message, channel_id, slack_ts):
    """
    メッセージの重複送信を防ぐためのキャッシュチェック付きSlack送信
    """
    try:
        response = await slack_client.chat_delete(channel=channel_id, ts=slack_ts)
        delete_mapping_by_discord(str(message.id))
    except Exception as e:
        logging.error(f"Error deleting message from Slack: {e}")

async def file_download(file_url, headers=None):
    """ファイルをダウンロードして転送する共通関数"""
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(file_url) as resp:
                if resp.status == 200:
                    file_content = await resp.read()
                    file_size = len(file_content)
                    if file_size > MAX_FILE_SIZE:
                        logging.error(f"ファイルサイズが大きすぎます: {file_size} bytes")
                        return
                    return file_content
                else:
                    logging.error(f"Download failed with status {resp.status}")
    except Exception as e:
        logging.error(f"ファイルダウンロードエラー: {e}")

def get_filename(file: discord.Attachment) -> str:
    if not file.title:
        return file.filename
    _, ext = os.path.splitext(file.filename)
    if ext and not file.title.endswith(ext):
        return file.title + ext
    return file.title

async def get_file_ids(files: List[discord.Attachment]):
    logging.info("Uploading files to Slack...")
    file_ids = []
    for file in files:
        try:
            # Step 0: download from URL
            result = await file_download(file.url)
            if result is None:
                return

            # Step 1: get upload URL
            resp1 = await slack_client.files_getUploadURLExternal(
                filename=get_filename(file),
                length=len(result),
            )
            upload_url = resp1.get("upload_url")
            file_id = resp1.get("file_id")

            if not upload_url or not file_id:
                logging.error(f"Failed to get upload URL: {resp1}")
                continue

            # Step 2: upload file data
            async with aiohttp.ClientSession() as session:
                http_resp = await session.post(upload_url, data=result, headers={"Content-Type": "application/octet-stream"})
                if http_resp.status != 200:
                    logging.error(f"Upload failed with status {http_resp.status}")
                    continue
                
            file_ids.append({"id": file_id})

        except Exception as e:
            logging.error(f"Error uploading file to Slack: {e}")
            continue
    
    return file_ids


async def send_file_to_slack(author, attachment, channel_id, fw_from=None, fw_id=None):
    """Discordのファイルを Slack に転送"""
    try:
        filename = attachment.filename
        # Step 0: download from URL
        result = await file_download(attachment.url)
        if result is None:
            return

        # Step 1: get upload URL
        resp1 = await slack_client.files_getUploadURLExternal(
            filename=filename,
            length=len(result),
        )
        upload_url = resp1.get("upload_url")
        file_id = resp1.get("file_id")

        if not upload_url or not file_id:
            logging.error(f"Failed to get upload URL: {resp1}")
            return

        # Step 2: upload file data
        async with aiohttp.ClientSession() as session:
            http_resp = await session.post(upload_url, data=result, headers={"Content-Type": "application/octet-stream"})
            if http_resp.status != 200:
                logging.error(f"Upload failed with status {http_resp.status}")
                return

        # Step 3: complete upload and share in channel
        if fw_from:
            comment = f"*{author.display_name}* [_*@{fw_from.display_name}* から転送_]:"
        else:
            comment = f"@{author.display_name}:*"
        resp3 = await slack_client.files_completeUploadExternal(
            files=[{"id": file_id}],
            channel_id=channel_id,
            initial_comment=comment
        )
        if resp3.get("ok"):
            logging.info(f"ファイル転送成功: {filename}")
        else:
            logging.error(f"Complete upload failed: {resp3}")

    except Exception as e:
        logging.error(f"Slackへのファイル転送エラー: {e}")

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return

    if reaction.message.channel.id == DISCORD_CHANNEL_ID_1 or reaction.message.channel.id == DISCORD_CHANNEL_ID_2:
        try:
            # メッセージIDをキーとしてSlackのts（タイムスタンプ）を取得
            slack_ts = message_cache.get(str(reaction.message.id))
            if slack_ts:
                emoji = EmojiMapper.discord_to_slack(str(reaction.emoji))
                if emoji:
                    await slack_client.reactions_add(
                        channel=DTOS[reaction.message.channel.id],
                        timestamp=slack_ts,
                        name=emoji.strip(':')
                    )
                    logging.info(f"Reaction synced to Slack: {emoji}")
        except Exception as e:
            logging.error(f"Failed to sync reaction to Slack: {e}")

@bot.event
async def on_reaction_remove(reaction, user):
    if user.bot:
        return

    if reaction.message.channel.id == DISCORD_CHANNEL_ID_1 or reaction.message.channel.id == DISCORD_CHANNEL_ID_2:
        try:
            slack_ts = message_cache.get(str(reaction.message.id))
            if slack_ts:
                emoji = EmojiMapper.discord_to_slack(str(reaction.emoji))
                if emoji:
                    await slack_client.reactions_remove(
                        channel=DTOS[reaction.message.channel.id],
                        timestamp=slack_ts,
                        name=emoji.strip(':')
                    )
                    logging.info(f"Reaction removed from Slack: {emoji}")
        except Exception as e:
            logging.error(f"Failed to remove reaction from Slack: {e}")

async def start_discord_bot():
    await bot.start(DISCORD_BOT_TOKEN)
