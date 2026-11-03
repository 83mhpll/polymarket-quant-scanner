"""Quantitative Portfolio Optimizer, Fractional Kelly Sizer, VaR Stress Engine & Paper-Trading Ledger."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .models import OrderBook
from .strategies.base import ARB, ARB_ONCHAIN, MM, SIGNAL, YIELD, Opportunity


def kelly_fraction(opp: Opportunity, bankroll: float, max_cap_per_trade: float = 1000.0) -> dict[str, Any]:
    """Compute strategy-aware Fractional Kelly sizing and risk budget."""
    if bankroll <= 0 or opp.capital <= 0 or opp.roi <= 0:
        return {"full_kelly_pct": 0.0, "kelly_pct": 0.0, "risk_budget_pct": 0.0,
                "target_usd": 0.0, "recommended_usd": 0.0, "rationale_th": "", "rationale_en": ""}

    cap_frac = min(1.0, max_cap_per_trade / bankroll)
    p = min(max(opp.confidence, 0.05), 0.995)
    q = 1.0 - p
    r = max(opp.roi, 1e-4)

    if opp.kind in (ARB, ARB_ONCHAIN):
        # Locked profit if all legs fill; main risk is unhedged leg unwind slippage (~3%)
        loss_slip = 0.03
        full_k = max(0.0, (p * r - q * loss_slip) / max(r * loss_slip, 1e-6))
        full_k = min(full_k, 1.0)
        frac_k = min(0.50 * full_k, cap_frac)
        risk_pct = frac_k * loss_slip
        rat_th = f"Half-Kelly Arb (ความสำเร็จ {p:.0%}, สำรองความเสี่ยงขาหลุด {loss_slip:.0%})"
        rat_en = f"Half-Kelly Arb (fill prob {p:.0%}, leg-unwind buffer {loss_slip:.0%})"
    elif opp.kind == MM:
        # Two-sided Maker quoting: size by daily reward Sharpe proxy, cap at 15% bankroll
        roi_day = float(opp.metrics.get("roi_day", r / max(opp.days or 7.0, 1.0)))
        vol_proxy = max(0.02, 1.0 - p)
        sharpe_d = roi_day / vol_proxy
        full_k = min(0.30, sharpe_d * 0.6)
        frac_k = min(0.50 * full_k, 0.15, cap_frac)
        risk_pct = frac_k * vol_proxy * 0.5
        rat_th = f"MM Reward Sharpe Sizing (ผลตอบแทนรางวัล {roi_day:.2%}/วัน, เพดาน 15% พอร์ต)"
        rat_en = f"MM Reward Sharpe Sizing (reward yield {roi_day:.2%}/day, 15% bankroll cap)"
    elif opp.kind == YIELD:
        # Resolution carry bond: Quarter-Kelly against tail-default probability q_tail
        q_tail = max(1.0 - p, 0.015)
        p_win = 1.0 - q_tail
        # Boost effective edge by time velocity (short-duration carry compounds faster)
        days_eff = max(opp.days or 7.0, 0.5)
        vel_mult = min(3.0, math.sqrt(14.0 / days_eff))
        b_eff = r * vel_mult
        full_k = max(0.0, (p_win * b_eff - q_tail) / max(b_eff, 1e-6))
        frac_k = min(0.25 * full_k, 0.08, cap_frac)
        risk_pct = frac_k  # 100% loss in binary tail default
        rat_th = f"Quarter-Kelly Carry (โอกาสชนะประเมิน {p_win:.1%}, เพดาน Tail-Risk 8% พอร์ต)"
        rat_en = f"Quarter-Kelly Carry (implied survival {p_win:.1%}, 8% tail-risk cap)"
    else:
        # Directional statistical signal: fixed 1% bankroll risk at stop-loss distance
        entry = opp.legs[0].avg_price if opp.legs else 0.5
        stop = float(opp.metrics.get("stop", entry * 0.85))
        stop_dist_pct = max(abs(entry - stop) / max(entry, 1e-4), 0.05)
        risk_pct = 0.01
        full_k = risk_pct / stop_dist_pct
        frac_k = min(full_k, 0.05, cap_frac)
        rat_th = f"Fixed 1% Stop-Loss Risk Budget (ระยะตัดขาดทุน {stop_dist_pct:.1%})"
        rat_en = f"Fixed 1% Stop-Loss Risk Budget (stop distance {stop_dist_pct:.1%})"

    target_usd = frac_k * bankroll
    # Respect available orderbook depth (`opp.capital`) for taker strategies
    depth_cap = max_cap_per_trade if opp.kind == MM else opp.capital
    rec_usd = round(min(target_usd, depth_cap, max_cap_per_trade), 2)
    return {
        "full_kelly_pct": round(full_k, 4),
        "kelly_pct": round(frac_k, 4),
        "risk_budget_pct": round(risk_pct, 4),
        "target_usd": round(target_usd, 2),
        "recommended_usd": rec_usd,
        "rationale_th": rat_th,
        "rationale_en": rat_en,
    }


def build_optimal_portfolio(opps: list[Opportunity], cfg: Config) -> dict[str, Any]:
    """Construct a correlation-constrained, Kelly-sized optimal portfolio across all strategies."""
    bankroll = float(cfg.bankroll)
    bucket_caps = {
        ARB: 0.60 * bankroll,
        ARB_ONCHAIN: 0.30 * bankroll,
        MM: 0.40 * bankroll,
        YIELD: 0.25 * bankroll,
        SIGNAL: 0.10 * bankroll,
    }
    event_cap = 0.15 * bankroll
    max_deploy = 0.85 * bankroll  # keep >= 15% USDC liquidity reserve

    used_bucket: dict[str, float] = defaultdict(float)
    used_event: dict[str, float] = defaultdict(float)
    by_kind_usd: dict[str, float] = defaultdict(float)
    total_alloc = 0.0
    allocations: list[dict[str, Any]] = []

    ranked = sorted(opps, key=lambda o: o.score if o.score else o.compute_score(), reverse=True)
    for o in ranked:
        if total_alloc >= max_deploy - 1.0:
            break
        kf = kelly_fraction(o, bankroll, cfg.max_capital_per_trade)
        rec = kf["recommended_usd"]
        if rec < 10.0:
            continue
        ev_key = o.metrics.get("event_id") or o.event_title
        rem_global = max_deploy - total_alloc
        rem_bucket = bucket_caps.get(o.kind, 0.20 * bankroll) - used_bucket[o.kind]
        rem_event = event_cap - used_event[ev_key]
        alloc_usd = round(min(rec, rem_global, rem_bucket, rem_event), 2)
        if alloc_usd < 10.0:
            continue

        scale = alloc_usd / max(o.capital, 1e-9)
        exp_prof = round(o.exp_profit * scale, 2)
        if exp_prof < cfg.min_profit_usd:
            continue

        total_alloc += alloc_usd
        used_bucket[o.kind] += alloc_usd
        used_event[ev_key] += alloc_usd
        by_kind_usd[o.kind] = round(by_kind_usd[o.kind] + alloc_usd, 2)

        allocations.append({
            "id": o.id,
            "kind": o.kind,
            "strategy": o.strategy,
            "title": o.title,
            "event_title": o.event_title,
            "url": o.url,
            "recommended_usd": alloc_usd,
            "scale_factor": round(scale, 4),
            "expected_profit_usd": exp_prof,
            "roi": round(o.roi, 4),
            "apr": round(o.apr, 4) if o.apr is not None else None,
            "days": round(o.days, 2) if o.days is not None else None,
            "confidence": round(o.confidence, 3),
            "kelly_pct": round(alloc_usd / bankroll, 4),
            "risk_budget_pct": round(kf["risk_budget_pct"] * (alloc_usd / max(rec, 1e-9)), 4),
            "rationale_th": kf["rationale_th"],
            "rationale_en": kf["rationale_en"],
        })

    tot_profit = round(sum(a["expected_profit_usd"] for a in allocations), 2)
    port_roi = tot_profit / total_alloc if total_alloc > 0 else 0.0
    w_days = (
        sum(a["recommended_usd"] * (a["days"] if a["days"] is not None else 7.0) for a in allocations) / total_alloc
        if total_alloc > 0 else 0.0
    )
    w_apr = port_roi * 365.0 / max(w_days, 0.5) if total_alloc > 0 else 0.0

    # Risk analytics: parametric 95% VaR and worst single-event tail stress loss
    var_sq = 0.0
    event_tail_loss: dict[str, float] = defaultdict(float)
    for a in allocations:
        u = a["recommended_usd"]
        k = a["kind"]
        loss_frac = 0.03 if k in (ARB, ARB_ONCHAIN) else (0.12 if k == MM else (1.0 if k == YIELD else 0.25))
        p_fail = max(0.02, 1.0 - a["confidence"])
        sigma_pos = u * loss_frac * math.sqrt(p_fail * (1.0 - p_fail))
        var_sq += sigma_pos ** 2
        event_tail_loss[a["event_title"]] += u * loss_frac

    var_95 = round(1.645 * math.sqrt(var_sq), 2)
    max_dd_stress = round(max(event_tail_loss.values(), default=0.0), 2)
    ann_vol_proxy = max((var_95 / 1.645) * math.sqrt(365.0 / max(w_days, 1.0)), 1.0)
    sharpe_proxy = round((tot_profit * (365.0 / max(w_days, 1.0))) / ann_vol_proxy, 2) if total_alloc > 0 else 0.0

    return {
        "bankroll_usd": round(bankroll, 2),
        "total_allocated_usd": round(total_alloc, 2),
        "cash_reserve_usd": round(bankroll - total_alloc, 2),
        "utilization_pct": round(total_alloc / bankroll, 4) if bankroll > 0 else 0.0,
        "expected_profit_usd": tot_profit,
        "portfolio_roi": round(port_roi, 4),
        "weighted_apr": round(w_apr, 4),
        "weighted_days": round(w_days, 2),
        "var_95_usd": var_95,
        "max_drawdown_stress_usd": max_dd_stress,
        "sharpe_proxy": sharpe_proxy,
        "by_kind_usd": dict(by_kind_usd),
        "allocations": allocations,
    }


class PaperLedger:
    """Persistent institutional paper-trading execution & live mark-to-market PnL engine."""

    def __init__(self, out_dir: str | Path, initial_cash: float = 10_000.0):
        self.path = Path(out_dir) / "paper_portfolio.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.default_initial_cash = initial_cash

    def _default_state(self, initial_cash: float | None = None) -> dict[str, Any]:
        ic = float(initial_cash if initial_cash is not None else self.default_initial_cash)
        return {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "initial_cash_usd": ic,
            "cash_usd": ic,
            "realized_pnl_usd": 0.0,
            "total_fees_usd": 0.0,
            "auto_trade": True,
            "positions": {},
            "closed_trades": [],
        }

    def load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                if "initial_cash_usd" not in d:
                    # Migrate legacy paper_portfolio.json
                    pos = d.get("positions", {})
                    spent = sum(float(p.get("allocated_usd", 0.0)) for p in pos.values())
                    ic = max(self.default_initial_cash, spent + 2000.0)
                    d["initial_cash_usd"] = ic
                    d["cash_usd"] = round(ic - spent, 2)
                    d["realized_pnl_usd"] = float(d.get("realized_pnl_usd", 0.0))
                    d["total_fees_usd"] = float(d.get("total_fees_usd", 0.0))
                    d["auto_trade"] = bool(d.get("auto_trade", True))
                    d["closed_trades"] = d.get("closed_trades", [])
                return d
            except json.JSONDecodeError:
                pass
        return self._default_state()

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        return state

    def reset_account(self, initial_cash: float = 10_000.0) -> dict[str, Any]:
        st = self._default_state(initial_cash)
        st["auto_trade"] = False
        return self.save(st)

    def open_position(
        self,
        opp: Opportunity | dict[str, Any],
        amount_usd: float | None = None,
        timestamp: str | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        st = self.load()
        ts = timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds")
        d = opp.to_dict() if hasattr(opp, "to_dict") else dict(opp)
        oid = str(d["id"])
        positions = st.get("positions", {})
        base_cap = max(float(d.get("capital", 0.0)), 1e-6)
        alloc = float(amount_usd if amount_usd is not None else d.get("metrics", {}).get("kelly_usd", base_cap))
        alloc = round(min(alloc, float(st.get("cash_usd", 0.0))), 2)
        if alloc < 5.0:
            return False, "Insufficient paper cash balance (minimum $5.00)", st

        scale = alloc / base_cap
        exp_prof = round(float(d.get("exp_profit", 0.0)) * scale, 2)
        fee_est = round(float(d.get("metrics", {}).get("fees", 0.0)) * scale, 4)

        legs_in = d.get("legs", [])
        legs_out = []
        for l in legs_in:
            ep = float(l.get("avg_price", l.get("entry_price", 0.5)))
            sh = float(l.get("size", l.get("shares", 0.0))) * scale
            legs_out.append({
                "token_id": l.get("token_id", ""),
                "outcome": l.get("outcome", ""),
                "side": l.get("side", "BUY"),
                "order_type": l.get("order_type", "FOK"),
                "entry_price": round(ep, 4),
                "shares": round(sh, 4),
                "live_bid": round(ep, 4),
                "live_ask": round(ep, 4),
                "mark_price": round(ep, 4),
            })

        if oid in positions:
            # Add to existing position
            prev = positions[oid]
            prev_alloc = float(prev.get("allocated_usd", 0.0))
            new_alloc = round(prev_alloc + alloc, 2)
            prev["allocated_usd"] = new_alloc
            prev["expected_profit_usd"] = round(float(prev.get("expected_profit_usd", 0.0)) + exp_prof, 2)
            prev["fees_paid_usd"] = round(float(prev.get("fees_paid_usd", 0.0)) + fee_est, 4)
            for idx, lg in enumerate(prev.get("legs", [])):
                if idx < len(legs_out):
                     lg["shares"] = round(float(lg.get("shares", 0.0)) + legs_out[idx]["shares"], 4)
        else:
            positions[oid] = {
                "opened_at": ts,
                "id": oid,
                "kind": d.get("kind", "ARB"),
                "strategy": d.get("strategy", ""),
                "title": d.get("title", ""),
                "event_title": d.get("event_title", ""),
                "url": d.get("url", ""),
                "allocated_usd": alloc,
                "expected_profit_usd": exp_prof,
                "roi": float(d.get("roi", 0.0)),
                "confidence": float(d.get("confidence", 0.5)),
                "fees_paid_usd": fee_est,
                "legs": legs_out,
            }

        st["cash_usd"] = round(float(st.get("cash_usd", 0.0)) - alloc, 2)
        st["total_fees_usd"] = round(float(st.get("total_fees_usd", 0.0)) + fee_est, 4)
        st["positions"] = positions
        self.save(st)
        return True, f"Opened paper position {oid} (${alloc:,.2f})", st

    def close_position(
        self,
        oid: str,
        books: dict[str, OrderBook] | None = None,
        mode: str = "market",
        timestamp: str | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        st = self.load()
        positions = st.get("positions", {})
        if oid not in positions:
            return False, f"Position {oid} not found", st
        pos = positions.pop(oid)
        ts = timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds")
        cost = float(pos.get("allocated_usd", 0.0))
        kind = pos.get("kind", "")

        if mode == "settle":
            # Contractual arbitrage merge / resolution payout
            proceeds = round(cost + float(pos.get("expected_profit_usd", 0.0)), 2)
            exit_type = "SETTLE_MERGE"
        else:
            # Market liquidation using live CLOB books if available
            marked = self._mark_single_position(pos, books or {})
            proceeds = round(float(marked.get("mark_value_usd", cost)), 2)
            exit_type = "MARKET_CLOSE"

        pnl = round(proceeds - cost, 2)
        roi = round(pnl / max(cost, 1e-6), 4)
        st["cash_usd"] = round(float(st.get("cash_usd", 0.0)) + proceeds, 2)
        st["realized_pnl_usd"] = round(float(st.get("realized_pnl_usd", 0.0)) + pnl, 2)
        closed = st.get("closed_trades", [])
        closed.insert(0, {
            "id": oid,
            "kind": kind,
            "strategy": pos.get("strategy", ""),
            "title": pos.get("title", ""),
            "event_title": pos.get("event_title", ""),
            "url": pos.get("url", ""),
            "opened_at": pos.get("opened_at", ""),
            "closed_at": ts,
            "exit_type": exit_type,
            "cost_usd": round(cost, 2),
            "proceeds_usd": proceeds,
            "realized_pnl_usd": pnl,
            "roi": roi,
        })
        st["closed_trades"] = closed[:200]
        st["positions"] = positions
        self.save(st)
        return True, f"Closed {oid} ({exit_type}): PnL ${pnl:+,.2f}", st

    def record_allocation(self, portfolio: dict[str, Any], opps: list[Opportunity], timestamp: str) -> dict[str, Any]:
        st = self.load()
        if not st.get("auto_trade", True):
            return st
        opp_map = {o.id: o for o in opps}
        positions = st.get("positions", {})
        for a in portfolio.get("allocations", []):
            oid = a["id"]
            if oid in positions or oid not in opp_map:
                continue
            if float(st.get("cash_usd", 0.0)) < 25.0:
                break
            self.open_position(opp_map[oid], amount_usd=float(a["recommended_usd"]), timestamp=timestamp)
            st = self.load()
        return st

    def _mark_single_position(self, pos: dict[str, Any], books: dict[str, OrderBook]) -> dict[str, Any]:
        kind = pos.get("kind", "")
        cost = float(pos.get("allocated_usd", 0.0))
        exp_prof = float(pos.get("expected_profit_usd", 0.0))

        # Market-making two-sided quote positions accrue liquidity rewards + spread capture over time
        if kind == "MM":
            opened_str = pos.get("opened_at", "")
            elapsed_days = 0.05
            if opened_str:
                try:
                    dt = datetime.fromisoformat(opened_str.replace("Z", "+00:00"))
                    elapsed_days = max(0.02, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
                except ValueError:
                    pass
            # 7-day horizon expected reward scaled to elapsed holding time + live mid stability
            accrued = min(exp_prof, exp_prof * (elapsed_days / 7.0))
            legs_marked = []
            for l in pos.get("legs", []):
                b = books.get(l.get("token_id", ""))
                ep = float(l.get("entry_price", 0.5))
                bid = b.best_bid if (b and b.best_bid is not None) else float(l.get("live_bid", ep))
                ask = b.best_ask if (b and b.best_ask is not None) else float(l.get("live_ask", ep))
                mid = b.mid if (b and b.mid is not None) else round((bid + ask) / 2.0, 4)
                legs_marked.append({**l, "live_bid": round(bid, 4), "live_ask": round(ask, 4), "mark_price": round(mid, 4)})
            mark_val = cost + accrued
            return {
                **pos,
                "legs": legs_marked,
                "mark_value_usd": round(mark_val, 2),
                "locked_payout_usd": round(cost + exp_prof, 2),
                "unrealized_pnl_usd": round(accrued, 2),
                "unrealized_roi": round(accrued / max(cost, 1e-6), 4),
            }

        leg_cost = leg_val = 0.0
        legs_marked = []
        for l in pos.get("legs", []):
            sh = float(l.get("shares", 0.0))
            ep = float(l.get("entry_price", 0.5))
            side = l.get("side", "BUY").upper()
            b = books.get(l.get("token_id", ""))
            bid = b.best_bid if (b and b.best_bid is not None) else float(l.get("live_bid", ep))
            ask = b.best_ask if (b and b.best_ask is not None) else float(l.get("live_ask", ep))
            mid = b.mid if (b and b.mid is not None) else round((bid + ask) / 2.0, 4)
            if side == "BUY":
                mark = mid if mid > 0 else bid
                leg_cost += sh * ep
                leg_val += sh * mark
            else:
                # Short/split-sell leg gains when ask/mid drops below entry_price
                mark = mid if mid > 0 else ask
                leg_cost += sh * ep
                leg_val += sh * (2.0 * ep - mark)
            legs_marked.append({
                **l,
                "live_bid": round(bid, 4),
                "live_ask": round(ask, 4),
                "mark_price": round(mark, 4),
            })

        # Scale mark value to allocated_usd basis if leg_cost > 0
        if leg_cost > 0:
            scaled_val = cost * (leg_val / leg_cost)
        else:
            scaled_val = cost
        # For locked ARB baskets, redeemable merge value is at least cost + expected_profit_usd * 0.85
        if kind == "ARB":
            scaled_val = max(scaled_val, cost + exp_prof)
        pnl = scaled_val - cost
        return {
            **pos,
            "legs": legs_marked,
            "mark_value_usd": round(scaled_val, 2),
            "locked_payout_usd": round(cost + exp_prof, 2),
            "unrealized_pnl_usd": round(pnl, 2),
            "unrealized_roi": round(pnl / max(cost, 1e-6), 4),
        }

    def mark_to_market(self, books: dict[str, OrderBook]) -> dict[str, Any]:
        state = self.load()
        positions = state.get("positions", {})
        tot_cost = tot_val = 0.0
        marked = []
        for oid, pos in positions.items():
            m = self._mark_single_position(pos, books)
            tot_cost += float(pos.get("allocated_usd", 0.0))
            tot_val += float(m["mark_value_usd"])
            marked.append(m)
            positions[oid]["legs"] = m["legs"]

        # Persist updated live leg marks
        state["positions"] = positions
        self.save(state)

        init_cash = float(state.get("initial_cash_usd", self.default_initial_cash))
        cash = float(state.get("cash_usd", 0.0))
        realized = float(state.get("realized_pnl_usd", 0.0))
        unrealized = round(tot_val - tot_cost, 2)
        equity = round(cash + tot_val, 2)
        closed = state.get("closed_trades", [])
        wins = sum(1 for c in closed if float(c.get("realized_pnl_usd", 0.0)) > 0)
        win_rate = round(wins / len(closed), 4) if closed else 1.0

        return {
            "initial_cash_usd": round(init_cash, 2),
            "cash_usd": round(cash, 2),
            "equity_usd": equity,
            "total_cost_usd": round(tot_cost, 2),
            "total_mark_usd": round(tot_val, 2),
            "unrealized_pnl_usd": unrealized,
            "realized_pnl_usd": round(realized, 2),
            "total_pnl_usd": round(realized + unrealized, 2),
            "total_return_pct": round((equity - init_cash) / max(init_cash, 1e-6), 4),
            "total_fees_usd": round(float(state.get("total_fees_usd", 0.0)), 2),
            "auto_trade": bool(state.get("auto_trade", True)),
            "n_positions": len(marked),
            "n_closed": len(closed),
            "win_rate_pct": win_rate,
            "positions": marked,
            "closed_trades": closed[:50],
        }
