"""Order execution (optional). DRY-RUN by default.

Live trading requires `pip install py-clob-client` and env vars:
  POLY_PRIVATE_KEY      private key of the signing wallet
  POLY_FUNDER           address holding funds (your Polymarket proxy wallet) — optional for EOA
  POLY_SIGNATURE_TYPE   0 = EOA, 1 = email/Magic proxy, 2 = browser-wallet proxy (default 1)

Safety:
  * every opportunity is RE-VALIDATED against fresh order books right before sending,
  * arb legs are sent as FOK (fill-or-kill) at the worst acceptable price, thinnest leg first,
  * if a later leg fails, already-filled legs are flagged for unwind (FAK sell at bid),
  * notional is capped by cfg.max_capital_per_trade, and you must confirm unless --yes.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path

from .api import PolyClient
from .config import CLOB_URL, Config
from .execution_math import basket_walk
from .strategies.base import ARB, ARB_ONCHAIN, MM, Leg, Opportunity

log = logging.getLogger("polyscan.executor")


def _size_step(tick: float) -> float:
    # keep price*size at <= 2 decimals (CLOB amount precision for marketable orders)
    if tick >= 0.01:
        return 1.0
    if tick >= 0.001:
        return 10.0
    return 100.0


def _round_size(size: float, tick: float) -> float:
    st = _size_step(tick)
    return math.floor(size / st) * st


def _round_px(px: float, tick: float, side: str) -> float:
    n = px / tick
    n = math.ceil(n - 1e-9) if side == "BUY" else math.floor(n + 1e-9)
    return round(min(max(n * tick, tick), 1 - tick), 6)


async def revalidate(opp: Opportunity, cfg: Config) -> tuple[bool, str, Opportunity]:
    """Re-price the opportunity on fresh books. Returns (ok, reason, possibly-resized opp)."""
    async with PolyClient(cfg) as c:
        books = await c.fetch_books([l.token_id for l in opp.legs])
    if any(l.token_id not in books for l in opp.legs):
        return False, "missing book", opp
    if opp.kind in (ARB,) and all(l.side == "BUY" for l in opp.legs):
        payout = float(opp.metrics.get("payout", 1.0))
        fee_rates = opp.metrics.get("fee_rates") or [0.07] * len(opp.legs)  # conservative if unknown
        maker_idx = opp.metrics.get("maker_leg")
        ladders = [
            books[l.token_id].bids[:1] if (maker_idx is not None and i == maker_idx) else books[l.token_id].asks
            for i, l in enumerate(opp.legs)
        ]
        f = basket_walk(ladders, fee_rates, side="buy", set_value=payout,
                        min_edge=cfg.min_arb_edge / payout, max_capital=min(opp.capital, cfg.max_capital_per_trade))
        if f.sets < max(books[l.token_id].min_order_size for l in opp.legs) or f.profit <= 0:
            return False, f"edge gone (top edge {f.first_edge:+.4f})", opp
        for i, l in enumerate(opp.legs):
            l.size, l.price_limit, l.avg_price = f.sets, f.worst_prices[i], f.avg_prices[i]
        opp.capital, opp.exp_profit, opp.roi = f.capital, f.profit, f.roi
        return True, f"ok: {f.sets:.2f} sets, profit ${f.profit:.2f}", opp
    # single-leg / MM: make sure price has not run away
    for l in opp.legs:
        b = books[l.token_id]
        if l.side == "BUY" and opp.kind != MM and (b.best_ask is None or b.best_ask > l.price_limit + 1e-9):
            return False, f"ask moved to {b.best_ask} > limit {l.price_limit}", opp
    return True, "ok", opp


class Executor:
    def __init__(self, cfg: Config, live: bool = False):
        self.cfg, self.live = cfg, live
        self.client = None
        self.log_path = Path(cfg.output_dir) / "trades.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if live:
            self._init_client()

    def _init_client(self) -> None:
        try:
            from py_clob_client.client import ClobClient  # type: ignore
        except ImportError as e:
            raise SystemExit("py-clob-client ไม่ได้ติดตั้ง: pip install py-clob-client") from e
        pk = os.getenv("POLY_PRIVATE_KEY")
        if not pk:
            raise SystemExit("ต้องตั้งค่า POLY_PRIVATE_KEY ก่อนใช้ --live")
        funder = os.getenv("POLY_FUNDER") or None
        sig = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))
        chain_id = int(os.getenv("POLY_CHAIN_ID", str(getattr(self.cfg, "chain_id", 137))))
        self.client = ClobClient(CLOB_URL, key=pk, chain_id=chain_id, signature_type=sig, funder=funder)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def _record(self, payload: dict) -> None:
        payload["ts"] = time.time()
        with self.log_path.open("a") as f:
            f.write(json.dumps(payload, default=str) + "\n")

    def _send(self, leg: Leg, order_type: str) -> dict:
        size = _round_size(leg.size, leg.tick_size)
        px = _round_px(leg.price_limit, leg.tick_size, leg.side)
        req = {"token_id": leg.token_id, "side": leg.side, "price": px, "size": size, "type": order_type,
               "neg_risk": leg.neg_risk, "outcome": leg.outcome}
        if size <= 0:
            return {**req, "success": False, "error": "size rounds to 0"}
        if not self.live:
            return {**req, "success": True, "dry_run": True}
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions  # type: ignore
        from py_clob_client.order_builder.constants import BUY, SELL  # type: ignore
        opts = PartialCreateOrderOptions(tick_size=str(leg.tick_size), neg_risk=leg.neg_risk)
        signed = self.client.create_order(
            OrderArgs(token_id=leg.token_id, price=px, size=size, side=BUY if leg.side == "BUY" else SELL), opts)
        resp = self.client.post_order(signed, getattr(OrderType, order_type))
        ok = bool(resp and resp.get("success", False) and not resp.get("errorMsg"))
        return {**req, "success": ok, "response": resp}

    def execute(self, opp: Opportunity) -> list[dict]:
        if opp.kind == ARB_ONCHAIN:
            raise SystemExit("โอกาสประเภท ARB* ต้องทำ split/convert on-chain — executor นี้ไม่ส่งให้อัตโนมัติ")
        if opp.capital > self.cfg.max_capital_per_trade * 1.0001:
            raise SystemExit(f"capital ${opp.capital:.2f} เกิน max_capital_per_trade")
        results: list[dict] = []
        if opp.kind == MM:
            for l in opp.legs:  # resting maker quotes
                results.append(self._send(l, "GTC"))
        elif opp.kind == ARB:
            if opp.metrics.get("mode") == "hybrid_maker_taker":
                maker_idx = int(opp.metrics.get("maker_leg", 0))
                results.append({**self._send(opp.legs[maker_idx], "GTC"), "role": "maker_anchor"})
                for i, l in enumerate(opp.legs):
                    if i != maker_idx:
                        results.append({
                            "token_id": l.token_id, "side": l.side, "price": _round_px(l.price_limit, l.tick_size, l.side),
                            "size": _round_size(l.size, l.tick_size), "type": "FOK", "outcome": l.outcome,
                            "staged_on_maker_fill": True, "success": True,
                        })
            else:
                # thinnest leg first minimises the chance of being left with an unhedged leg
                for l in sorted(opp.legs, key=lambda l: l.size * l.avg_price):
                    r = self._send(l, "FOK")
                    results.append(r)
                    if not r["success"]:
                        filled = [x for x in results[:-1] if x["success"]]
                        if filled:
                            log.error("leg failed after %d filled legs — UNWIND REQUIRED: %s", len(filled), filled)
                            results.append({"unwind_required": filled})
                        break
        else:
            for l in opp.legs:
                results.append(self._send(l, "FOK"))
        self._record({"opp": opp.id, "strategy": opp.strategy, "kind": opp.kind, "live": self.live, "results": results})
        return results
