import asyncio
import aiohttp
import os
import logging
import math
import time
from dotenv import load_dotenv
from kalshi_client import KalshiClient
from risk_engine import RiskEngine
from db import log_trade
from telegram_bot import send_telegram_alert

load_dotenv()
logger = logging.getLogger(__name__)

EIA_API_KEY = os.getenv("EIA_API_KEY")

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


# ── EIA Government Price Feed (no rate limits!) ─────────────────────────

class PriceFeed:
    """
    Fetches WTI Crude and Natural Gas spot prices from the US Energy
    Information Administration (EIA) API — free, reliable, no rate limits.
    Uses the same EIA_API_KEY already configured for the EIA Sniper.
    Caches results for `cache_ttl` seconds.
    """

    # WTI Crude Oil Spot Price (daily)
    WTI_URL = (
        "https://api.eia.gov/v2/petroleum/pri/spt/data/"
        "?api_key={key}"
        "&frequency=daily"
        "&data[0]=value"
        "&facets[product][]=EPCWTI"
        "&sort[0][column]=period"
        "&sort[0][direction]=desc"
        "&length=30"
    )

    # Henry Hub Natural Gas Spot Price (daily)
    NG_URL = (
        "https://api.eia.gov/v2/natural-gas/pri/fut/data/"
        "?api_key={key}"
        "&frequency=daily"
        "&data[0]=value"
        "&sort[0][column]=period"
        "&sort[0][direction]=desc"
        "&length=30"
    )

    def __init__(self, cache_ttl: int = 300):
        self._cache: dict[str, dict] = {}
        self._cache_ts: dict[str, float] = {}
        self._cache_ttl = cache_ttl

    async def get_market_parameters(self) -> dict:
        """Fetch spot + compute OU model params for WTI and NG."""
        params = {}

        if not EIA_API_KEY:
            logger.error("[PriceFeed] EIA_API_KEY not configured")
            return params

        for label, url_template in [("CL=F", self.WTI_URL), ("NG=F", self.NG_URL)]:
            now = time.time()
            if label in self._cache and (now - self._cache_ts.get(label, 0)) < self._cache_ttl:
                params[label] = self._cache[label]
                logger.info(f"[PriceFeed] {label} via cache (spot=${self._cache[label]['spot']:.2f})")
                continue

            result = await self._fetch_eia(label, url_template)
            if result:
                self._cache[label] = result
                self._cache_ts[label] = now
                params[label] = result
            elif label in self._cache:
                params[label] = self._cache[label]
                logger.warning(f"[PriceFeed] Using stale cache for {label}")

        return params

    async def _fetch_eia(self, label: str, url_template: str) -> dict | None:
        url = url_template.format(key=EIA_API_KEY)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.error(f"[PriceFeed] EIA API HTTP {resp.status} for {label}")
                        return None
                    data = await resp.json(content_type=None)

            records = data.get("response", {}).get("data", [])
            if len(records) < 2:
                logger.warning(f"[PriceFeed] EIA returned only {len(records)} records for {label}")
                return None

            # Extract closing prices (most recent first)
            closes = []
            for rec in records:
                try:
                    closes.append(float(rec["value"]))
                except (ValueError, TypeError, KeyError):
                    continue

            if len(closes) < 2:
                return None

            # Reverse so oldest is first (matches OU model expectation)
            closes.reverse()

            result = self._compute_ou_params(closes)
            logger.info(f"[PriceFeed] {label} via EIA API ✅ (spot=${result['spot']:.2f}, {len(closes)} days)")
            return result

        except Exception as e:
            logger.error(f"[PriceFeed] EIA API error for {label}: {e}")
            return None

    @staticmethod
    def _compute_ou_params(closes: list[float]) -> dict:
        spot = closes[-1]
        mu = sum(closes) / len(closes)

        returns = [(closes[i] - closes[i-1]) / closes[i-1]
                    for i in range(1, len(closes))]

        if returns:
            avg_ret = sum(returns) / len(returns)
            variance = sum((r - avg_ret) ** 2 for r in returns) / len(returns)
            sigma = math.sqrt(variance) * spot * math.sqrt(252)
        else:
            sigma = spot * 0.3

        return {"spot": spot, "mu": mu, "sigma": sigma}


