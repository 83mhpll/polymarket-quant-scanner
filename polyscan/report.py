"""Console table + JSON/CSV persistence."""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

from .scanner import ScanResult
from .strategies.base import ARB, ARB_ONCHAIN, MM, SIGNAL, YIELD

COLORS = {ARB: "\033[92m", ARB_ONCHAIN: "\033[96m", YIELD: "\033[93m", MM: "\033[95m", SIGNAL: "\033[90m"}
RESET, BOLD = "\033[0m", "\033[1m"


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def print_result(res: ScanResult, top: int = 30, color: bool = True) -> None:
    w = shutil.get_terminal_size((160, 40)).columns
    title_w = max(30, w - 98)
    c = (lambda k: COLORS.get(k, "")) if color else (lambda k: "")
    r, b = (RESET, BOLD) if color else ("", "")
    print(f"\n{b}Polymarket Scanner{r}  {res.timestamp}  | markets={res.n_markets} books={res.n_books} "
          f"negRiskEvents={res.n_negrisk_events} requests={res.n_requests} time={res.duration_s}s")
    print("found: " + ", ".join(f"{k}={v}" for k, v in res.by_strategy.items()))
    for e in res.errors:
        print(f"  ! {e}")
    hdr = f"{'#':>3} {'ID':<10} {'KIND':<6} {'STRATEGY':<13} {'CAPITAL':>9} {'PROFIT':>8} {'ROI':>7} {'DAYS':>6} {'APR':>8} {'CONF':>5} {'SCORE':>7}  OPPORTUNITY"
    print(b + hdr + r)
    print("-" * min(w, len(hdr) + title_w))
    for i, o in enumerate(res.opportunities[:top], 1):
        apr = o.apr
        apr_s = f"{apr:>7.0%}" if apr is not None and apr < 1000 else ("   >999x" if apr else "      -")
        days = f"{o.days:>6.1f}" if o.days is not None else "     -"
        print(f"{i:>3} {o.id:<10} {c(o.kind)}{o.kind:<6}{r} {o.strategy:<13} {o.capital:>9,.0f} {o.exp_profit:>8,.2f} "
              f"{o.roi:>7.2%} {days} {apr_s} {o.confidence:>5.2f} {o.score:>7.2f}  "
              f"{_trunc(o.event_title, title_w // 2)} :: {_trunc(o.title, title_w)}")
    if not res.opportunities:
        print("  (ไม่พบโอกาสที่ผ่านเกณฑ์ในรอบนี้ — ปกติสำหรับ arb แท้ ลองลด min_arb_edge / min_profit_usd)")
    if res.portfolio and res.portfolio.get("allocations"):
        p = res.portfolio
        print(f"\n{b}Optimal Quant Portfolio (Fractional Kelly + Correlation Caps){r}  "
              f"Allocated=${p['total_allocated_usd']:,.0f}/${p['bankroll_usd']:,.0f} ({p['utilization_pct']:.1%}) | "
              f"Exp.Profit=${p['expected_profit_usd']:,.2f} ({p['portfolio_roi']:.2%}) | "
              f"VaR95=${p['var_95_usd']:,.2f} | Sharpe={p['sharpe_proxy']:.2f}")


def print_portfolio(p: dict, mtm: dict | None = None) -> None:
    if not p or not p.get("allocations"):
        print("ยังไม่มีข้อมูลพอร์ต — รัน `python3 -m polyscan scan` ก่อน")
        return
    print(f"\n{BOLD}=== Optimal Quant Portfolio Allocation (Bankroll ${p['bankroll_usd']:,.0f}) ==={RESET}")
    print(f"Allocated: ${p['total_allocated_usd']:,.2f} ({p['utilization_pct']:.1%}) | Cash Reserve: ${p['cash_reserve_usd']:,.2f}")
    print(f"Expected Profit: ${p['expected_profit_usd']:,.2f} (ROI {p['portfolio_roi']:.2%}, Weighted APR {p['weighted_apr']:.0%}, Avg Days {p['weighted_days']:.1f})")
    print(f"Risk Metrics: 95% VaR = ${p['var_95_usd']:,.2f} | Worst Event Stress Loss = ${p['max_drawdown_stress_usd']:,.2f} | Sharpe Proxy = {p['sharpe_proxy']:.2f}")
    print(f"{'#':>2} {'ID':<10} {'KIND':<6} {'STRATEGY':<16} {'ALLOC $':>9} {'KELLY%':>7} {'EXP.PROF':>9} {'ROI':>7}  RATIONALE / MARKET")
    print("-" * 120)
    for i, a in enumerate(p["allocations"], 1):
        print(f"{i:>2} {a['id']:<10} {a['kind']:<6} {a['strategy']:<16} {a['recommended_usd']:>9,.2f} "
              f"{a['kelly_pct']:>6.1%} {a['expected_profit_usd']:>9,.2f} {a['roi']:>7.2%}  "
              f"{_trunc(a['event_title'], 40)} :: {_trunc(a['title'], 45)}")
    if mtm and mtm.get("n_positions"):
        print(f"\n{BOLD}Paper-Trading Mark-to-Market Ledger{RESET}: {mtm['n_positions']} tracked positions | "
              f"Cost ${mtm['total_cost_usd']:,.2f} -> Mark ${mtm['total_mark_usd']:,.2f} | "
              f"Unrealized PnL ${mtm['unrealized_pnl_usd']:+,.2f}")


