"""Opportunity model shared by all strategies."""
from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from typing import Any

# Opportunity kinds, ordered from most to least certain
ARB = "ARB"          # locked-in profit, executable purely on the CLOB
ARB_ONCHAIN = "ARB*"  # locked-in, but needs an on-chain split / convert step
YIELD = "YIELD"      # high-probability carry trade (tail risk)
MM = "MM"            # liquidity-reward market making
SIGNAL = "SIGNAL"    # statistical edge, directional risk

# Ranking weight per kind: locked profit > on-chain locked > rewards > carry > signals
KIND_WEIGHT = {ARB: 1.0, ARB_ONCHAIN: 0.8, MM: 0.5, YIELD: 0.3, SIGNAL: 0.2}


@dataclass
class Leg:
    token_id: str
    outcome: str
    market_question: str
    side: str            # BUY / SELL
    price_limit: float   # worst price to accept (use as FOK limit)
    avg_price: float
    size: float          # shares
    neg_risk: bool = False
    tick_size: float = 0.01


@dataclass
class Opportunity:
    strategy: str
    kind: str
    title: str
    event_title: str
    url: str
    legs: list[Leg]
    capital: float              # USDC required
    exp_profit: float           # expected / locked $ profit
    roi: float                  # exp_profit / capital
    days: float | None          # days until capital is released (None = unknown)
    confidence: float           # 0..1 (1 = mathematically locked)
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    score: float = 0.0

    def __post_init__(self) -> None:
        if not self.id:
            key = self.strategy + "|" + "|".join(f"{l.token_id}:{l.side}" for l in self.legs)
            self.id = hashlib.sha1(key.encode()).hexdigest()[:10]

    @property
    def apr(self) -> float | None:
        if self.days is None:
            return None
        d = max(self.days, 1 / 24)
        return self.roi * 365 / d

    def compute_score(self) -> float:
        """Risk-adjusted, capital-time-efficient score.

        score = confidence * profit * time_efficiency, with time_efficiency = 1/sqrt(1+days)
        so that instant arbs rank above equal-$ arbs that lock capital for months.
        """
        d = self.days if self.days is not None else 30.0
        d = max(d, 0.0)
        w = KIND_WEIGHT.get(self.kind, 0.5)
        self.score = w * self.confidence * self.exp_profit / math.sqrt(1.0 + d)
        return self.score

    def to_dict(self) -> dict:
        d = asdict(self)
        d["apr"] = self.apr
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Opportunity":
        d = dict(d)
        d.pop("apr", None)
        d["legs"] = [Leg(**l) for l in d.get("legs", [])]
        return cls(**d)
