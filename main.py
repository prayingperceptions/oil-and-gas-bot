import asyncio
import logging
import signal
import sys
from eia_sniper import eia_sniper_loop
from wti_tracer import wti_tracer_loop
from db import init_db
from telegram_bot import send_telegram_alert

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

async def main():
    logger.info("Starting Kalshi Energy Arb Bot...")
    await send_telegram_alert("🚀 Kalshi Energy Arb Bot is starting...")
    
    # Initialize SQLite database
    await init_db()

    # Create tasks for the continuous strategy loops
    sniper_task = asyncio.create_task(eia_sniper_loop())
    tracer_task = asyncio.create_task(wti_tracer_loop())

    # Wait for tasks to complete
    await asyncio.gather(sniper_task, tracer_task)

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
