"""Strategy 4 — Endgame / resolution-yield ("bond") carry.

Buy the near-certain side (ask in [0.90, 0.995]) of markets resolving soon and collect the
remaining cents at resolution. Return is small per trade but annualises well; the risk is
a fat left tail (lose the whole stake if the 'sure thing' fails), so we:
  * require short time-to-resolution and real depth,
  * penalise recent volatility (the market is still 'live'),
  * report break-even probability = all-in cost per share,
  * size with a tail-risk budget instead of Kelly (true probability is unknown).
"""
from __future__ import annotations

import math
from collections import defaultdict

from ..config import Config
from ..execution_math import walk_single
from ..models import Market, OrderBook
from .base import YIELD, Leg, Opportunity

NAME = "endgame_yield"


def scan(markets: list[Market], books: dict[str, OrderBook], cfg: Config) -> list[Opportunity]:
    opps: list[Opportunity] = []
    for m in markets:
        d = m.days_to_end
        if d is None or d > cfg.endgame_max_days or m.liquidity < cfg.min_liquidity or m.volume_24h < cfg.min_volume_24h:
            continue
        pending = d < 0  # past end date: awaiting resolution / UMA proposal
        if d < -14:
            continue
        for o in m.outcomes:
            b = books.get(o.token_id)
            if not b or not b.asks:
                continue
            a = b.best_ask
            if not (cfg.endgame_min_price <= a <= cfg.endgame_max_price):
                continue
            # tail-risk budget: never risk more than 10% of bankroll on one 'sure thing'
            cap = min(cfg.max_capital_per_trade, 0.10 * cfg.bankroll)
            sh, cost, fees = walk_single(b.asks, max_capital=cap, price_limit=a + cfg.endgame_price_window,
                                         fee_rate=m.fee_rate)
            if sh < b.min_order_size or cost <= 0:
                continue
            avg_all_in = cost / sh                 # break-even probability (including fee)
            avg_raw = (cost - fees) / sh           # pure fill price
            profit = sh - cost                     # if resolves as expected
            if profit < cfg.min_profit_usd:
                continue
            days_eff = max(d, 0.5) + (2.0 if pending or d < 1 else 0.0)  # UMA liveness / settlement lag
            roi = profit / cost
            apr = roi * 365 / days_eff
            if apr < cfg.endgame_min_apr:
                continue

            # confidence heuristics
            vol_pen = min(1.0, abs(m.one_day_change) / 0.05) * 0.25 + min(1.0, abs(m.one_hour_change) / 0.02) * 0.15
            spread_pen = min(0.2, (b.spread or 0) * 4)
            disputed = any("disput" in str(s).lower() for s in m.uma_statuses)
            conf = max(0.05, avg_all_in - vol_pen - spread_pen - (0.3 if disputed else 0.0))
            notes = [f"Break-even win-rate {avg_all_in:.3%}; ขาดทุนสูงสุด ${cost:,.2f} ถ้าผิด",
                     f"ผลตอบแทน {roi:.2%} ใน ~{days_eff:.1f} วัน (APR {apr:.0%})"]
            if pending:
                notes.append("เลยวันสิ้นสุดแล้ว — รอ UMA resolve (มักเป็นช่วงความเสี่ยงต่ำ ถ้าผลชัดเจน)")
            if disputed:
                notes.append("⚠️ มีประวัติ dispute ใน UMA")
            if abs(m.one_day_change) > 0.03:
                notes.append(f"⚠️ ราคาเคลื่อน {m.one_day_change:+.2%} ใน 24 ชม. — ตลาดยังไม่นิ่ง")
            opps.append(Opportunity(
                strategy=NAME, kind=YIELD,
                title=f"BUY {o.name} @ ≤{a + cfg.endgame_price_window:.3f} ({m.label})",
                event_title=m.event_title, url=m.url,
                legs=[Leg(o.token_id, o.name, m.question, "BUY", min(0.999, a + cfg.endgame_price_window),
                          avg_raw, sh, m.neg_risk, b.tick_size)],
                capital=cost, exp_profit=profit, roi=roi, days=days_eff,
                confidence=round(conf * (1 - math.exp(-cfg.endgame_max_days / max(days_eff, 0.1))), 3),
                notes=notes,
                metrics={"break_even_p": avg_all_in, "apr": apr, "shares": sh, "fees": fees,
                         "spread": b.spread, "d1_change": m.one_day_change, "pending": pending,
                         "event_id": m.event_id},
            ))

    # Event-level diversification cap (prevents 15 correlated 'Exact Score NO' legs on the same match)
    max_per_ev = getattr(cfg, "endgame_max_per_event", 2)
    opps.sort(key=lambda o: o.compute_score(), reverse=True)
    by_ev: dict[str, int] = defaultdict(int)
    filtered: list[Opportunity] = []
    for o in opps:
        ev_id = o.metrics.get("event_id") or o.event_title
        if by_ev[ev_id] < max_per_ev:
            by_ev[ev_id] += 1
            filtered.append(o)
    return filtered
