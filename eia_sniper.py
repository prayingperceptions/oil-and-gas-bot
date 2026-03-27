import asyncio
import aiohttp
import os
import logging
import datetime
from dotenv import load_dotenv
from kalshi_client import KalshiClient
from risk_engine import RiskEngine
from db import log_trade, log_eia_report
from telegram_bot import send_telegram_alert

logger = logging.getLogger(__name__)
load_dotenv()

EIA_API_KEY = os.getenv("EIA_API_KEY")

# length=2 retrieves this week and last week to calculate the true draw/build
EIA_URL = f"https://api.eia.gov/v2/petroleum/sum/sndw/data/?api_key={EIA_API_KEY}&frequency=weekly&data[0]=value&sort[0][column]=period&sort[0][direction]=desc&length=2"

kalshi = KalshiClient()
risk = RiskEngine()

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

    # Invalidate balance cache before a sweep to get fresh numbers
    risk.invalidate_cache()

    for market in market_response["markets"]:
        ticker = market["ticker"]
        # Example Kalshi ticker: EIA-23MAR08-B2.5 (Built > 2.5m) or EIA-23MAR08-D1.0 (Drew > 1.0m)
        try:
            target_str = ticker.split("-")[-1]
            is_arb = False
            if target_str.startswith("B"):
                bound = float(target_str[1:])
                if draw_build_mmbbl > bound:
                    is_arb = True
            elif target_str.startswith("D"):
                bound = float(target_str[1:])
                if draw_build_mmbbl < -bound:
                    is_arb = True

            if is_arb:
                # EIA arbs are near-certain — use fair_prob ≈ 0.98
                yes_ask = market.get("yes_ask", 99)
                sizing = await risk.get_position_size("eia_sniper", 0.98, yes_ask)

                if sizing["contracts"] > 0:
                    count = sizing["contracts"]
                    price = sizing["price"]
                    logger.info(
                        f"🚨 EIA SNIPER ARB: {ticker} | "
                        f"{count} contracts @ {price}¢ | "
                        f"Tier: {sizing['tier']} | Risk: ${sizing['risk_usd']:.2f}"
                    )
                    await kalshi.create_order(ticker, "buy", "market", price, count)
                    await log_trade(ticker, "buy_yes", count, price, "eia_sniper")
                    await send_telegram_alert(
                        f"<b>[EIA SNIPER ARB]</b> {ticker}\n"
                        f"📦 {count} contracts @ {price}¢\n"
                        f"💰 Risk: ${sizing['risk_usd']:.2f} | Tier: {sizing['tier']}\n"
                        f"📊 Balance: ${sizing['balance']:.2f} 🛢️"
                    )
                    # Re-fetch balance for the next market in this sweep
                    risk.invalidate_cache()
                else:
                    logger.info(f"EIA SNIPER: No edge or insufficient balance for {ticker}")
        except Exception as e:
            continue

async def eia_sniper_loop():
    logger.info("Starting EIA Sniper strategy...")
    
    while True:
        try:
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
        except Exception as e:
            logger.error(f"EIA Sniper loop error (will retry): {e}")
            await asyncio.sleep(30)
