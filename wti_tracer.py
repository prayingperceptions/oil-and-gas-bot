import asyncio
import yfinance as yf
import logging

logger = logging.getLogger(__name__)

class PriceFeed:
    def __init__(self):
        self.tickers = ["CL=F", "NG=F"] # WTI Crude Oil, Natural Gas Futures
    
    async def get_current_prices(self) -> dict:
        # yfinance is synchronous, so we run it in an executor
        prices = {}
        try:
            for ticker in self.tickers:
                def fetch(t: str):
                    data = yf.Ticker(t)
                    hist = data.history(period="1d", interval="1m")
                    if not hist.empty:
                        return float(hist['Close'].iloc[-1])
                    return None
                
                price = await asyncio.to_thread(fetch, ticker)
                if price is not None:
                    prices[ticker] = price
            return prices
        except Exception as e:
            logger.error(f"Error fetching prices from yfinance: {e}")
            return {}

async def wti_tracer_loop():
    logger.info("Starting WTI/NG Price Tracer...")
    feed = PriceFeed()
    while True:
        prices = await feed.get_current_prices()
        if prices:
            wti = prices.get("CL=F", "N/A")
            ng = prices.get("NG=F", "N/A")
            logger.info(f"Spot prices - WTI: {wti}, NG: {ng}")
            
            # TODO: Add Ornstein-Uhlenbeck evaluation & Kalshi trade dispatch
        
        await asyncio.sleep(60) # Poll every 60 seconds
