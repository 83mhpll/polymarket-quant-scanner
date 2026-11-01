"""Strategy 5 — Liquidity-reward market making.

Polymarket pays daily USDC rewards to resting two-sided limit orders near the midpoint.
Per-order score (docs "Liquidity Rewards"):   S(v, s) = ((v - s) / v)^2 * size
  v = max qualifying spread (rewardsMaxSpread, in cents), s = distance from the (size-cut) midpoint.
Two-sided score: Q_min = min(Q_bid, Q_ask) if 0.10 <= mid <= 0.90, else max(min(Qb,Qa), max(Qb,Qa)/3).
Our expected daily reward ≈ dailyRate * Q_ours / (Q_ours + Q_others).

We estimate Q_others from the live book, simulate placing `mm_capital` split across both sides
at a distance d (one tick behind the touch, inside v), and rank by reward ROI net of an
adverse-selection penalty derived from recent volatility.
"""
from __future__ import annotations

from collections import defaultdict

from ..config import Config
from ..models import Market, OrderBook
from .base import MM, Leg, Opportunity

NAME = "rewards_mm"
SINGLE_SIDE_DIV = 3.0


def _size_cut_mid(b: OrderBook, min_size: float) -> float | None:
    """Adjusted midpoint using best bid/ask with size >= min_size (prevents dust manipulation)."""
    valid_b = [l.price for l in b.bids if l.size >= min_size]
    valid_a = [l.price for l in b.asks if l.size >= min_size]
    if valid_b and valid_a and valid_b[0] < valid_a[0]:
        return (valid_b[0] + valid_a[0]) / 2.0
    return b.mid


def _q(levels, mid: float, v: float, min_size: float, side: str) -> float:
    tot = 0.0
    for l in levels:
        s = (mid - l.price) if side == "bid" else (l.price - mid)
        if s < 0 or s >= v or l.size < min_size:
            continue
        tot += ((v - s) / v) ** 2 * l.size
    return tot


def _qmin(qb: float, qa: float, mid: float) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(qb, qa), max(qb / SINGLE_SIDE_DIV, qa / SINGLE_SIDE_DIV))
    return min(qb, qa)


