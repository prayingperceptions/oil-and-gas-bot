import asyncio
import yfinance as yf
import logging
import math
import time
from kalshi_client import KalshiClient
from db import log_trade
from telegram_bot import send_telegram_alert

logger = logging.getLogger(__name__)

def norm_cdf(x):
    """Gaussian CDF approximation without scipy dependency."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def ou_probability_above_strike(S0, K, mu, theta, sigma, t):
    """
    Computes P(S_t > K) under an Ornstein-Uhlenbeck process.
    t is time in days (e.g., 0.5 for 12 hours).
    """
    if t <= 0:
        return 1.0 if S0 > K else 0.0

    E_S = S0 * math.exp(-theta * t) + mu * (1 - math.exp(-theta * t))
    Var_S = (sigma**2 / (2 * theta)) * (1 - math.exp(-2 * theta * t))
    
    if Var_S <= 0:
        return 1.0 if E_S > K else 0.0
        
    std_S = math.sqrt(Var_S)
    d = (K - E_S) / std_S
    return 1.0 - norm_cdf(d)

class PriceFeed:
    def __init__(self):
        self.tickers = ["CL=F", "NG=F"]
        
    async def get_market_parameters(self) -> dict:
        """Fetch spot and compute historical mu and sigma for the OU model."""
        params = {}
        try:
            for ticker in self.tickers:
                def fetch(t: str) -> dict | None:
                    data = yf.Ticker(t)
                    # Get 30 days of data to compute long-term mean and historical volatility
                    hist = data.history(period="1mo", interval="1d")
                    if not hist.empty:
                        closes = hist['Close'].tolist()
                        spot = closes[-1]
                        mu = sum(closes) / len(closes)
                        
                        # Daily returns for sigma
                        returns = [(closes[i] - closes[i-1])/closes[i-1] for i in range(1, len(closes))]
                        if len(returns) > 0:
                            avg_ret = sum(returns) / len(returns)
                            variance = sum((r - avg_ret)**2 for r in returns) / len(returns)
                            sigma = math.sqrt(variance) * spot * math.sqrt(252) # Annualized rough absolute vol
                        else:
                            sigma = spot * 0.3 # Fallback 30% annualized vol
                            
                        return {"spot": spot, "mu": mu, "sigma": sigma}
                    return None
                
                res = await asyncio.to_thread(fetch, ticker)
                if res is not None:
                    params[ticker] = res
            return params
        except Exception as e:
            logger.error(f"yfinance fetch error: {e}")
            return {}

async def wti_tracer_loop():
    logger.info("Starting WTI/NG Price Tracer Strategy...")
    feed = PriceFeed()
    kalshi = KalshiClient()
    
    if not kalshi.private_key:
        logger.error("Kalshi Client missing RSA Key. WTI Tracer running in observation only.")
        
    # Standard theta parameter for Crude/NatGas (Mean Reversion Speed)
    # theta=5.0 implies mean reversion half-life of ~0.14 years (approx 1.5 months)
    THETA = 5.0 
    
    while True:
        try:
            params = await feed.get_market_parameters()
            wti_params = params.get("CL=F")
            
            if wti_params:
                spot = wti_params["spot"]
                mu = wti_params["mu"]
                sigma = wti_params["sigma"]
                
                logger.info(f"[WTI Tracer] Spot: ${spot:.2f} | 30d Mean: ${mu:.2f} | Vol: {sigma:.2f}")
                
                # Fetch Kalshi WTI Daily markets
                market_response = await kalshi.get_active_markets("WTID")
                
                if market_response and "markets" in market_response:
                    for market in market_response["markets"]:
                        ticker = market["ticker"]
                        subtitle = market.get("subtitle", "") # e.g. "WTI closes above $80.00"
                        
                        # Parse strike from ticker WTID-24NOV06-T82.00
                        try:
                            # Typically Kalshi formats targets as T[Price]
                            strike_str = ticker.split("-T")[-1]
                            K = float(strike_str)
                            
                            yes_ask = market.get("yes_ask", 100)
                            no_ask = market.get("no_ask", 100)
                            
                            # Extremely rough days to expiry calculation (assume expires at 4PM EST today)
                            # A real impl would parse market["close_time"]
                            t_days = 0.5 # 12 hours
                            
                            fair_prob = ou_probability_above_strike(spot, K, mu, THETA, sigma, t_days)
                            fair_cents = fair_prob * 100
                            
                            # Execution threshold triggers (Edge >> 15 cents)
                            if fair_cents > (yes_ask + 15):
                                logger.info(f"🚨 TRACER ARB FOUND: Buy YES on {ticker} at {yes_ask}c (Fair: {fair_cents:.1f}c)")
                                await kalshi.create_order(ticker, "buy", "market", yes_ask, 1)
                                await log_trade(ticker, "buy_yes", 1, yes_ask, "wti_ou_tracer")
                                await send_telegram_alert(f"<b>[WTI TRACER ARB]</b> Bought 1 YES on {ticker} @ {yes_ask}c 🛢️")
                                
                            elif (100 - fair_cents) > (no_ask + 15):
                                logger.info(f"🚨 TRACER ARB FOUND: Buy NO on {ticker} at {no_ask}c (Fair NO: {(100-fair_cents):.1f}c)")
                                await kalshi.create_order(ticker, "sell", "market", 100-no_ask, 1) # Buying NO means selling YES
                                await log_trade(ticker, "buy_no", 1, no_ask, "wti_ou_tracer")
                                await send_telegram_alert(f"<b>[WTI TRACER ARB]</b> Bought 1 NO on {ticker} @ {no_ask}c 🛢️")
                                
                        except ValueError:
                            continue # Could not parse strike
        except Exception as e:
            logger.error(f"Error in WTI Tracer Loop: {e}")
            
        await asyncio.sleep(60) # Evaluate every minute
