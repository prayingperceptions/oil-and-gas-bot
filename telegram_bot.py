import asyncio
import aiohttp
import os
import logging
import datetime
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
logger = logging.getLogger(__name__)

# ── Heartbeat interval (seconds). Default: every 6 hours ────────────────
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "21600"))


async def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not found. Skipping alert.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    text = await response.text()
                    logger.error(f"Failed to send Telegram alert: {text}")
                else:
                    logger.info("Telegram alert sent successfully.")
    except Exception as e:
        logger.error(f"Exception sending Telegram alert: {e}")


async def heartbeat_loop():
    """Send a periodic heartbeat message to Telegram so you know the bot is alive."""
    logger.info(f"Heartbeat loop started (interval: {HEARTBEAT_INTERVAL}s)")
    start_time = datetime.datetime.now(datetime.timezone.utc)

    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        uptime = datetime.datetime.now(datetime.timezone.utc) - start_time
        hours = int(uptime.total_seconds() // 3600)
        minutes = int((uptime.total_seconds() % 3600) // 60)
        await send_telegram_alert(
            f"💚 <b>Heartbeat</b>\n\n"
            f"Bot is alive and running.\n"
            f"Uptime: <b>{hours}h {minutes}m</b>"
        )
