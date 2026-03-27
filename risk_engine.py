import logging
import time
import math
from kalshi_client import KalshiClient

logger = logging.getLogger(__name__)

# ── Capital Tiers ────────────────────────────────────────────────────────
# Each tier defines:
#   max_single_risk  – max fraction of balance to risk on one trade
#   max_exposure     – max fraction of balance in open risk across all trades
#   kelly_fraction   – multiplier on raw Kelly % (1.0 = full Kelly)
#   min_balance      – lower bound for this tier (inclusive)

TIERS = [
    {
        "name": "Whale",
        "min_balance": 50_000.00,
        "max_single_risk": 0.05,
        "max_exposure": 0.25,
        "kelly_fraction": 0.25,
    },
    {
        "name": "Scale",
        "min_balance": 10_000.00,
        "max_single_risk": 0.10,
        "max_exposure": 0.35,
        "kelly_fraction": 0.50,
    },
    {
        "name": "Growth",
        "min_balance": 1_000.00,
        "max_single_risk": 0.15,
        "max_exposure": 0.40,
        "kelly_fraction": 1.00,
    },
    {
        "name": "Starter",
        "min_balance": 100.00,
        "max_single_risk": 0.20,
        "max_exposure": 0.50,
        "kelly_fraction": 1.00,
    },
    {
        "name": "Micro",
        "min_balance": 0.00,
        "max_single_risk": 0.30,
        "max_exposure": 0.60,
        "kelly_fraction": 1.00,
    },
]

# Per-strategy risk budget share (must sum to 1.0)
STRATEGY_BUDGET = {
    "eia_sniper": 0.70,
    "wti_tracer": 0.30,
}

# ── Helpers ──────────────────────────────────────────────────────────────

def _get_tier(balance: float) -> dict:
    """Return the capital tier for the given balance (tiers sorted high→low)."""
    for tier in TIERS:
        if balance >= tier["min_balance"]:
            return tier
    return TIERS[-1]  # fallback to Micro


def kelly_fraction(win_prob: float, market_price_cents: int) -> float:
    """
    Compute Kelly % for a binary Kalshi contract.

    Parameters
    ----------
    win_prob : float
        Estimated probability the contract resolves YES (0–1).
    market_price_cents : int
        Price you'd pay for the contract in cents (1–99).

    Returns
    -------
    float
        Optimal fraction of bankroll to wager (can be negative → no bet).
    """
    if market_price_cents <= 0 or market_price_cents >= 100:
        return 0.0

    cost = market_price_cents / 100.0          # what you pay
    payout = 1.0                                # Kalshi contracts pay $1
    profit = payout - cost                      # net profit if win
    b = profit / cost                           # odds ratio

    p = win_prob
    q = 1.0 - p

    if b <= 0:
        return 0.0

    kelly = (p * b - q) / b
    return max(kelly, 0.0)  # never go negative


# ── Risk Engine ──────────────────────────────────────────────────────────

