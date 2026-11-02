"""Scan orchestrator: universe -> order books -> strategies -> ranked opportunities."""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .api import PolyClient
from .config import Config
from .models import Market, OrderBook
from .portfolio import PaperLedger, build_optimal_portfolio, kelly_fraction
from .strategies import ALL, binary_arb, cross_market_arb, endgame, ladder_arb, negrisk_arb, rewards_mm, stat_signal
from .strategies.base import Opportunity
from .strategies.negrisk_arb import EventGroup

log = logging.getLogger("polyscan.scanner")


@dataclass
class ScanResult:
    timestamp: str
    duration_s: float
    n_markets: int
    n_books: int
    n_negrisk_events: int
    n_requests: int
    opportunities: list[Opportunity]
    by_strategy: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    portfolio: dict = field(default_factory=dict)
    paper_mtm: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp, "duration_s": self.duration_s, "n_markets": self.n_markets,
            "n_books": self.n_books, "n_negrisk_events": self.n_negrisk_events,
            "n_requests": self.n_requests, "by_strategy": self.by_strategy, "errors": self.errors,
            "portfolio": self.portfolio, "paper_mtm": self.paper_mtm,
            "opportunities": [o.to_dict() for o in self.opportunities],
        }


async def _build_negrisk_groups(client: PolyClient, markets: list[Market], cfg: Config,
                                max_fetch: int | None = None) -> tuple[list[EventGroup], list[Market]]:
    by_ev: dict[str, list[Market]] = defaultdict(list)
    for m in markets:
        if m.neg_risk and m.event_id and m.is_binary:
            by_ev[m.event_id].append(m)

    # Rank candidate negRisk events by potential dislocation + 24h volume
    max_f = max_fetch if max_fetch is not None else getattr(cfg, "negrisk_max_fetch", 120)
    cands = []
    for ev_id, ms in by_ev.items():
        s_ask = sum(m.best_ask for m in ms if m.best_ask is not None)
        s_bid = sum(m.best_bid for m in ms if m.best_bid is not None)
        vol = sum(m.volume_24h for m in ms)
        score = min(abs(1.0 - s_ask), abs(1.0 - s_bid)) - min(0.2, vol / 1e7)
        cands.append((score, ev_id))
    cands.sort()
    cands = cands[:max_f]

    async def build(ev_id: str) -> EventGroup | None:
        res = await client.fetch_event_markets(ev_id)
        if not res:
            return None
        legs, raw = res
        if not legs or len(legs) > cfg.max_negrisk_legs:
            return None
        open_raw = [d for d in raw if d.get("active") and not d.get("closed")]
        tradable = [m for m in legs if m.accepting_orders]
        notes = []
        # an open leg that cannot be traded breaks the basket (unless it's an unnamed placeholder)
        untradable = [d for d in open_raw
                      if not (d.get("acceptingOrders") and d.get("enableOrderBook"))
                      and (d.get("groupItemTitle") or d.get("question"))
                      and not str(d.get("question", "")).lower().startswith(("placeholder", "person ", "candidate "))]
        complete = len(tradable) >= 2 and not untradable
        if untradable:
            notes.append(f"{len(untradable)} leg(s) ไม่เปิดเทรด")
        first = tradable[0] if tradable else legs[0]
        return EventGroup(
            event_id=ev_id, title=first.event_title, url=first.url, legs=tradable, complete=complete,
            augmented=first.neg_risk_augmented, has_other=any(m.neg_risk_other for m in tradable),
            n_closed=len(raw) - len(open_raw), notes=notes,
        )

    groups = [g for g in await asyncio.gather(*(build(e) for _, e in cands)) if g]
    extra = [m for g in groups for m in g.legs]
    return groups, extra


async def run_scan(cfg: Config, strategies: list[str] | None = None) -> ScanResult:
    t0 = time.time()
    enabled = set(strategies or ALL)
    errors: list[str] = []
    async with PolyClient(cfg) as client:
        markets = await client.fetch_markets(cfg.max_markets)
        log.info("fetched %d markets", len(markets))

        groups: list[EventGroup] = []
        if "negrisk_arb" in enabled:
            groups, extra = await _build_negrisk_groups(client, markets, cfg)
            known = {m.id for m in markets}
            markets_all = markets + [m for m in extra if m.id not in known]
        else:
            markets_all = markets

        tokens = [o.token_id for m in markets_all for o in m.outcomes]
        books: dict[str, OrderBook] = await client.fetch_books(tokens)
        log.info("fetched %d books for %d tokens", len(books), len(tokens))

        opps: list[Opportunity] = []
        sync_jobs = {
            "binary_arb": lambda: binary_arb.scan(markets_all, books, cfg),
            "negrisk_arb": lambda: negrisk_arb.scan(groups, books, cfg),
            "ladder_arb": lambda: ladder_arb.scan(markets_all, books, cfg),
            "cross_market_arb": lambda: cross_market_arb.scan(markets_all, books, cfg),
            "endgame_yield": lambda: endgame.scan(markets_all, books, cfg),
            "rewards_mm": lambda: rewards_mm.scan(markets_all, books, cfg),
        }
        by_strategy: dict[str, int] = {}
        for name, job in sync_jobs.items():
            if name not in enabled:
                continue
            try:
                found = job()
                by_strategy[name] = len(found)
                opps.extend(found)
            except Exception as e:  # keep scanning even if one strategy breaks
                errors.append(f"{name}: {e!r}")
                log.error("strategy %s failed:\n%s", name, traceback.format_exc())
        if "stat_signal" in enabled:
            try:
                found = await stat_signal.scan(markets_all, books, cfg, client)
                by_strategy["stat_signal"] = len(found)
                opps.extend(found)
            except Exception as e:
                errors.append(f"stat_signal: {e!r}")
                log.error("stat_signal failed:\n%s", traceback.format_exc())
        n_req = client.n_requests

    # de-duplicate (same legs), attach Kelly sizing metrics, and rank
    uniq: dict[str, Opportunity] = {}
    for o in opps:
        o.compute_score()
        kf = kelly_fraction(o, cfg.bankroll, cfg.max_capital_per_trade)
        o.metrics["kelly_pct"] = kf["kelly_pct"]
        o.metrics["kelly_usd"] = kf["recommended_usd"]
        if o.id not in uniq or uniq[o.id].score < o.score:
            uniq[o.id] = o
    ranked = sorted(uniq.values(), key=lambda o: o.score, reverse=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    portfolio = build_optimal_portfolio(ranked, cfg)
    ledger = PaperLedger(cfg.output_dir)
    ledger.record_allocation(portfolio, ranked, ts)
    paper_mtm = ledger.mark_to_market(books)
    return ScanResult(
        timestamp=ts,
        duration_s=round(time.time() - t0, 2),
        n_markets=len(markets_all), n_books=len(books), n_negrisk_events=len(groups),
        n_requests=n_req, opportunities=ranked, by_strategy=by_strategy, errors=errors,
        portfolio=portfolio, paper_mtm=paper_mtm,
    )
