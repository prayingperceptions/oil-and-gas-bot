import asyncio
import logging
import signal
import sys
import datetime
from eia_sniper import eia_sniper_loop
from wti_tracer import wti_tracer_loop
from db import init_db
from telegram_bot import send_telegram_alert
from kalshi_client import KalshiClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

async def daily_pnl_loop():
    logger.info("Starting Daily PnL tracking loop...")
    kalshi = KalshiClient()
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        # Approximate 5PM EST (21:00 or 22:00 UTC)
        if now.hour in [21, 22] and now.minute == 0:
            try:
                if kalshi.private_key:
                    balance_data = await kalshi.get_balance()
                    balance_cents = balance_data.get("balance", 0)
                    available = balance_cents / 100.0
                    await send_telegram_alert(f"📊 <b>Daily Wrap-Up</b>\n\n💰 Live Kalshi Balance: <b>${available:.2f}</b>")
            except Exception as e:
                logger.error(f"PnL loop error: {e}")
            await asyncio.sleep(61) # Ensure doesn't trigger twice in same minute
        else:
            await asyncio.sleep(30)

async def main():
    logger.info("Starting Kalshi Energy Arb Bot...")
    await send_telegram_alert("🚀 Kalshi Energy Arb Bot is starting...")
    
    # Initialize SQLite database
    await init_db()

    # Create tasks for the continuous strategy loops
    sniper_task = asyncio.create_task(eia_sniper_loop())
    tracer_task = asyncio.create_task(wti_tracer_loop())
    pnl_task = asyncio.create_task(daily_pnl_loop())

    # Wait for tasks to complete
    await asyncio.gather(sniper_task, tracer_task, pnl_task)

if __name__ == "__main__":
    def handle_exception(loop, context):
        msg = context.get("exception", context["message"])
        logger.error(f"Caught exception: {msg}")
        
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(handle_exception)
    
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Shutting down bot via KeyboardInterrupt...")
        sys.exit(0)
