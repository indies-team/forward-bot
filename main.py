import asyncio
import logging
import tracemalloc

from bot.discord_bot import start_discord_bot
from bot.slack_bot import start_slack_bot
from config import LOG_LEVEL

# トレースバック追跡を有効化
tracemalloc.start()

logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

async def main():
    await asyncio.gather(
        start_discord_bot(),
        start_slack_bot(),
    )

if __name__ == "__main__":
    asyncio.run(main())
