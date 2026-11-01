"""Strategy 6 — Statistical overreaction / order-flow signal (directional, speculative).

For liquid markets with an abnormal recent move we compute, in log-odds space:
  z = Δlogit(last 3h) / (σ_hourly · √3)
and combine it with order-book imbalance near the mid:
  * |z| large & book leans AGAINST the move  -> mean-reversion (fade) signal
  * |z| large & book leans WITH the move     -> momentum (follow) signal
Targets/stops are expressed in probability space; size uses a fixed-fractional risk budget
(1% bankroll at the stop). This is NOT arbitrage — treat as a ranked watch-list.
"""
from __future__ import annotations

import asyncio
import math
import statistics

from ..config import Config
from ..execution_math import taker_fee
from ..models import Market, OrderBook
from .base import SIGNAL, Leg, Opportunity

NAME = "stat_signal"


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def inv_logit(x: float) -> float:
    return 1 / (1 + math.exp(-x))


async def scan(markets: list[Market], books: dict[str, OrderBook], cfg: Config, client) -> list[Opportunity]:
    cands = [m for m in markets if m.is_binary and m.liquidity >= 5000 and m.volume_24h >= 5000
             and (m.days_to_end or 0) > 1 and m.yes.token_id in books and books[m.yes.token_id].mid
             and 0.05 <= books[m.yes.token_id].mid <= 0.95]
    cands.sort(key=lambda m: abs(m.one_hour_change) + abs(m.one_day_change) / 4, reverse=True)
    cands = cands[: cfg.signal_top_k]
    hists = await asyncio.gather(*(client.price_history(m.yes.token_id, "1w", 60) for m in cands))

    opps: list[Opportunity] = []
    for m, h in zip(cands, hists):
        if len(h) < 48:
            continue
        xs = [logit(p) for _, p in h]
        rets = [b - a for a, b in zip(xs[:-1], xs[1:])]
        base = rets[:-3]
        if len(base) < 24:
            continue
        # robust sigma: MAD-scaled, floored (illiquid series have many zero returns)
        med = statistics.median(base)
        mad = statistics.median([abs(r - med) for r in base]) * 1.4826
        sigma = max(statistics.pstdev(base) * 0.5 + mad * 0.5, 0.01)
        move_3h = xs[-1] - xs[-4]
        move_6h = xs[-1] - xs[-7] if len(xs) >= 8 else move_3h
        z3 = move_3h / (sigma * math.sqrt(3))
        z6 = move_6h / (sigma * math.sqrt(6))
        z, move = (z3, move_3h) if abs(z3) >= abs(z6) else (z6, move_6h)
        if abs(z) < cfg.signal_min_z:
            continue
        b_yes = books[m.yes.token_id]
        imb = b_yes.imbalance(0.03)
        if imb is None or abs(imb) < 0.15:
            continue
        mode = "momentum" if (imb > 0) == (move > 0) else "reversion"
        go_yes = (move > 0) if mode == "momentum" else (move < 0)
        o = m.yes if go_yes else m.no
        b = books.get(o.token_id)
        if not b or not b.asks or (b.spread and b.spread > 0.05):
            continue
        entry = b.best_ask
        # target: 50% retrace (reversion) or 50% extension (momentum) of the move, in logit space
        # make sure the move anchor exceeds the bid-ask spread so target > entry
        eff_move = math.copysign(max(abs(move), sigma * 2.5), move)
        x_now = logit(b_yes.mid)
        x_tgt = x_now - 0.55 * eff_move if mode == "reversion" else x_now + 0.55 * eff_move
        x_stop = x_now + 0.35 * eff_move if mode == "reversion" else x_now - 0.35 * eff_move
        p_tgt_yes, p_stop_yes = inv_logit(x_tgt), inv_logit(x_stop)
        tgt = p_tgt_yes if go_yes else 1 - p_tgt_yes
        stop = p_stop_yes if go_yes else 1 - p_stop_yes
        fee = taker_fee(entry, m.fee_rate) * 2
        gain, loss = tgt - entry - fee, entry - stop + fee
        if gain <= 0 or loss <= 0:
            continue
        # heuristic hit probability: tilt from 50% by signal strength (bounded)
        strength = min(1.0, (abs(z) - cfg.signal_min_z) / 3) * min(1.0, abs(imb))
        p_hit = 0.5 + 0.12 * strength
        ev_per_share = p_hit * gain - (1 - p_hit) * loss
        if ev_per_share <= 0:
            continue
        risk_budget = 0.01 * cfg.bankroll
        shares = min(risk_budget / loss, cfg.max_capital_per_trade / entry, sum(l.size for l in b.asks[:3]))
        if shares < b.min_order_size:
            continue
        opps.append(Opportunity(
            strategy=NAME, kind=SIGNAL,
            title=f"{mode.upper()} BUY {o.name} @ {entry:.3f} → tgt {tgt:.3f} / stop {stop:.3f}",
            event_title=m.question, url=m.url,
            legs=[Leg(o.token_id, o.name, m.question, "BUY", entry, entry, shares, m.neg_risk, b.tick_size)],
            capital=shares * entry, exp_profit=shares * ev_per_share, roi=ev_per_share / entry,
            days=1.0, confidence=0.25 + 0.15 * strength,
            notes=[f"z={z:+.2f} (3h move in log-odds), book imbalance={imb:+.2f}",
                   f"R:R = {gain / loss:.2f}, p_hit (heuristic) ≈ {p_hit:.0%}",
                   "สัญญาณเชิงสถิติ — มีความเสี่ยงทิศทาง ใช้ stop-loss เสมอ"],
            metrics={"z": z, "imbalance": imb, "sigma_h": sigma, "target": tgt, "stop": stop, "mode": mode},
        ))
    return opps