class RiskEngine:
    """
    Queries Kalshi balance and computes position sizes using Kelly Criterion
    scaled by capital-tier limits.  Caches balance for `cache_ttl` seconds.
    """

    def __init__(self, cache_ttl: int = 60):
        self.kalshi = KalshiClient()
        self._cached_balance: float | None = None
        self._cache_ts: float = 0.0
        self._cache_ttl = cache_ttl

    # ── Balance ──────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Return available balance in dollars, with caching."""
        now = time.time()
        if self._cached_balance is not None and (now - self._cache_ts) < self._cache_ttl:
            return self._cached_balance

        try:
            data = await self.kalshi.get_balance()
            balance_cents = data.get("balance", 0)
            self._cached_balance = balance_cents / 100.0
            self._cache_ts = now
            logger.info(f"[RiskEngine] Fetched balance: ${self._cached_balance:.2f}")
        except Exception as e:
            logger.error(f"[RiskEngine] Balance fetch failed: {e}")
            if self._cached_balance is None:
                self._cached_balance = 0.0
        return self._cached_balance

    def invalidate_cache(self):
        """Force a fresh balance fetch on the next call."""
        self._cached_balance = None

    # ── Position Sizing ──────────────────────────────────────────────

    async def get_position_size(
        self,
        strategy: str,
        fair_prob: float,
        market_price_cents: int,
    ) -> dict:
        """
        Compute the optimal number of contracts and limit price.

        Parameters
        ----------
        strategy : str
            "eia_sniper" or "wti_tracer"
        fair_prob : float
            Model-estimated probability the contract resolves YES (0–1).
        market_price_cents : int
            Best ask price in cents you'd pay.

        Returns
        -------
        dict with keys:
            contracts : int   – number of contracts to buy (0 = skip)
            price     : int   – price in cents to submit
            tier      : str   – capital tier name
            balance   : float – current balance in dollars
            risk_usd  : float – total dollar risk of this order
            edge      : float – estimated edge (fair_prob - implied_prob)
            kelly_raw : float – raw Kelly fraction before tier scaling
        """
        balance = await self.get_balance()
        tier = _get_tier(balance)

        # Minimum viable balance
        if balance < 1.00:
            logger.warning(f"[RiskEngine] Balance ${balance:.2f} too low to trade.")
            return self._skip(tier, balance)

        # Compute Kelly
        raw_kelly = kelly_fraction(fair_prob, market_price_cents)
        if raw_kelly <= 0:
            logger.info(f"[RiskEngine] No edge (kelly={raw_kelly:.4f}). Skipping.")
            return self._skip(tier, balance, raw_kelly=raw_kelly)

        # Scale Kelly by tier fraction
        scaled_kelly = raw_kelly * tier["kelly_fraction"]

        # Strategy budget share
        budget_share = STRATEGY_BUDGET.get(strategy, 0.30)

        # Max dollars to risk on this trade
        max_risk_dollars = balance * tier["max_single_risk"] * budget_share

        # Kelly-optimal dollars
        kelly_dollars = balance * scaled_kelly * budget_share

        # Take the lesser of Kelly-optimal and tier cap
        risk_dollars = min(kelly_dollars, max_risk_dollars)

        # Convert to contracts: each contract costs market_price_cents
        cost_per_contract = market_price_cents / 100.0
        if cost_per_contract <= 0:
            return self._skip(tier, balance, raw_kelly=raw_kelly)

        contracts = int(risk_dollars / cost_per_contract)
        contracts = max(contracts, 1)  # always at least 1 if we have edge

        # Final risk check: don't exceed max_single_risk
        actual_risk = contracts * cost_per_contract
        if actual_risk > balance * tier["max_single_risk"]:
            contracts = int((balance * tier["max_single_risk"]) / cost_per_contract)
            contracts = max(contracts, 1)
            actual_risk = contracts * cost_per_contract

        # Don't spend more than we have
        if actual_risk > balance * 0.95:  # leave 5% buffer
            contracts = int((balance * 0.95) / cost_per_contract)
            contracts = max(contracts, 1) if contracts >= 1 else 0
            actual_risk = contracts * cost_per_contract

        edge = fair_prob - (market_price_cents / 100.0)

        result = {
            "contracts": contracts,
            "price": market_price_cents,
            "tier": tier["name"],
            "balance": round(balance, 2),
            "risk_usd": round(actual_risk, 2),
            "edge": round(edge, 4),
            "kelly_raw": round(raw_kelly, 4),
        }
        logger.info(
            f"[RiskEngine] {strategy} | Tier: {tier['name']} | "
            f"Balance: ${balance:.2f} | Edge: {edge:.2%} | "
            f"Kelly: {raw_kelly:.2%} → {scaled_kelly:.2%} | "
            f"Contracts: {contracts} @ {market_price_cents}¢ = ${actual_risk:.2f} risk"
        )
        return result

    @staticmethod
    def _skip(tier: dict, balance: float, raw_kelly: float = 0.0) -> dict:
        return {
            "contracts": 0,
            "price": 0,
            "tier": tier["name"],
            "balance": round(balance, 2),
            "risk_usd": 0.0,
            "edge": 0.0,
            "kelly_raw": round(raw_kelly, 4),
        }