# ── WTI Tracer Strategy Loop ────────────────────────────────────────────

SCAN_INTERVAL = 300  # 5 minutes

async def wti_tracer_loop():
    logger.info("Starting WTI/NG Price Tracer Strategy (EIA feed)...")
    feed = PriceFeed(cache_ttl=SCAN_INTERVAL)
    kalshi = KalshiClient()
    risk = RiskEngine()
    
    if not kalshi.private_key:
        logger.error("Kalshi Client missing RSA Key. WTI Tracer running in observation only.")
        
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
                
                market_response = await kalshi.get_active_markets("WTID")
                
                if market_response and "markets" in market_response:
                    for market in market_response["markets"]:
                        ticker = market["ticker"]
                        
                        try:
                            strike_str = ticker.split("-T")[-1]
                            K = float(strike_str)
                            
                            yes_ask = market.get("yes_ask", 100)
                            no_ask = market.get("no_ask", 100)
                            t_days = 0.5
                            
                            fair_prob = ou_probability_above_strike(spot, K, mu, THETA, sigma, t_days)
                            fair_cents = fair_prob * 100
                            
                            # ── Buy YES edge (8¢ threshold) ─────────
                            if fair_cents > (yes_ask + 8):
                                sizing = await risk.get_position_size("wti_tracer", fair_prob, yes_ask)
                                
                                if sizing["contracts"] > 0:
                                    count = sizing["contracts"]
                                    logger.info(
                                        f"🚨 TRACER ARB: Buy YES {ticker} | "
                                        f"{count} @ {yes_ask}¢ (Fair: {fair_cents:.1f}¢) | "
                                        f"Tier: {sizing['tier']} | Risk: ${sizing['risk_usd']:.2f}"
                                    )
                                    await kalshi.create_order(ticker, "buy", "market", yes_ask, count)
                                    await log_trade(ticker, "buy_yes", count, yes_ask, "wti_ou_tracer")
                                    await send_telegram_alert(
                                        f"<b>[WTI TRACER ARB]</b> {ticker}\n"
                                        f"📈 Buy YES: {count} @ {yes_ask}¢ (Fair: {fair_cents:.1f}¢)\n"
                                        f"💰 Risk: ${sizing['risk_usd']:.2f} | Edge: {sizing['edge']:.2%}\n"
                                        f"📊 Tier: {sizing['tier']} | Bal: ${sizing['balance']:.2f} 🛢️"
                                    )
                                    risk.invalidate_cache()

                            # ── Buy NO edge (8¢ threshold) ──────────
                            elif (100 - fair_cents) > (no_ask + 8):
                                fair_no_prob = 1.0 - fair_prob
                                sizing = await risk.get_position_size("wti_tracer", fair_no_prob, no_ask)
                                
                                if sizing["contracts"] > 0:
                                    count = sizing["contracts"]
                                    logger.info(
                                        f"🚨 TRACER ARB: Buy NO {ticker} | "
                                        f"{count} @ {no_ask}¢ (Fair NO: {(100-fair_cents):.1f}¢) | "
                                        f"Tier: {sizing['tier']} | Risk: ${sizing['risk_usd']:.2f}"
                                    )
                                    await kalshi.create_order(ticker, "sell", "market", 100 - no_ask, count)
                                    await log_trade(ticker, "buy_no", count, no_ask, "wti_ou_tracer")
                                    await send_telegram_alert(
                                        f"<b>[WTI TRACER ARB]</b> {ticker}\n"
                                        f"📉 Buy NO: {count} @ {no_ask}¢ (Fair NO: {(100-fair_cents):.1f}¢)\n"
                                        f"💰 Risk: ${sizing['risk_usd']:.2f} | Edge: {sizing['edge']:.2%}\n"
                                        f"📊 Tier: {sizing['tier']} | Bal: ${sizing['balance']:.2f} 🛢️"
                                    )
                                    risk.invalidate_cache()
                                
                        except ValueError:
                            continue
        except Exception as e:
            logger.error(f"Error in WTI Tracer Loop (will retry): {e}")
            
        await asyncio.sleep(SCAN_INTERVAL)
