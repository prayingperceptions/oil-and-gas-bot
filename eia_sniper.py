import asyncio
import aiohttp
import os
import logging
import datetime
from dotenv import load_dotenv
from kalshi_client import KalshiClient
from db import log_trade, log_eia_report
from telegram_bot import send_telegram_alert

logger = logging.getLogger(__name__)
load_dotenv()

EIA_API_KEY = os.getenv("EIA_API_KEY")

# length=2 retrieves this week and last week to calculate the true draw/build
EIA_URL = f"https://api.eia.gov/v2/petroleum/sum/sndw/data/?api_key={EIA_API_KEY}&frequency=weekly&data[0]=value&sort[0][column]=period&sort[0][direction]=desc&length=2"

kalshi = KalshiClient()

async def fetch_eia_draw_build() -> float | None:
    if not EIA_API_KEY or EIA_API_KEY == "your_eia_api_key_here":
        logger.warning("EIA API Key not configured. Skipping execution.")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(EIA_URL, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    records = data.get("response", {}).get("data", [])
                    if len(records) >= 2:
                        current_inv = float(records[0]["value"])
                        previous_inv = float(records[1]["value"])
                        
                        # EIA values are often in Thousands of Barrels (Mbbl)
                        # We convert to Millions of Barrels (MMbbl) to match Kalshi markets
                        diff_mbbl = current_inv - previous_inv
                        diff_mmbbl = diff_mbbl / 1000.0
                        return diff_mmbbl
                return None
    except Exception as e:
        logger.error(f"EIA API Exception: {e}")
        return None

async def execute_eia_arb(draw_build_mmbbl: float):
    # Fetch Kalshi EIA Weekly markets
    market_response = await kalshi.get_active_markets("EIA")
    
    if not market_response or "markets" not in market_response:
        return
        
    for market in market_response["markets"]:
        ticker = market["ticker"]
        # Example Kalshi ticker: EIA-23MAR08-B2.5 (Built > 2.5m) or EIA-23MAR08-D1.0 (Drew > 1.0m)
        try:
            target_str = ticker.split("-")[-1]
            if target_str.startswith("B"):
                bound = float(target_str[1:])
                # If True Build > Kalshi Bound, it's a guaranteed YES
                if draw_build_mmbbl > bound:
                    logger.info(f"🚨 EIA SNIPER ARB: True Build {draw_build_mmbbl:.2f} > Bound {bound}. Sweeping {ticker} YES!")
                    await kalshi.create_order(ticker, "buy", "market", 99, 100) # Sweep the book up to 99c
                    await log_trade(ticker, "buy_yes", 100, 99, "eia_sniper")
                    await send_telegram_alert(f"<b>[EIA SNIPER ARB]</b> Swept YES on {ticker} for Build {draw_build_mmbbl:.2f}M 🛢️")
            elif target_str.startswith("D"):
                bound = float(target_str[1:])
                # If True Draw > Kalshi Bound (Draws are negative inventory change)
                if draw_build_mmbbl < -bound:
                    logger.info(f"🚨 EIA SNIPER ARB: True Draw {draw_build_mmbbl:.2f} > Bound {bound}. Sweeping {ticker} YES!")
                    await kalshi.create_order(ticker, "buy", "market", 99, 100) 
                    await log_trade(ticker, "buy_yes", 100, 99, "eia_sniper")
                    await send_telegram_alert(f"<b>[EIA SNIPER ARB]</b> Swept YES on {ticker} for Draw {draw_build_mmbbl:.2f}M 🛢️")
        except Exception as e:
            continue

async def eia_sniper_loop():
    logger.info("Starting EIA Sniper strategy...")
    
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # If Wednesday and close to 10:30 AM EST (14:30 EST/15:30 EDT)
        # Note: robust implementation requires pytz parsing to handle DST cleanly. Using rough 14-16 range here.
        if now.weekday() == 2 and now.hour in [14, 15] and 29 <= now.minute <= 35:
            logger.info("⚡ Within EIA release window, firing API...")
            diff = await fetch_eia_draw_build()
            if diff is not None:
                logger.info(f"Received Immediate EIA Delta: {diff:.2f} MMbbl")
                await log_eia_report(diff)
                await execute_eia_arb(diff)
                
                # Sleep deeply after executing to prevent duplicate spam this week
                await asyncio.sleep(86400) 
            else:
                await asyncio.sleep(0.5) # Try again in 500ms
        else:
            await asyncio.sleep(60) # Sleep longer outside the window
