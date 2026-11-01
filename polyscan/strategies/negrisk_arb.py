"""Strategy 2 — Negative-risk (multi-outcome, mutually exclusive) basket arbitrage.

In a negRisk event exactly one outcome resolves YES. For N tradable legs:
  (a) YES-basket: buy 1 YES of every leg. Pays exactly $1.  Arb if sum(ask_YES + fee) < 1.
  (b) NO-basket:  buy 1 NO of every leg.  Pays N-1 (N if nobody wins). Arb if sum(ask_NO + fee) < N-1.
      NO shares can be converted instantly to (N-1) USDC via the NegRiskAdapter (convertPositions).
  (c) YES-sell:   mint and sell YES of every leg (split + convert). Arb if sum(bid_YES - fee) > 1.
The NO-basket is the *safer* side: if the event resolves with no winner it pays N, not N-1.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..execution_math import basket_walk
from ..models import Market, OrderBook
from .base import ARB, ARB_ONCHAIN, Leg, Opportunity

NAME = "negrisk_arb"


@dataclass
class EventGroup:
    event_id: str
    title: str
    url: str
    legs: list[Market]
    complete: bool               # all open legs of the event are present and have books
    augmented: bool = False      # outcomes can still be added (negRiskAugmented)
    has_other: bool = False      # an "Other" catch-all leg exists
    n_closed: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def days(self) -> float | None:
        ds = [m.days_to_end for m in self.legs if m.days_to_end is not None]
        return max(ds) if ds else None


def _legs(ms: list[Market], books: dict[str, OrderBook], fills, which: int, side: str, avg, worst, size) -> list[Leg]:
    out = []
    for i, m in enumerate(ms):
        o = m.outcomes[which]
        out.append(Leg(o.token_id, f"{m.label} [{o.name}]", m.question, side, worst[i], avg[i], size,
                       True, books[o.token_id].tick_size))
    return out


def scan(groups: list[EventGroup], books: dict[str, OrderBook], cfg: Config) -> list[Opportunity]:
    opps: list[Opportunity] = []
    for g in groups:
        ms = g.legs
        n = len(ms)
        if n < 2 or not g.complete:
            continue
        if any(not m.is_binary for m in ms):
            continue
        yes_b = [books.get(m.yes.token_id) for m in ms]
        no_b = [books.get(m.no.token_id) for m in ms]
        if any(b is None for b in yes_b + no_b):
            continue
        fees = [m.fee_rate for m in ms]
        min_shares = max(max(b.min_order_size for b in yes_b), 5.0)
        base_conf = 0.95
        risk_notes = list(g.notes)
        if g.augmented and not g.has_other:
            base_conf = 0.55
            risk_notes.append("negRiskAugmented และไม่มี leg 'Other' — อาจมีผู้ชนะที่ไม่อยู่ในรายการ")
        elif g.augmented:
            base_conf = 0.85
            risk_notes.append("negRiskAugmented: อาจมีการเพิ่ม outcome ใหม่ (แยกออกจาก 'Other')")

        # (a) YES basket — only valid if the listed legs are exhaustive
        exhaustive = not (g.augmented and not g.has_other)
        if exhaustive and all(b.asks for b in yes_b) and sum(b.asks[0].price for b in yes_b) < 1.0:
            f = basket_walk([b.asks for b in yes_b], fees, side="buy", set_value=1.0,
                            min_edge=cfg.min_arb_edge, max_capital=cfg.max_capital_per_trade,
                            slippage=cfg.slippage_buffer)
            if f.sets >= min_shares and f.profit >= cfg.min_profit_usd:
                opps.append(Opportunity(
                    strategy=NAME, kind=ARB,
                    title=f"YES-basket {n} legs, Σask={sum(f.avg_prices):.3f}",
                    event_title=g.title, url=g.url,
                    legs=_legs(ms, books, f, 0, "BUY", f.avg_prices, f.worst_prices, f.sets),
                    capital=f.capital, exp_profit=f.profit, roi=f.roi, days=g.days,
                    confidence=base_conf * 0.97,
                    notes=risk_notes + ["จ่าย $1 ต่อชุดเมื่อ event resolve (ถือจนจบ)",
                                        "เสี่ยงกรณี event resolve แบบไม่มีผู้ชนะ (ทุก leg = NO)"],
                    metrics={"sets": f.sets, "legs": n, "fees": f.fees, "edge_top": f.first_edge,
                             "payout": 1.0, "fee_rates": fees},
                ))

        # (b) NO basket — payout N-1 per set
        if all(b.asks for b in no_b) and sum(b.asks[0].price for b in no_b) < n - 1:
            f = basket_walk([b.asks for b in no_b], fees, side="buy", set_value=float(n - 1),
                            min_edge=cfg.min_arb_edge / (n - 1), max_capital=cfg.max_capital_per_trade,
                            slippage=cfg.slippage_buffer)
            if f.sets >= min_shares and f.profit >= cfg.min_profit_usd:
                opps.append(Opportunity(
                    strategy=NAME, kind=ARB,
                    title=f"NO-basket {n} legs, ΣNOask={sum(f.avg_prices):.3f} < {n - 1}",
                    event_title=g.title, url=g.url,
                    legs=_legs(ms, books, f, 1, "BUY", f.avg_prices, f.worst_prices, f.sets),
                    capital=f.capital, exp_profit=f.profit, roi=f.roi,
                    days=0.0,  # convertPositions gives N-1 USDC immediately
                    confidence=base_conf,
                    notes=risk_notes + [f"convertPositions (NegRiskAdapter) แปลง {n} NO -> {n - 1} USDC ได้ทันที",
                                        "ถ้าไม่มีผู้ชนะจะได้ N แทน N-1 (upside)"],
                    metrics={"sets": f.sets, "legs": n, "fees": f.fees, "edge_top": f.first_edge,
                             "payout": float(n - 1), "fee_rates": fees},
                ))
        elif getattr(cfg, "enable_hybrid_arb", True) and all(b.asks and b.bids for b in no_b) and sum(b.asks[0].price for b in no_b) < n - 1:
            # Hybrid NO-basket: rest Maker bid (0 fee) on the highest-fee leg, Taker ask on the other N-1 legs
            anchor = max(range(n), key=lambda k: no_b[k].asks[0].price * (1.0 - no_b[k].asks[0].price) * fees[k])
            hyb_ladders = [no_b[k].bids[:1] if k == anchor else no_b[k].asks for k in range(n)]
            hyb_fees = [0.0 if k == anchor else fees[k] for k in range(n)]
            f = basket_walk(hyb_ladders, hyb_fees, side="buy", set_value=float(n - 1),
                            min_edge=cfg.min_arb_edge / (n - 1), max_capital=cfg.max_capital_per_trade)
            if f.sets >= min_shares and f.profit >= cfg.min_profit_usd:
                legs = _legs(ms, books, f, 1, "BUY", f.avg_prices, f.worst_prices, f.sets)
                legs[anchor].outcome += " (Maker)"
                opps.append(Opportunity(
                    strategy=NAME, kind=ARB,
                    title=f"HYBRID NO-basket {n} legs, ΣNO={sum(f.avg_prices):.3f} < {n - 1}",
                    event_title=g.title, url=g.url,
                    legs=legs,
                    capital=f.capital, exp_profit=f.profit, roi=f.roi,
                    days=0.0,
                    confidence=base_conf * 0.85,
                    notes=risk_notes + [
                        f"วาง GTC Maker Bid ที่ขา '{ms[anchor].label} [NO]' @ {f.worst_prices[anchor]:.3f} (0% fee) เมื่อแมตช์แล้วกวาด Taker ที่เหลืออีก {n-1} ขา",
                        f"แปลง {n} NO -> {n - 1} USDC ทันทีผ่าน NegRiskAdapter (convertPositions)",
                    ],
                    metrics={"sets": f.sets, "legs": n, "fees": f.fees, "edge_top": f.first_edge,
                             "payout": float(n - 1), "fee_rates": hyb_fees, "mode": "hybrid_maker_taker", "maker_leg": anchor},
                ))

        # (c) sell YES basket (mint via split + convert) — needs on-chain ops
        if all(b.bids for b in yes_b) and sum(b.bids[0].price for b in yes_b) > 1.0:
            f = basket_walk([b.bids for b in yes_b], fees, side="sell", set_value=1.0,
                            min_edge=cfg.min_arb_edge, max_capital=cfg.max_capital_per_trade,
                            slippage=cfg.slippage_buffer)
            if f.sets >= min_shares and f.profit >= cfg.min_profit_usd:
                opps.append(Opportunity(
                    strategy=NAME, kind=ARB_ONCHAIN,
                    title=f"SELL YES-basket {n} legs, Σbid={sum(f.avg_prices):.3f}",
                    event_title=g.title, url=g.url,
                    legs=_legs(ms, books, f, 0, "SELL", f.avg_prices, f.worst_prices, f.sets),
                    capital=f.capital, exp_profit=f.profit, roi=f.roi, days=0.0,
                    confidence=base_conf * 0.9,
                    notes=risk_notes + ["ต้อง split USDC ทุก leg แล้ว convert NO ทั้งหมด -> N-1 USDC (on-chain)"],
                    metrics={"sets": f.sets, "legs": n, "edge_top": f.first_edge},
                ))
    return opps
