"""Local web dashboard: background scan loop + auto-refreshing UI at http://localhost:8080"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from aiohttp import web

from .alerts import send_alerts
from .config import Config
from .report import save_result
from .scanner import ScanResult, run_scan

log = logging.getLogger("polyscan.dashboard")
HTML = Path(__file__).with_name("dashboard.html")


class State:
    def __init__(self) -> None:
        self.result: ScanResult | None = None
        self.scanning = False
        self.last_error = ""


async def scan_loop(app: web.Application) -> None:
    cfg: Config = app["cfg"]
    st: State = app["state"]
    # Load existing latest.json immediately so the UI is populated on startup
    latest = Path(cfg.output_dir) / "latest.json"
    if latest.exists():
        try:
            d = json.loads(latest.read_text())
            from .strategies.base import Opportunity
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
            res = await run_scan(cfg, app["strategies"])
            st.result, st.last_error = res, ""
            save_result(res, cfg.output_dir)
            await send_alerts(res.opportunities, cfg)
            log.info("scan done: %d opportunities in %.1fs", len(res.opportunities), res.duration_s)
        except Exception as e:  # never kill the loop
            st.last_error = repr(e)
            log.exception("scan failed")
        st.scanning = False
        await asyncio.sleep(cfg.scan_interval)


async def index(_: web.Request) -> web.Response:
    return web.Response(text=HTML.read_text(encoding="utf-8"), content_type="text/html")


async def api_result(req: web.Request) -> web.Response:
    st: State = req.app["state"]
    body = st.result.to_dict() if st.result else {"opportunities": []}
    body.update({"scanning": st.scanning, "last_error": st.last_error,
                 "interval": req.app["cfg"].scan_interval, "config": req.app["cfg"].to_dict()})
    return web.Response(text=json.dumps(body, ensure_ascii=False, default=str), content_type="application/json")


async def _start(app: web.Application) -> None:
    app["task"] = asyncio.create_task(scan_loop(app))


async def _stop(app: web.Application) -> None:
    app["task"].cancel()


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8080, strategies: list[str] | None = None) -> None:
    app = web.Application()
    app["cfg"], app["state"], app["strategies"] = cfg, State(), strategies
    app.router.add_get("/", index)
    app.router.add_get("/api/result", api_result)
    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)
    print(f"Dashboard: http://{host}:{port}")
    web.run_app(app, host=host, port=port, print=None)
