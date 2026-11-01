"""Central configuration. Override via config.json in the project root or CLI flags."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


@dataclass
class Config:
    # ---------- universe ----------
    max_markets: int = 1500            # top-N active markets by 24h volume to scan
    min_volume_24h: float = 0.0        # filter for directional / MM strategies
    min_liquidity: float = 500.0
    max_negrisk_legs: int = 60         # skip negRisk events with more legs than this

    # ---------- risk / sizing ----------
    bankroll: float = 5000.0           # total capital (USDC) used for sizing suggestions
    max_capital_per_trade: float = 1000.0
    min_profit_usd: float = 0.50       # discard opportunities with lower expected $ profit
    slippage_buffer: float = 0.0       # extra $ per share safety margin on arb legs

    # ---------- arbitrage ----------
    min_arb_edge: float = 0.002        # min net edge per $1 payout (after fees) on the marginal share
    enable_hybrid_arb: bool = True     # scan Maker+Taker (0-fee on resting leg) arbs when Taker+Taker is blocked by fees
    negrisk_max_fetch: int = 120       # max candidate negRisk events to fetch full leg lists for

    # ---------- endgame / resolution yield ----------
    endgame_min_price: float = 0.90
    endgame_max_price: float = 0.995
    endgame_max_days: float = 21.0
    endgame_min_apr: float = 0.25
    endgame_price_window: float = 0.005   # walk ask book up to best_ask + window
    endgame_max_per_event: int = 2        # diversify: max yield legs from the same event

    # ---------- liquidity-reward market making ----------
    mm_capital: float = 1000.0         # reference capital per market for reward estimate
    mm_min_daily_roi: float = 0.002    # 0.2%/day
    mm_min_days_to_end: float = 3.0
    mm_max_spread: float = 0.12        # skip dead/uncalibrated books with spread > 12c
    mm_max_per_event: int = 2          # max MM markets per event

    # ---------- statistical signals ----------
    signal_top_k: int = 50             # how many movers to pull price history for
    signal_min_z: float = 2.0

    # ---------- infra ----------
    concurrency: int = 8
    request_timeout: float = 30.0
    scan_interval: int = 60
    output_dir: str = str(ROOT / "output")

    # ---------- alerts (optional, env vars take precedence) ----------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook: str = ""
    alert_min_score: float = 0.0
    alert_kinds: tuple = ("ARB",)

    @classmethod
    def load(cls, path: str | os.PathLike | None = None, **overrides) -> "Config":
        cfg = cls()
        p = Path(path) if path else ROOT / "config.json"
        if p.exists():
            data = json.loads(p.read_text())
            for f in fields(cls):
                if f.name in data:
                    val = data[f.name]
                    setattr(cfg, f.name, tuple(val) if isinstance(getattr(cfg, f.name), tuple) else val)
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        cfg.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", cfg.telegram_bot_token)
        cfg.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", cfg.telegram_chat_id)
        cfg.discord_webhook = os.getenv("DISCORD_WEBHOOK", cfg.discord_webhook)
        return cfg

    def to_dict(self) -> dict:
        d = asdict(self)
        for secret in ("telegram_bot_token", "discord_webhook"):
            if d.get(secret):
                d[secret] = "***"
        return d
