"""Typed domain models built from Gamma (metadata) and CLOB (order book) payloads."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x) if x is not None and x != "" else default
    except (TypeError, ValueError):
        return default


def _jl(x: Any) -> list:
    if isinstance(x, list):
        return x
    if isinstance(x, str) and x:
        try:
            v = json.loads(x)
            return v if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _dt(s: Any) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Level:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[Level]  # best (highest) first
    asks: list[Level]  # best (lowest) first
    tick_size: float = 0.01
    min_order_size: float = 5.0
    neg_risk: bool = False
    last_trade: float | None = None

    @classmethod
    def from_clob(cls, d: dict) -> "OrderBook":
        bids = [Level(_f(l["price"]), _f(l["size"])) for l in d.get("bids") or [] if _f(l.get("size")) > 0]
        asks = [Level(_f(l["price"]), _f(l["size"])) for l in d.get("asks") or [] if _f(l.get("size")) > 0]
        bids.sort(key=lambda l: -l.price)
        asks.sort(key=lambda l: l.price)
        return cls(
            token_id=str(d.get("asset_id")),
            bids=bids,
            asks=asks,
            tick_size=_f(d.get("tick_size"), 0.01),
            min_order_size=_f(d.get("min_order_size"), 5.0),
            neg_risk=bool(d.get("neg_risk")),
            last_trade=_f(d.get("last_trade_price"), None) if d.get("last_trade_price") else None,
        )

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def depth_usd(self, side: str, within: float) -> float:
        """USDC notional resting within `within` of the touch."""
        lv = self.bids if side == "bid" else self.asks
        if not lv:
            return 0.0
        top = lv[0].price
        return sum(l.price * l.size for l in lv if abs(l.price - top) <= within + 1e-12)

    def imbalance(self, within: float = 0.03) -> float | None:
        """(bid depth - ask depth)/(sum) near the touch, in [-1, 1]."""
        m = self.mid
        if m is None:
            return None
        b = sum(l.size for l in self.bids if m - l.price <= within)
        a = sum(l.size for l in self.asks if l.price - m <= within)
        return (b - a) / (a + b) if a + b > 0 else None


@dataclass
class Outcome:
    name: str
    token_id: str
    gamma_price: float


@dataclass
class Market:
    id: str
    condition_id: str
    question: str
    slug: str
    event_id: str
    event_slug: str
    event_title: str
    end_date: datetime | None
    outcomes: list[Outcome]
    neg_risk: bool
    neg_risk_other: bool
    neg_risk_augmented: bool
    fee_rate: float
    volume_24h: float
    liquidity: float
    best_bid: float | None
    best_ask: float | None
    one_hour_change: float
    one_day_change: float
    rewards_daily: float
    rewards_min_size: float
    rewards_max_spread: float  # in cents, as in Gamma
    tick: float
    min_size: float
    accepting_orders: bool
    group_item_title: str
    group_item_threshold: str
    uma_statuses: list = field(default_factory=list)
    active: bool = True
    closed: bool = False

    @classmethod
    def from_gamma(cls, d: dict, event: dict | None = None) -> "Market | None":
        names = _jl(d.get("outcomes"))
        tokens = _jl(d.get("clobTokenIds"))
        prices = _jl(d.get("outcomePrices"))
        if len(tokens) < 2 or len(names) != len(tokens):
            return None
        ev = event or ((d.get("events") or [{}])[0] if d.get("events") else {})
        fs = d.get("feeSchedule") or {}
        fee_rate = _f(fs.get("rate"), 0.0) if d.get("feesEnabled", True) else 0.0
        rewards = d.get("clobRewards") or []
        if isinstance(rewards, str):
            rewards = _jl(rewards)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = sum(
            _f(r.get("rewardsDailyRate"))
            for r in rewards
            if isinstance(r, dict)
            and (not r.get("startDate") or str(r.get("startDate"))[:10] <= today)
            and (not r.get("endDate") or str(r.get("endDate"))[:10] >= today)
        )
        return cls(
            id=str(d.get("id")),
            condition_id=d.get("conditionId", ""),
            question=d.get("question", ""),
            slug=d.get("slug", ""),
            event_id=str(ev.get("id", "")),
            event_slug=ev.get("slug", d.get("slug", "")),
            event_title=ev.get("title", d.get("question", "")),
            end_date=_dt(d.get("endDate")) or _dt(ev.get("endDate")),
            outcomes=[
                Outcome(str(n), str(t), _f(prices[i]) if i < len(prices) else 0.0)
                for i, (n, t) in enumerate(zip(names, tokens))
            ],
            neg_risk=bool(d.get("negRisk")),
            neg_risk_other=bool(d.get("negRiskOther")),
            neg_risk_augmented=bool(ev.get("negRiskAugmented")),
            fee_rate=fee_rate,
            volume_24h=_f(d.get("volume24hr")),
            liquidity=_f(d.get("liquidityNum") or d.get("liquidity")),
            best_bid=_f(d.get("bestBid"), None) if d.get("bestBid") is not None else None,
            best_ask=_f(d.get("bestAsk"), None) if d.get("bestAsk") is not None else None,
            one_hour_change=_f(d.get("oneHourPriceChange")),
            one_day_change=_f(d.get("oneDayPriceChange")),
            rewards_daily=daily,
            rewards_min_size=_f(d.get("rewardsMinSize")),
            rewards_max_spread=_f(d.get("rewardsMaxSpread")),
            tick=_f(d.get("orderPriceMinTickSize"), 0.01),
            min_size=_f(d.get("orderMinSize"), 5.0),
            accepting_orders=bool(d.get("acceptingOrders")) and bool(d.get("enableOrderBook")),
            group_item_title=d.get("groupItemTitle") or "",
            group_item_threshold=str(d.get("groupItemThreshold") or ""),
            uma_statuses=_jl(d.get("umaResolutionStatuses")),
            active=bool(d.get("active", True)),
            closed=bool(d.get("closed", False)),
        )

    @property
    def yes(self) -> Outcome:
        return self.outcomes[0]

    @property
    def no(self) -> Outcome:
        return self.outcomes[1]

    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2

    @property
    def days_to_end(self) -> float | None:
        if not self.end_date:
            return None
        return (self.end_date - datetime.now(timezone.utc)).total_seconds() / 86400

    @property
    def url(self) -> str:
        return f"https://polymarket.com/event/{self.event_slug}"

    @property
    def label(self) -> str:
        return self.group_item_title or self.question
