"""Quantitative Portfolio Optimizer, Fractional Kelly Sizer, VaR Stress Engine & Paper-Trading Ledger."""
from __future__ import annotations

import json
import math
from collections import defaultdict
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
    """Persistent paper-trading & mark-to-market PnL tracker."""

    def __init__(self, out_dir: str | Path):
        self.path = Path(out_dir) / "paper_portfolio.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record_allocation(self, portfolio: dict[str, Any], opps: list[Opportunity], timestamp: str) -> dict[str, Any]:
        opp_map = {o.id: o for o in opps}
        state = self.load()
        positions = state.get("positions", {})
        for a in portfolio.get("allocations", []):
            oid = a["id"]
            if oid in positions or oid not in opp_map:
                continue
            o = opp_map[oid]
            scale = a["scale_factor"]
            positions[oid] = {
                "opened_at": timestamp,
                "id": oid,
                "kind": o.kind,
                "strategy": o.strategy,
                "title": o.title,
                "event_title": o.event_title,
                "allocated_usd": a["recommended_usd"],
                "expected_profit_usd": a["expected_profit_usd"],
                "legs": [
                    {"token_id": l.token_id, "outcome": l.outcome, "side": l.side,
                     "entry_price": l.avg_price, "shares": round(l.size * scale, 4)}
                    for l in o.legs
                ],
            }
        state["updated_at"] = timestamp
        state["positions"] = positions
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        return state

    def load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except json.JSONDecodeError:
                pass
        return {"updated_at": "", "positions": {}}

    def mark_to_market(self, books: dict[str, OrderBook]) -> dict[str, Any]:
        state = self.load()
        positions = state.get("positions", {})
        tot_cost = tot_val = 0.0
        marked = []
        for oid, pos in positions.items():
            leg_cost = leg_val = 0.0
            for l in pos.get("legs", []):
                sh = float(l["shares"])
                ep = float(l["entry_price"])
                b = books.get(l["token_id"])
                mark = (b.mid if b and b.mid is not None else (b.best_bid if b and b.best_bid is not None else ep))
                leg_cost += sh * ep
                leg_val += sh * mark
            pnl = leg_val - leg_cost
            tot_cost += leg_cost
            tot_val += leg_val
            marked.append({**pos, "mark_value_usd": round(leg_val, 2), "unrealized_pnl_usd": round(pnl, 2)})
        return {
            "n_positions": len(marked),
            "total_cost_usd": round(tot_cost, 2),
            "total_mark_usd": round(tot_val, 2),
            "unrealized_pnl_usd": round(tot_val - tot_cost, 2),
            "positions": marked,
        }
