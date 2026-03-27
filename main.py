import asyncio
import logging
import signal
import sys
import os
import datetime
from aiohttp import web
from eia_sniper import eia_sniper_loop
from wti_tracer import wti_tracer_loop
from db import init_db
from telegram_bot import send_telegram_alert, heartbeat_loop
from kalshi_client import KalshiClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# ── Global state for health checks ───────────────────────────────────────
TASK_STATUS: dict[str, str] = {}   # task_name -> "running" | "restarting" | "dead"
BOT_START_TIME = datetime.datetime.now(datetime.timezone.utc)


# ── Health-check web server ──────────────────────────────────────────────
async def health_handler(request):
    uptime = datetime.datetime.now(datetime.timezone.utc) - BOT_START_TIME
    return web.json_response({
        "status": "ok",
        "uptime_seconds": int(uptime.total_seconds()),
        "tasks": TASK_STATUS,
    })


async def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health-check server listening on port {port}")


# ── Supervised task runner ───────────────────────────────────────────────
async def supervised_task(name: str, coro_fn, *args, max_retries: int = 0):
    """
    Runs `coro_fn(*args)` in an infinite retry loop.
    max_retries=0 means unlimited retries (truly 24/7).
    Sends a Telegram alert on every crash + restart.
    """
    retries = 0
    while True:
        try:
            TASK_STATUS[name] = "running"
            logger.info(f"[Supervisor] Starting task '{name}'")
            await coro_fn(*args)
            # If the coroutine returns normally, it finished its work — break out
            break
        except asyncio.CancelledError:
            TASK_STATUS[name] = "cancelled"
            logger.info(f"[Supervisor] Task '{name}' cancelled.")
            break
        except Exception as e:
            retries += 1
            TASK_STATUS[name] = "restarting"
            logger.error(f"[Supervisor] Task '{name}' crashed (attempt #{retries}): {e}")
            await send_telegram_alert(
                f"⚠️ <b>Task Crash</b>\n\n"
                f"Task: <code>{name}</code>\n"
                f"Error: <code>{e}</code>\n"
                f"Restarting in 10 s … (attempt #{retries})"
            )
            if 0 < max_retries <= retries:
                TASK_STATUS[name] = "dead"
                logger.error(f"[Supervisor] Task '{name}' exceeded {max_retries} retries. Giving up.")
                await send_telegram_alert(f"🛑 <b>Task '{name}' is DEAD</b> after {max_retries} retries.")
                break
            await asyncio.sleep(10)


# ── Daily PnL ────────────────────────────────────────────────────────────
async def daily_pnl_loop():
    logger.info("Starting Daily PnL tracking loop...")
    kalshi = KalshiClient()
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        # Approximate 5 PM EST (21:00 or 22:00 UTC)
        if now.hour in [21, 22] and now.minute == 0:
            try:
                if kalshi.private_key:
                    balance_data = await kalshi.get_balance()
                    balance_cents = balance_data.get("balance", 0)
                    available = balance_cents / 100.0
                    await send_telegram_alert(
                        f"📊 <b>Daily Wrap-Up</b>\n\n💰 Live Kalshi Balance: <b>${available:.2f}</b>"
                    )
            except Exception as e:
                logger.error(f"PnL loop error: {e}")
            await asyncio.sleep(61)  # Prevent double-fire
        else:
            await asyncio.sleep(30)


# ── Main ─────────────────────────────────────────────────────────────────
async def main():
    logger.info("🚀 Starting Kalshi Energy Arb Bot (24/7 Supervised Mode)...")
    await send_telegram_alert("🚀 Kalshi Energy Arb Bot is starting (24/7 mode)...")

    # Initialize database
    await init_db()

    # Start health-check server (needed by Railway / render / fly.io)
    await start_health_server()

    # Launch all strategies under supervision
    tasks = [
        asyncio.create_task(supervised_task("eia_sniper", eia_sniper_loop)),
        asyncio.create_task(supervised_task("wti_tracer", wti_tracer_loop)),
        asyncio.create_task(supervised_task("daily_pnl", daily_pnl_loop)),
        asyncio.create_task(supervised_task("heartbeat", heartbeat_loop)),
    ]

    # Graceful shutdown on SIGTERM / SIGINT
    stop = asyncio.Event()

    def _signal_handler():
        logger.info("Received shutdown signal, stopping tasks...")
        stop.set()
        for t in tasks:
            t.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    await stop.wait()
    await asyncio.gather(*tasks, return_exceptions=True)
    await send_telegram_alert("🔴 Kalshi Energy Arb Bot has shut down.")
    logger.info("Bot shut down cleanly.")


if __name__ == "__main__":
    def handle_exception(loop, context):
        msg = context.get("exception", context["message"])
        logger.error(f"Caught unhandled exception: {msg}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(handle_exception)

    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Shutting down bot via KeyboardInterrupt...")
        sys.exit(0)