def print_detail(o) -> None:
    print(f"\n{BOLD}[{o.id}] {o.kind} {o.strategy}{RESET}  {o.event_title}\n  {o.title}\n  {o.url}")
    print(f"  capital ${o.capital:,.2f}  profit ${o.exp_profit:,.2f}  ROI {o.roi:.2%}  days {o.days}  conf {o.confidence}")
    for l in o.legs:
        print(f"   - {l.side:<4} {l.size:>10,.2f} sh  {l.outcome[:50]:<50} avg {l.avg_price:.4f}  limit {l.price_limit:.4f}")
    for n in o.notes:
        print(f"   • {n}")


def print_farm_leaderboard(opps: list, top: int = 25) -> None:
    farm_opps = [o for o in opps if o.kind == MM]
    if not farm_opps:
        print("ยังไม่พบตลาด LP Farming ที่ผ่านเกณฑ์ — ลองสแกนรอบใหม่หรือลดเกณฑ์ mm_min_daily_roi")
        return
    print(f"\n{BOLD}=== Polymarket LP Farming & Liquidity Rewards Desk ==={RESET}")
    print(f"Active Farming Pools Found: {len(farm_opps)} pools | Total Daily Incentives: ${sum(o.metrics.get('daily_pool_usd', 0) for o in farm_opps):,.0f}/day")
    hdr = f"{'#':>2} {'ID':<10} {'COMP':<10} {'POOL/d':>8} {'SHARE':>7} {'REWARD/d':>9} {'REBATE/d':>9} {'NET/d':>8} {'APR':>7} {'TIGHT (BID/ASK)':<18} {'MIN SH':>7}  MARKET"
    print(BOLD + hdr + RESET)
    print("-" * 135)
    for i, o in enumerate(farm_opps[:top], 1):
        m = o.metrics or {}
        comp_str = f"{m.get('competition_label', 'Med')} ({m.get('competition_bars', 3)}/5)"
        pool_d = m.get('daily_pool_usd', 0.0)
        share = m.get('pool_share_pct', 0.0)
        rew_d = m.get('daily_reward_usd', 0.0)
        reb_d = m.get('maker_rebates_day_usd', 0.0)
        net_d = m.get('net_daily_usd', 0.0)
        apr = (net_d * 365 / max(o.capital, 1.0)) if o.capital > 0 else 0.0
        tight_s = f"{m.get('tight_bid', 0.0):.3f} / {m.get('tight_ask', 0.0):.3f}"
        min_sh = m.get('min_size_shares', 0.0)
        print(f"{i:>2} {o.id:<10} {comp_str:<10} ${pool_d:>7.0f} {share:>6.1%} ${rew_d:>8.2f} "
              f"${reb_d:>8.2f} ${net_d:>7.2f} {apr:>6.0%} {tight_s:<18} {min_sh:>7.0f}  {_trunc(o.event_title, 42)}")
    print("\nAction: วาง Limit Order สองฝั่งภายในแบนด์ ±v จาก Midpoint | หรือรัน `python3 -m polyscan execute <ID>` เพื่อตรวจสอบ Order Book สด")


def save_result(res: ScanResult, out_dir: str) -> tuple[Path, Path]:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    jp = d / "latest.json"
    jp.write_text(json.dumps(res.to_dict(), ensure_ascii=False, indent=2, default=str))
    cp = d / "latest.csv"
    with cp.open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["id", "kind", "strategy", "event", "title", "capital", "exp_profit", "roi", "days", "apr",
                     "confidence", "score", "url"])
        for o in res.opportunities:
            wr.writerow([o.id, o.kind, o.strategy, o.event_title, o.title, round(o.capital, 2),
                         round(o.exp_profit, 4), round(o.roi, 6), o.days, o.apr, o.confidence, round(o.score, 4), o.url])
    # append compact history for later analysis / backtesting of signal decay
    with (d / "history.jsonl").open("a") as f:
        for o in res.opportunities:
            if o.kind in (ARB, ARB_ONCHAIN):
                f.write(json.dumps({"ts": res.timestamp, "id": o.id, "strategy": o.strategy, "profit": o.exp_profit,
                                    "roi": o.roi, "capital": o.capital, "title": o.title}, ensure_ascii=False) + "\n")
    return jp, cp
