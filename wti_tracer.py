import asyncio
import aiohttp
import logging
import math
import time
from kalshi_client import KalshiClient
from risk_engine import RiskEngine
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


# ── Multi-Source Price Feed ──────────────────────────────────────────────

class PriceFeed:
    """
    Fetches WTI Crude (CL=F) and Natural Gas (NG=F) prices from multiple
    Yahoo Finance endpoints with automatic fallback and caching:
      1. Yahoo Finance Spark API (lightweight, less rate-limited)
      2. Yahoo Finance v8 Chart API (full OHLCV chart data)
    Caches results for `cache_ttl` seconds to avoid rate limits (429).
    """

    SPARK_URL = "https://query1.finance.yahoo.com/v8/finance/spark?symbols={symbol}&range=1mo&interval=1d"
    CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    }

    def __init__(self, cache_ttl: int = 300):
        self.tickers = {"CL=F": "CL=F", "NG=F": "NG=F"}
        self._cache: dict[str, dict] = {}
        self._cache_ts: dict[str, float] = {}
        self._cache_ttl = cache_ttl  # 5 min cache to avoid 429s

    async def get_market_parameters(self) -> dict:
        """Fetch spot + compute historical mu and sigma for the OU model."""
        params = {}
        for label, symbol in self.tickers.items():
            # Check cache first
            now = time.time()
            if label in self._cache and (now - self._cache_ts.get(label, 0)) < self._cache_ttl:
                params[label] = self._cache[label]
                logger.info(f"[PriceFeed] {label} via cache (spot=${self._cache[label]['spot']:.2f})")
                continue

            result = await self._fetch_with_fallback(symbol)
            if result:
                self._cache[label] = result
                self._cache_ts[label] = now
                params[label] = result
            elif label in self._cache:
                # Use stale cache if all sources fail
                params[label] = self._cache[label]
                logger.warning(f"[PriceFeed] Using stale cache for {label}")
        return params

    async def _fetch_with_fallback(self, symbol: str) -> dict | None:
        """Try each source in order until one works."""

        # ── Source 1: Yahoo Finance Spark API (lightweight) ──────────
        try:
            data = await self._fetch_spark(symbol)
            if data:
                logger.info(f"[PriceFeed] {symbol} via Spark API ✅ (spot=${data['spot']:.2f})")
                return data
        except Exception as e:
            logger.warning(f"[PriceFeed] Spark API failed for {symbol}: {e}")

        # Small delay between sources to be polite
        await asyncio.sleep(2)

        # ── Source 2: Yahoo Finance v8 Chart API ─────────────────────
        try:
            data = await self._fetch_chart(symbol)
            if data:
                logger.info(f"[PriceFeed] {symbol} via Chart API ✅ (spot=${data['spot']:.2f})")
                return data
        except Exception as e:
            logger.warning(f"[PriceFeed] Chart API failed for {symbol}: {e}")

        logger.error(f"[PriceFeed] ALL sources failed for {symbol}")
        return None

    # ── Yahoo Finance Spark API ──────────────────────────────────────

    async def _fetch_spark(self, symbol: str) -> dict | None:
        url = self.SPARK_URL.format(symbol=symbol)
        async with aiohttp.ClientSession(headers=self.HEADERS) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    logger.warning(f"[PriceFeed] Spark API rate-limited (429) for {symbol}")
                    return None
                if resp.status != 200:
                    logger.warning(f"[PriceFeed] Spark API HTTP {resp.status} for {symbol}")
                    return None
                data = await resp.json(content_type=None)

        symbol_data = data.get(symbol, {})
        closes = symbol_data.get("close", [])

        if not closes:
            # Try the nested format
            closes = []
            for item in symbol_data.get("indicators", {}).get("quote", [{}]):
                closes = item.get("close", [])

        closes = [c for c in closes if c is not None]
        if len(closes) < 2:
            return None

        return self._compute_ou_params(closes)

    # ── Yahoo Finance v8 Chart API ───────────────────────────────────

    async def _fetch_chart(self, symbol: str) -> dict | None:
        url = self.CHART_URL.format(symbol=symbol)
        async with aiohttp.ClientSession(headers=self.HEADERS) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    logger.warning(f"[PriceFeed] Chart API rate-limited (429) for {symbol}")
                    return None
                if resp.status != 200:
                    logger.warning(f"[PriceFeed] Chart API HTTP {resp.status} for {symbol}")
                    return None
                data = await resp.json(content_type=None)

        chart = data.get("chart", {}).get("result", [])
        if not chart:
            return None

        result = chart[0]
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        closes = [c for c in closes if c is not None]

        if len(closes) < 2:
            return None

        return self._compute_ou_params(closes)

    # ── Shared OU parameter computation ──────────────────────────────

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
            sigma = spot * 0.3  # Fallback 30% annualized vol

        return {"spot": spot, "mu": mu, "sigma": sigma}


# ── WTI Tracer Strategy Loop ────────────────────────────────────────────

# Scan interval: 5 minutes (avoids Yahoo rate limits while still catching moves)
SCAN_INTERVAL = 300

async def wti_tracer_loop():
    logger.info("Starting WTI/NG Price Tracer Strategy (multi-source feed)...")
    feed = PriceFeed(cache_ttl=SCAN_INTERVAL)
    kalshi = KalshiClient()
    risk = RiskEngine()
    
    if not kalshi.private_key:
        logger.error("Kalshi Client missing RSA Key. WTI Tracer running in observation only.")
        
    # Standard theta parameter for Crude/NatGas (Mean Reversion Speed)
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
