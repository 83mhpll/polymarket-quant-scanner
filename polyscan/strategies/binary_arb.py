"""Strategy 1 — Binary complement arbitrage.

(a) BUY-BOTH:   ask(YES) + ask(NO) + fees < 1  -> buy both; merge immediately for $1 (or hold to resolution).
(b) SPLIT-SELL: bid(YES) + bid(NO) - fees > 1  -> split $1 into YES+NO on-chain (CTF), sell both.
Both are depth-aware: we walk the books level-by-level and stop at the marginal break-even.
"""
from __future__ import annotations

from ..config import Config
from ..execution_math import basket_walk
from ..models import Market, OrderBook
from .base import ARB, ARB_ONCHAIN, Leg, Opportunity

NAME = "binary_arb"


def scan(markets: list[Market], books: dict[str, OrderBook], cfg: Config) -> list[Opportunity]:
    opps: list[Opportunity] = []
    for m in markets:
        if not m.is_binary:
            continue
        b0, b1 = books.get(m.yes.token_id), books.get(m.no.token_id)
        if not b0 or not b1:
            continue
        min_shares = max(b0.min_order_size, b1.min_order_size, m.min_size)
        fees = [m.fee_rate, m.fee_rate]

        # (a) buy both outcomes
        if b0.asks and b1.asks and b0.asks[0].price + b1.asks[0].price < 1.0:
            f = basket_walk([b0.asks, b1.asks], fees, side="buy", set_value=1.0,
                            min_edge=cfg.min_arb_edge, max_capital=cfg.max_capital_per_trade,
                            slippage=cfg.slippage_buffer)
            if f.sets >= min_shares and f.profit >= cfg.min_profit_usd:
                opps.append(Opportunity(
                    strategy=NAME, kind=ARB,
                    title=f"BUY {m.yes.name}+{m.no.name} @ {f.avg_prices[0]:.3f}+{f.avg_prices[1]:.3f}",
                    event_title=m.question, url=m.url,
                    legs=[
                        Leg(m.yes.token_id, m.yes.name, m.question, "BUY", f.worst_prices[0], f.avg_prices[0], f.sets, m.neg_risk, b0.tick_size),
                        Leg(m.no.token_id, m.no.name, m.question, "BUY", f.worst_prices[1], f.avg_prices[1], f.sets, m.neg_risk, b1.tick_size),
                    ],
                    capital=f.capital, exp_profit=f.profit, roi=f.roi,
                    days=0.0,  # mergePositions returns $1/set immediately
                    confidence=0.97,
                    notes=["Merge YES+NO -> 1 USDC ทันทีผ่าน CTF mergePositions หรือถือจนตลาดจบ",
                           "ความเสี่ยง: leg risk ขณะส่งคำสั่ง (ใช้ FOK)"],
                    notes_en=["Merge YES+NO -> 1 USDC immediately via CTF mergePositions, or hold to resolution",
                              "Execution risk: leg risk between orders (use FOK limit orders)"],
                    metrics={"sets": f.sets, "fees": f.fees, "edge_top": f.first_edge, "edge_marginal": f.last_edge,
                             "payout": 1.0, "fee_rates": fees},
                ))

        # (b) split $1 -> YES+NO and sell both into bids
        if b0.bids and b1.bids and b0.bids[0].price + b1.bids[0].price > 1.0:
            f = basket_walk([b0.bids, b1.bids], fees, side="sell", set_value=1.0,
                            min_edge=cfg.min_arb_edge, max_capital=cfg.max_capital_per_trade,
                            slippage=cfg.slippage_buffer)
            if f.sets >= min_shares and f.profit >= cfg.min_profit_usd:
                opps.append(Opportunity(
                    strategy=NAME, kind=ARB_ONCHAIN,
                    title=f"SPLIT+SELL {m.yes.name}/{m.no.name} @ {f.avg_prices[0]:.3f}+{f.avg_prices[1]:.3f}",
                    event_title=m.question, url=m.url,
                    legs=[
                        Leg(m.yes.token_id, m.yes.name, m.question, "SELL", f.worst_prices[0], f.avg_prices[0], f.sets, m.neg_risk, b0.tick_size),
                        Leg(m.no.token_id, m.no.name, m.question, "SELL", f.worst_prices[1], f.avg_prices[1], f.sets, m.neg_risk, b1.tick_size),
                    ],
                    capital=f.capital, exp_profit=f.profit, roi=f.roi, days=0.0, confidence=0.93,
                    notes=["ต้อง splitPosition USDC -> YES+NO บน CTF ก่อนขาย (on-chain)"],
                    notes_en=["Requires on-chain CTF splitPosition (USDC -> YES+NO) before selling both legs"],
                    metrics={"sets": f.sets, "fees": f.fees, "edge_top": f.first_edge},
                ))
    return opps
