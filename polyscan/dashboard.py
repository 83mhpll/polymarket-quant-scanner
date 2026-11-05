"""Real-time web dashboard server: high-frequency CLOB delta scanner + Paper Trading API + GitBook UI."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from aiohttp import web

from .alerts import send_alerts
from .config import Config
from .portfolio import PaperLedger
from .report import save_result
from .scanner import RealtimeScanner, ScanResult
from .strategies.base import Opportunity

log = logging.getLogger("polyscan.dashboard")
HTML = Path(__file__).with_name("dashboard.html")


class State:
    def __init__(self, cfg: Config, strategies: list[str] | None) -> None:
        self.result: ScanResult | None = None
        self.scanning = False
        self.last_error = ""
        self.rt_scanner = RealtimeScanner(cfg, strategies, universe_ttl_s=180.0)
        self.scan_mode = "REALTIME_DELTA"
        self.tick_interval = min(cfg.scan_interval, 6.0)


async def scan_loop(app: web.Application) -> None:
    cfg: Config = app["cfg"]
    st: State = app["state"]
    latest = Path(cfg.output_dir) / "latest.json"
    if latest.exists():
        try:
            d = json.loads(latest.read_text())
            st.result = ScanResult(
                timestamp=d.get("timestamp", ""), duration_s=d.get("duration_s", 0.0),
                n_markets=d.get("n_markets", 0), n_books=d.get("n_books", 0),
                n_negrisk_events=d.get("n_negrisk_events", 0), n_requests=d.get("n_requests", 0),
                opportunities=[Opportunity.from_dict(o) for o in d.get("opportunities", [])],
                by_strategy=d.get("by_strategy", {}), errors=d.get("errors", []),
                portfolio=d.get("portfolio", {}), paper_mtm=d.get("paper_mtm", {}),
            )
        except Exception:
            pass
    while True:
        st.scanning = True
        try:
            res = await st.rt_scanner.tick(force_full=False)
            st.result, st.last_error = res, ""
            save_result(res, cfg.output_dir)
            await send_alerts(res.opportunities, cfg)
            log.info("realtime tick #%d: %d opps in %.2fs", st.rt_scanner.tick_count, len(res.opportunities), res.duration_s)
        except Exception as e:
            st.last_error = repr(e)
            log.exception("scan failed")
        st.scanning = False
        await asyncio.sleep(st.tick_interval)


async def index(_: web.Request) -> web.Response:
    return web.Response(
        text=HTML.read_text(encoding="utf-8"),
        content_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


async def api_result(req: web.Request) -> web.Response:
    st: State = req.app["state"]
    cfg: Config = req.app["cfg"]
    ledger = PaperLedger(cfg.output_dir, cfg.bankroll)
    mtm = ledger.mark_to_market(st.rt_scanner.books)
    if st.result:
        st.result.paper_mtm = mtm
    body = st.result.to_dict() if st.result else {"opportunities": [], "paper_mtm": mtm}
    body.update({
        "scanning": st.scanning,
        "last_error": st.last_error,
        "interval": st.tick_interval,
        "tick_count": st.rt_scanner.tick_count,
        "realtime": True,
        "config": cfg.to_dict(),
    })
    return web.Response(text=json.dumps(body, ensure_ascii=False, default=str), content_type="application/json")


async def api_scan_now(req: web.Request) -> web.Response:
    st: State = req.app["state"]
    cfg: Config = req.app["cfg"]
    payload = await req.json() if req.can_read_body else {}
    force_full = bool(payload.get("full", False))
    if not st.scanning:
        st.scanning = True
        try:
            res = await st.rt_scanner.tick(force_full=force_full)
            st.result, st.last_error = res, ""
            save_result(res, cfg.output_dir)
        except Exception as e:
            st.last_error = repr(e)
        finally:
            st.scanning = False
    return await api_result(req)


async def api_paper(req: web.Request) -> web.Response:
    st: State = req.app["state"]
    cfg: Config = req.app["cfg"]
    ledger = PaperLedger(cfg.output_dir, cfg.bankroll)
    payload = await req.json() if req.can_read_body else {}
    action = payload.get("action", "status")
    msg = "OK"
    ok = True

    if action == "open":
        oid = str(payload.get("id", ""))
        amount = payload.get("amount_usd")
        opp_obj = payload.get("opportunity")
        if not opp_obj and st.result:
            for o in st.result.opportunities:
                if o.id == oid:
                    opp_obj = o
                    break
        if opp_obj:
            ok, msg, _ = ledger.open_position(opp_obj, amount_usd=float(amount) if amount else None)
        else:
            ok, msg = False, f"Opportunity {oid} not found"
    elif action == "deploy_kelly":
        if st.result and st.result.portfolio:
            s = ledger.load()
            s["auto_trade"] = True
            ledger.save(s)
            ledger.record_allocation(st.result.portfolio, st.result.opportunities, st.result.timestamp)
            msg = "Deployed optimal Kelly portfolio"
        else:
            ok, msg = False, "No portfolio available yet"
    elif action == "close":
        oid = str(payload.get("id", ""))
        mode = payload.get("mode", "market")
        ok, msg, _ = ledger.close_position(oid, books=st.rt_scanner.books, mode=mode)
    elif action == "toggle_auto":
        s = ledger.load()
        s["auto_trade"] = bool(payload.get("auto_trade", not s.get("auto_trade", True)))
        ledger.save(s)
        msg = f"Auto-Paper Trade {'ON' if s['auto_trade'] else 'OFF'}"
    elif action == "reset":
        cash = float(payload.get("initial_cash_usd", cfg.bankroll))
        ledger.reset_account(cash)
        msg = f"Paper account reset to ${cash:,.2f}"

    mtm = ledger.mark_to_market(st.rt_scanner.books)
    if st.result:
        st.result.paper_mtm = mtm
        save_result(st.result, cfg.output_dir)
    return web.Response(
        text=json.dumps({"ok": ok, "message": msg, "paper_mtm": mtm}, ensure_ascii=False, default=str),
        content_type="application/json",
    )


async def _start(app: web.Application) -> None:
    app["task"] = asyncio.create_task(scan_loop(app))


async def _stop(app: web.Application) -> None:
    app["task"].cancel()


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8080, strategies: list[str] | None = None) -> None:
    app = web.Application()
    app["cfg"], app["state"], app["strategies"] = cfg, State(cfg, strategies), strategies
    app.router.add_get("/", index)
    app.router.add_get("/api/result", api_result)
    app.router.add_post("/api/scan", api_scan_now)
    app.router.add_get("/api/paper", api_paper)
    app.router.add_post("/api/paper", api_paper)
    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)
    print(f"Real-Time Dashboard & Paper Trading Desk: http://{host}:{port}")
    web.run_app(app, host=host, port=port, print=None)
