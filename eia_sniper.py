import asyncio
import aiohttp
import os
import logging
import datetime
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

EIA_API_KEY = os.getenv("EIA_API_KEY")
EIA_URL = f"https://api.eia.gov/v2/petroleum/sum/sndw/data/?api_key={EIA_API_KEY}&frequency=weekly&data[0]=value&sort[0][column]=period&sort[0][direction]=desc&length=1"

async def fetch_eia_data():
    if not EIA_API_KEY or EIA_API_KEY == "your_eia_api_key_here":
        logger.warning("EIA API Key not configured.")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(EIA_URL, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                else:
                    logger.error(f"EIA API HTTP Error: {response.status}")
                    return None
    except Exception as e:
        logger.error(f"EIA API Exception: {e}")
        return None

async def eia_sniper_loop():
    logger.info("Starting EIA Sniper loop...")
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # If Wednesday and close to 10:30 AM EST (around 15:30 UTC depending on daylight saving)
        if now.weekday() == 2 and now.hour == 15 and 29 <= now.minute <= 35:
            logger.info("Within EIA release window, polling...")
            data = await fetch_eia_data()
            if data:
                logger.info(f"Received EIA Data: {data}")
                # TODO: Parse draw/build and trigger Kalshi sweep
            
            await asyncio.sleep(0.5) # Spam EIA endpoint within the window
        else:
            await asyncio.sleep(60) # Sleep longer outside the window