def scan(markets: list[Market], books: dict[str, OrderBook], cfg: Config) -> list[Opportunity]:
    opps: list[Opportunity] = []
    for m in markets:
        if m.rewards_daily <= 0 or m.rewards_max_spread <= 0 or not m.is_binary:
            continue
        if m.volume_24h < cfg.min_volume_24h or m.liquidity < cfg.min_liquidity:
            continue
        d_end = m.days_to_end
        if d_end is not None and d_end < cfg.mm_min_days_to_end:
            continue
        b = books.get(m.yes.token_id)
        if not b or not b.bids or not b.asks or b.mid is None:
            continue
        max_sp = getattr(cfg, "mm_max_spread", 0.12)
        if b.spread is None or b.spread > max_sp:
            continue
        min_sz = max(m.rewards_min_size, b.min_order_size)
        mid = _size_cut_mid(b, min_sz)
        if mid is None:
            continue
        v, tick = m.rewards_max_spread / 100.0, b.tick_size or m.tick
        qb_o = _q(b.bids, mid, v, min_sz, "bid")
        qa_o = _q(b.asks, mid, v, min_sz, "ask")
        q_others = _qmin(qb_o, qa_o, mid)
        # require existing qualifying depth so we never quote into a dead/unanchored book
        if q_others < min_sz * 0.25:
            continue

        # quote inside the reward band without crossing the touch
        bid_px = min(b.best_bid, round((mid - max(tick, v * 0.25)) / tick) * tick)
        ask_px = max(b.best_ask, round((mid + max(tick, v * 0.25)) / tick) * tick)
        if mid - bid_px >= v:
            bid_px = round((mid - v * 0.5) / tick) * tick
        if ask_px - mid >= v:
            ask_px = round((mid + v * 0.5) / tick) * tick
        if bid_px <= 0 or ask_px >= 1 or bid_px >= ask_px or bid_px >= b.best_ask or ask_px <= b.best_bid:
            continue
        half = cfg.mm_capital / 2
        bid_sz = half / bid_px                 # buy YES
        ask_sz = half / (1 - ask_px)           # sell YES == buy NO at 1-ask
        if bid_sz < min_sz or ask_sz < min_sz:
            continue
        qb_me = ((v - (mid - bid_px)) / v) ** 2 * bid_sz
        qa_me = ((v - (ask_px - mid)) / v) ** 2 * ask_sz
        q_me = _qmin(qb_me, qa_me, mid)
        if q_me <= 0:
            continue
        share = q_me / (q_me + q_others)
        daily = m.rewards_daily * share
        roi_day = daily / cfg.mm_capital

        # adverse selection: expected inventory loss ~ prob of getting picked off * move size
        vol = abs(m.one_hour_change) * 24 ** 0.5 * 0.5 + abs(m.one_day_change) * 0.5
        adverse_day = vol * cfg.mm_capital * 0.25
        net_day = daily - adverse_day
        if roi_day < cfg.mm_min_daily_roi or net_day <= cfg.min_profit_usd:
            continue
        horizon = min(7.0, d_end if d_end is not None else 7.0)
        # discount confidence when our share of the pool is overly dominant (thin competition = higher toxic-flow risk)
        comp_factor = min(1.0, q_others / (q_me + 1e-9)) ** 0.25
        conf = max(0.1, min(0.75, (0.75 - vol * 3) * comp_factor))
        opps.append(Opportunity(
            strategy=NAME, kind=MM,
            title=f"Quote {bid_px:.3f} / {ask_px:.3f}  (reward ${m.rewards_daily:.0f}/day, share {share:.1%})",
            event_title=m.question, url=m.url,
            legs=[
                Leg(m.yes.token_id, f"{m.yes.name} bid", m.question, "BUY", bid_px, bid_px, bid_sz, m.neg_risk, tick),
                Leg(m.no.token_id, f"{m.no.name} bid (=YES ask {ask_px:.3f})", m.question, "BUY",
                    round(1 - ask_px, 6), round(1 - ask_px, 6), ask_sz, m.neg_risk, tick),
            ],
            capital=cfg.mm_capital, exp_profit=net_day * horizon, roi=net_day * horizon / cfg.mm_capital,
            days=horizon, confidence=round(conf, 3),
            notes=[f"รางวัลคาดการณ์ ${daily:.2f}/วัน ({roi_day:.2%}/วัน), หัก adverse-selection ≈ ${adverse_day:.2f}/วัน",
                   f"Adjusted mid {mid:.3f} | ต้องอยู่ใน band ±{v * 100:.1f}¢ และ size ≥ {min_sz:.0f} shares",
                   "เป็น maker (ค่าธรรมเนียม 0) — ยกเลิก/ปรับ quote เมื่อ mid เคลื่อน"],
            notes_en=[f"Est. reward ${daily:.2f}/day ({roi_day:.2%}/day), less adverse-selection ≈ ${adverse_day:.2f}/day",
                      f"Adjusted mid {mid:.3f} | Must quote within ±{v * 100:.1f}¢ band with size ≥ {min_sz:.0f} shares",
                      "0% Maker fee — cancel/re-quote when midpoint shifts"],
            metrics={"daily_reward": daily, "share": share, "q_others": q_others, "q_me": q_me,
                     "mid": mid, "max_spread_c": m.rewards_max_spread, "roi_day": roi_day, "net_day": net_day,
                     "event_id": m.event_id},
        ))

    # Event-level diversification cap
    max_per_ev = getattr(cfg, "mm_max_per_event", 2)
    opps.sort(key=lambda o: o.compute_score(), reverse=True)
    by_ev: dict[str, int] = defaultdict(int)
    filtered: list[Opportunity] = []
    for o in opps:
        ev_id = o.metrics.get("event_id") or o.event_title
        if by_ev[ev_id] < max_per_ev:
            by_ev[ev_id] += 1
            filtered.append(o)
    return filtered
