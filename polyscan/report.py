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


def print_detail(o) -> None:
    print(f"\n{BOLD}[{o.id}] {o.kind} {o.strategy}{RESET}  {o.event_title}\n  {o.title}\n  {o.url}")
    print(f"  capital ${o.capital:,.2f}  profit ${o.exp_profit:,.2f}  ROI {o.roi:.2%}  days {o.days}  conf {o.confidence}")
    for l in o.legs:
        print(f"   - {l.side:<4} {l.size:>10,.2f} sh  {l.outcome[:50]:<50} avg {l.avg_price:.4f}  limit {l.price_limit:.4f}")
    for n in o.notes:
        print(f"   • {n}")


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
