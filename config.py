# APIトークンや設定
SLACK_USER_TOKEN = "YOUR_TOKEN"  # User OAuth Access Token
SLACK_BOT_TOKEN = "YOUR_TOKEN"  # Bot OAuth Access Token
SLACK_APP_TOKEN = "YOUR_TOKEN"  # Slack App Level Token
SLACK_VERIFICATION_TOKEN = "YOUR_TOKEN" # Verification Token
DISCORD_BOT_TOKEN = "YOUR_TOKEN" # Discord Bot Token
DATABASE_URL = "sqlite:///migration.db" # データベースのURL
NGROK_AUTH_TOKEN = "YOUR_TOKEN" # ngrokの認証トークン
NOFW = "[NOFW]"

# 通知専用チャンネルID
DISCORD_CHANNEL_ID_1 = 1234567890
DISCORD_CHANNEL_ID_2 = 1234567890
DISCORD_CHANNEL_ID_3 = 1234567890
SLACK_CHANNEL_ID_1 = "C0812345678"
SLACK_CHANNEL_ID_2 = "C0812345678"
SUB_CHANNEL_ID = 'C0812345678'

NOTIFY_CHANNEL_IDS = [
    'C0812345678',
]

STOD = {
    SLACK_CHANNEL_ID_1: DISCORD_CHANNEL_ID_1,
    SLACK_CHANNEL_ID_2: DISCORD_CHANNEL_ID_2,
    SUB_CHANNEL_ID: DISCORD_CHANNEL_ID_2,
}
for not_ch in NOTIFY_CHANNEL_IDS:
    STOD[not_ch] = DISCORD_CHANNEL_ID_3

DTOS = {DISCORD_CHANNEL_ID_1: SLACK_CHANNEL_ID_1, DISCORD_CHANNEL_ID_2: SLACK_CHANNEL_ID_2}

# (S, D) pairs
DOUBLE_MAP = [
    ("<@U12345678>", "<@12345678>"),
]

DTOS_MAP = [
    ("@ABC", "<@U12345678>"),
]

STOD_MAP = []

# Discord News Channel
DISCORD_NEWS_CHANNEL_ID = 1234567890
DISCORD_ROLE_ID = 1234567890
DISCORD_ARXIV_CHANNEL_ID = 1234567890
DISCORD_LOG_CHANNEL_ID = 1234567890

# ファイル転送の設定を追加

# 最大ファイルサイズ (50MB)
MAX_FILE_SIZE = 50 * 1024 * 1024

# NewsAPI設定
NEWS_API_KEY = "YOUR_API_KEY"
NEWS_KEYWORDS = [
    "機械学習", "AI", "Google", "OpenAI", "自動運転", "Waymo",
    "Machine Learning", "Artificial Intelligence", "Deep Learning"
]

# デバッグ設定
DEBUG_MODE = True

# ログ設定
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_LEVEL = 'DEBUG'

# Slack Bot Scopes
REQUIRED_BOT_SCOPES = [
    "channels:history",
    "channels:read",
    "chat:write",
    "files:read",
    "im:history",
    "users:read"
]

# App Level Token Scopes
REQUIRED_APP_SCOPES = [
    "connections:write",
    "authorizations:read"
]
