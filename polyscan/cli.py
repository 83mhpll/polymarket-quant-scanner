"""CLI entry point.

  python -m polyscan scan                  # one scan, print + save output/latest.{json,csv}
  python -m polyscan watch --interval 30   # continuous scanning + alerts
  python -m polyscan dashboard --port 8080 # web UI
  python -m polyscan show <id>             # details of an opportunity from latest scan
  python -m polyscan execute <id> [--live] [--yes]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .alerts import send_alerts
from .config import Config
from .report import print_detail, print_farm_leaderboard, print_portfolio, print_result, save_result
from .scanner import run_scan
from .strategies import ALL
from .strategies.base import Opportunity


def _load_opp(cfg: Config, oid: str) -> Opportunity:
    p = Path(cfg.output_dir) / "latest.json"
    if not p.exists():
        sys.exit("ยังไม่มีผลสแกน — รัน `python -m polyscan scan` ก่อน")
    for d in json.loads(p.read_text())["opportunities"]:
        if d["id"] == oid:
            return Opportunity.from_dict(d)
    sys.exit(f"ไม่พบ id {oid} ในผลสแกนล่าสุด")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="polyscan", description="Polymarket smart opportunity scanner")
    ap.add_argument("--config", help="path to config.json")
    ap.add_argument("--max-markets", type=int)
    ap.add_argument("--max-capital", type=float, dest="max_capital_per_trade")
    ap.add_argument("--bankroll", type=float)
    ap.add_argument("--min-edge", type=float, dest="min_arb_edge")
    ap.add_argument("--min-profit", type=float, dest="min_profit_usd")
    ap.add_argument("--strategies", help="comma list: " + ",".join(ALL))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("--top", type=int, default=40); s.add_argument("--no-color", action="store_true")
    w = sub.add_parser("watch"); w.add_argument("--interval", type=int); w.add_argument("--top", type=int, default=25)
    d = sub.add_parser("dashboard"); d.add_argument("--port", type=int, default=8080); d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--interval", type=int)
    fm = sub.add_parser("farm", help="show Polymarket LP Farming & Liquidity Rewards leaderboard")
    fm.add_argument("--top", type=int, default=25)
    sh = sub.add_parser("show"); sh.add_argument("id")
    pf = sub.add_parser("portfolio", help="show optimal Kelly portfolio & paper-trading PnL")
    ex = sub.add_parser("execute"); ex.add_argument("id"); ex.add_argument("--live", action="store_true")
    ex.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.load(a.config, max_markets=a.max_markets, max_capital_per_trade=a.max_capital_per_trade,
                      bankroll=a.bankroll, min_arb_edge=a.min_arb_edge, min_profit_usd=a.min_profit_usd,
                      scan_interval=getattr(a, "interval", None))
    strategies = [x.strip() for x in a.strategies.split(",")] if a.strategies else None

    if a.cmd == "scan":
        res = asyncio.run(run_scan(cfg, strategies))
        print_result(res, a.top, color=not a.no_color)
        jp, cp = save_result(res, cfg.output_dir)
        print(f"\nsaved → {jp}  {cp}")
    elif a.cmd == "watch":
        async def loop() -> None:
            while True:
                res = await run_scan(cfg, strategies)
                print("\033[2J\033[H", end="")
                print_result(res, a.top)
                save_result(res, cfg.output_dir)
                n = await send_alerts(res.opportunities, cfg)
                if n:
                    print(f"\nsent {n} alert(s)")
                await asyncio.sleep(cfg.scan_interval)
        try:
            asyncio.run(loop())
        except KeyboardInterrupt:
            pass
    elif a.cmd == "dashboard":
        from .dashboard import serve
        serve(cfg, a.host, a.port, strategies)
    elif a.cmd == "farm":
        p = Path(cfg.output_dir) / "latest.json"
        if not p.exists():
            print("ยังไม่มีผลสแกนในเครื่อง กำลังสแกนตลาดสดให้อัตโนมัติ...")
            res = asyncio.run(run_scan(cfg, ["rewards_mm"]))
            save_result(res, cfg.output_dir)
            opps = res.opportunities
        else:
            d = json.loads(p.read_text())
            opps = [Opportunity.from_dict(o) for o in d.get("opportunities", [])]
            if not any(o.kind == "MM" for o in opps):
                res = asyncio.run(run_scan(cfg, ["rewards_mm"]))
                save_result(res, cfg.output_dir)
                opps = res.opportunities
        print_farm_leaderboard(opps, a.top)
    elif a.cmd == "show":
        print_detail(_load_opp(cfg, a.id))
    elif a.cmd == "portfolio":
        p = Path(cfg.output_dir) / "latest.json"
        if not p.exists():
            sys.exit("ยังไม่มีผลสแกน — รัน `python3 -m polyscan scan` ก่อน")
        d = json.loads(p.read_text())
        print_portfolio(d.get("portfolio", {}), d.get("paper_mtm", {}))
    elif a.cmd == "execute":
        from .executor import Executor, revalidate
        opp = _load_opp(cfg, a.id)
        ok, why, opp = asyncio.run(revalidate(opp, cfg))
        print_detail(opp)
        print(f"\nre-validation: {why}")
        if not ok:
            sys.exit(1)
        mode = "LIVE " if a.live else "DRY-RUN"
        if a.live and not a.yes:
            if input(f"ส่งคำสั่ง {mode} ทุน ${opp.capital:,.2f}? พิมพ์ 'yes' เพื่อยืนยัน: ").strip().lower() != "yes":
                sys.exit("ยกเลิก")
        for r in Executor(cfg, live=a.live).execute(opp):
            print(json.dumps(r, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
