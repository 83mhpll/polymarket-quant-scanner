"""Fee model + depth-aware basket execution simulator.

Polymarket taker fee (per share):   fee(p) = feeRate * p * (1 - p)
Makers pay no fee. All arbitrage here is simulated as taker (crossing the book),
which is the conservative assumption.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Level

EPS = 1e-9


def taker_fee(price: float, rate: float) -> float:
    return rate * price * (1.0 - price)


@dataclass
class BasketFill:
    sets: float = 0.0                 # number of complete baskets
    capital: float = 0.0              # USDC deployed
    profit: float = 0.0               # guaranteed $ profit (payout - cost) for buy; revenue - mint for sell
    fees: float = 0.0
    avg_prices: list[float] = field(default_factory=list)
    worst_prices: list[float] = field(default_factory=list)
    first_edge: float = 0.0           # edge per set on the best level
    last_edge: float = 0.0            # edge per set on the marginal level

    @property
    def roi(self) -> float:
        return self.profit / self.capital if self.capital > EPS else 0.0


def basket_walk(
    ladders: list[list[Level]],
    fee_rates: list[float],
    *,
    side: str,
    set_value: float,
    min_edge: float = 0.0,
    max_capital: float | None = None,
    slippage: float = 0.0,
) -> BasketFill:
    """Walk N order books simultaneously, buying (or selling) one share of every leg per "set".

    side="buy":  each set costs sum(ask_i + fee_i) and pays `set_value` for sure.
    side="sell": each set needs `set_value` of collateral (minting) and returns sum(bid_i - fee_i).

    Continues while the *marginal* set edge >= min_edge * set_value. Returns aggregated fill.
    """
    n = len(ladders)
    out = BasketFill(avg_prices=[0.0] * n, worst_prices=[0.0] * n)
    if n == 0 or any(not l for l in ladders):
        return out
    idx = [0] * n
    rem = [ladders[i][0].size for i in range(n)]
    notional = [0.0] * n
    threshold = min_edge * set_value
    first = True
    while all(idx[i] < len(ladders[i]) for i in range(n)):
        prices = [ladders[i][idx[i]].price for i in range(n)]
        fees = [taker_fee(p, r) for p, r in zip(prices, fee_rates)]
        if side == "buy":
            unit = sum(prices) + sum(fees) + slippage * n
            edge = set_value - unit
            cap_per_set = unit
        else:
            unit = sum(prices) - sum(fees) - slippage * n
            edge = unit - set_value
            cap_per_set = set_value
        if first:
            out.first_edge = edge
            first = False
        if edge < threshold or edge <= 0:
            break
        q = min(rem)
        if max_capital is not None:
            q = min(q, (max_capital - out.capital) / cap_per_set)
        if q <= EPS:
            break
        out.sets += q
        out.capital += q * cap_per_set
        out.profit += q * edge
        out.fees += q * sum(fees)
        out.last_edge = edge
        for i in range(n):
            notional[i] += q * prices[i]
            out.worst_prices[i] = prices[i]
            rem[i] -= q
            if rem[i] <= EPS:
                idx[i] += 1
                if idx[i] < len(ladders[i]):
                    rem[i] = ladders[i][idx[i]].size
        if max_capital is not None and out.capital >= max_capital - EPS:
            break
    if out.sets > EPS:
        out.avg_prices = [notional[i] / out.sets for i in range(n)]
    return out


def walk_single(levels: list[Level], *, max_capital: float, price_limit: float, fee_rate: float) -> tuple[float, float, float]:
    """Buy up to max_capital from asks priced <= price_limit. Returns (shares, cost_incl_fee, fees)."""
    shares = cost = fees = 0.0
    for l in levels:
        if l.price > price_limit + EPS:
            break
        unit = l.price + taker_fee(l.price, fee_rate)
        q = min(l.size, (max_capital - cost) / unit)
        if q <= EPS:
            break
        shares += q
        cost += q * unit
        fees += q * taker_fee(l.price, fee_rate)
    return shares, cost, fees
