"""Optional push alerts (Telegram / Discord). Configure via env vars:
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DISCORD_WEBHOOK
"""
from __future__ import annotations

import logging

import aiohttp

from .config import Config
from .strategies.base import Opportunity

log = logging.getLogger("polyscan.alerts")
_sent: set[str] = set()


def _fmt(o: Opportunity) -> str:
    return (f"[{o.kind}] {o.strategy} | {o.event_title}\n{o.title}\n"
            f"capital ${o.capital:,.0f} → profit ${o.exp_profit:,.2f} (ROI {o.roi:.2%}, conf {o.confidence:.2f})\n{o.url}")


async def send_alerts(opps: list[Opportunity], cfg: Config) -> int:
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id) and not cfg.discord_webhook:
        return 0
    new = [o for o in opps if o.kind in cfg.alert_kinds and o.score >= cfg.alert_min_score and o.id not in _sent]
    if not new:
        return 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        for o in new[:10]:
            msg = _fmt(o)
            try:
                if cfg.telegram_bot_token and cfg.telegram_chat_id:
                    await s.post(f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
                                 json={"chat_id": cfg.telegram_chat_id, "text": msg, "disable_web_page_preview": True})
                if cfg.discord_webhook:
                    await s.post(cfg.discord_webhook, json={"content": msg[:1900]})
                _sent.add(o.id)
            except aiohttp.ClientError as e:
                log.warning("alert failed: %r", e)
    return len(new)
