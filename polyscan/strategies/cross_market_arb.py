"""Strategy 7 — Cross-Event Temporal Monotonicity, Duplicate Mirror & Range-Threshold Synthesis Arbitrage.

Captures three structural cross-market inefficiencies across the Polymarket CLOB:
  1. Cross-Event Temporal / Deadline Monotonicity Arbitrage:
     Matches binary Yes/No propositions across all events whose normalized question stems
     differ only by deadline (<DATE>) with |days_a - days_b| >= 5.
       * "by / before <DATE>"       : earlier deadline (strong) => later deadline (weak)
       * "through / remain <DATE>"  : later deadline (strong)   => earlier deadline (weak)
     Trade: BUY YES(weak) + BUY NO(strong), guaranteed minimum payout $1.00 (up to $2.00 if weak & ~strong).

  2. Cross-Event Duplicate / Mirror Market Arbitrage:
     Detects identical normalized binary propositions listed across two distinct markets/events
     with resolution timestamps within 24 hours.
     Trade: BUY YES(M_1) + BUY NO(M_2) (or vice versa), guaranteed payout $1.00 in all states.

  3. Range-Partition vs. Threshold Inclusion Arbitrage:
     Within an event, links range buckets [X, Y] with cumulative threshold markets (>= X or <= Y):
       * Range[X, Y] => Threshold(>= X) and Range[X, Y] => Threshold(<= Y).
         Trade: BUY YES(Threshold) + BUY NO(Range), guaranteed payout >= $1.00.
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..config import Config
from ..execution_math import basket_walk
from ..models import Market, OrderBook
from .base import ARB, Leg, Opportunity

NAME = "cross_market_arb"

_MONTH_NAMES = (
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_DATE_PHRASE_RE = re.compile(
    rf"\b(?:(?:by|before|on or before|in or before|through|until|in|on|of|end of)\s+)?"
    rf"(?:"
    rf"(?:{_MONTH_NAMES})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*,?\s*\d{{4}})?|"
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTH_NAMES})\.?(?:\s*,?\s*\d{{4}})?|"
    rf"(?:{_MONTH_NAMES})\.?\s+\d{{4}}|"
    rf"(?:q[1-4])\s+\d{{4}}|"
    rf"(?:202[4-9]|203\d)"
    rf")\b",
    re.I,
)
BY_KW = re.compile(r"\b(by|before|on or before|in or before|prior to)\b", re.I)
THROUGH_KW = re.compile(r"\b(through|until|remain|remains|continue|continues|still|stay|stays)\b", re.I)
UP_KW = re.compile(
    r"\b(reach|reaches|hit|hits|above|exceed|exceeds|greater than|more than|at least|higher than|or more|or higher|surpass|top|over)\b|↑|>=",
    re.I,
)
DOWN_KW = re.compile(
    r"\b(dip|dips|below|under|less than|fall to|falls to|drop to|drops to|lower than|or less|or lower)\b|↓|<=",
    re.I,
)
RANGE_RE = re.compile(
    r"(?:between\s+\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmMbB])?\s+and\s+\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmMbB])?)"
    r"|(?:\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmMbB])?\s*(?:to|-|–|—)\s*\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmMbB])?)",
    re.I,
)
NUM_RE = re.compile(r"\$?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kKmMbB])?\b")


def _num(tok: str, suf: str | None) -> float:
    v = float(tok.replace(",", ""))
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get((suf or "").lower(), 1.0)
    return v * mult


def _is_yes_no(m: Market) -> bool:
    if not m.is_binary or not m.accepting_orders:
        return False
    return m.outcomes[0].name.strip().lower() == "yes" and m.outcomes[1].name.strip().lower() == "no"


def _norm_question(q: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s$%<>=-]", "", q.lower())).strip()


def _temporal_stem(m: Market) -> tuple[str, int] | None:
    """Return (stem_with_DATE, sign) where sign=+1 means P(YES) increases with end_date."""
    if not _is_yes_no(m) or not m.end_date:
        return None
    q = m.question.strip()
    matches = list(_DATE_PHRASE_RE.finditer(q))
    if len(matches) != 1:
        return None
    stem = (q[: matches[0].start()] + " <DATE> " + q[matches[0].end() :]).lower()
    stem = re.sub(r"\s+", " ", re.sub(r"[?.,!]", "", stem)).strip()
    if len(stem.split()) < 4:
        return None
    if THROUGH_KW.search(q):
        return stem, -1
    if BY_KW.search(q):
        return stem, +1
    return None


def _emit_pair_arb(
    weak: Market,
    strong: Market,
    books: dict[str, OrderBook],
    cfg: Config,
    *,
    sub_type: str,
    logic_th: str,
    logic_en: str,
    double_payout: bool = True,
) -> Opportunity | None:
    bs_no = books.get(strong.no.token_id)
    bw_yes = books.get(weak.yes.token_id)
    if not bs_no or not bw_yes or not bs_no.asks or not bw_yes.asks:
        return None
    days = max([d for d in (strong.days_to_end, weak.days_to_end) if d is not None], default=None)
    min_sh = max(bs_no.min_order_size, bw_yes.min_order_size)

    # (a) Pure Taker-Taker
    if bs_no.asks[0].price + bw_yes.asks[0].price < 1.0:
        f = basket_walk(
            [bw_yes.asks, bs_no.asks], [weak.fee_rate, strong.fee_rate],
            side="buy", set_value=1.0, min_edge=cfg.min_arb_edge,
            max_capital=cfg.max_capital_per_trade, slippage=cfg.slippage_buffer,
        )
        if f.sets >= min_sh and f.profit >= cfg.min_profit_usd:
            return Opportunity(
                strategy=NAME, kind=ARB,
                title=f"[{sub_type}] YES[{weak.label[:28]}] + NO[{strong.label[:28]}] = {sum(f.avg_prices):.3f}",
                event_title=f"{weak.event_title[:40]} ↔ {strong.event_title[:40]}" if weak.event_id != strong.event_id else weak.event_title,
                url=weak.url,
                legs=[
                    Leg(weak.yes.token_id, f"{weak.label} [Yes]", weak.question, "BUY",
                        f.worst_prices[0], f.avg_prices[0], f.sets, weak.neg_risk, bw_yes.tick_size),
                    Leg(strong.no.token_id, f"{strong.label} [No]", strong.question, "BUY",
                        f.worst_prices[1], f.avg_prices[1], f.sets, strong.neg_risk, bs_no.tick_size),
                ],
                capital=f.capital, exp_profit=f.profit, roi=f.roi, days=days, confidence=0.86,
                notes=[logic_th, f"หักค่าธรรมเนียม Taker ทั้งสองขาแล้ว (${f.fees:.2f}) กำไรขั้นต่ำล็อกทันที"]
                + (["โอกาสรับ $2.00 ต่อชุดหากผลตกในช่วงกลาง (Free Convexity)"] if double_payout else []),
                notes_en=[logic_en, f"Net of two-sided Taker fees (${f.fees:.2f}), minimum profit is locked"]
                + (["Pays $2.00 per set if outcome lands between the two bounds (free convexity)"] if double_payout else []),
                metrics={"sets": f.sets, "sub_type": sub_type, "edge_top": f.first_edge,
                         "payout": 1.0, "fee_rates": [weak.fee_rate, strong.fee_rate], "mode": "taker"},
            )

    # (b) Hybrid Maker-Taker
    if not getattr(cfg, "enable_hybrid_arb", True) or not bs_no.bids or not bw_yes.bids:
        return None
    f1 = basket_walk([bw_yes.bids[:1], bs_no.asks], [0.0, strong.fee_rate], side="buy", set_value=1.0,
                     min_edge=cfg.min_arb_edge, max_capital=cfg.max_capital_per_trade)
    f2 = basket_walk([bw_yes.asks, bs_no.bids[:1]], [weak.fee_rate, 0.0], side="buy", set_value=1.0,
                     min_edge=cfg.min_arb_edge, max_capital=cfg.max_capital_per_trade)
    f, maker_leg = (f1, 0) if f1.profit >= f2.profit else (f2, 1)
    if f.sets >= min_sh and f.profit >= cfg.min_profit_usd:
        m_lbl = f"{weak.label[:25]} YES" if maker_leg == 0 else f"{strong.label[:25]} NO"
        t_lbl = f"{strong.label[:25]} NO" if maker_leg == 0 else f"{weak.label[:25]} YES"
        return Opportunity(
            strategy=NAME, kind=ARB,
            title=f"HYBRID [{sub_type}] YES[{weak.label[:24]}] + NO[{strong.label[:24]}] = {sum(f.avg_prices):.3f}",
            event_title=f"{weak.event_title[:40]} ↔ {strong.event_title[:40]}" if weak.event_id != strong.event_id else weak.event_title,
            url=weak.url,
            legs=[
                Leg(weak.yes.token_id, f"{weak.label} [Yes]" + (" (Maker)" if maker_leg == 0 else " (Taker)"),
                    weak.question, "BUY", f.worst_prices[0], f.avg_prices[0], f.sets, weak.neg_risk, bw_yes.tick_size),
                Leg(strong.no.token_id, f"{strong.label} [No]" + (" (Maker)" if maker_leg == 1 else " (Taker)"),
                    strong.question, "BUY", f.worst_prices[1], f.avg_prices[1], f.sets, strong.neg_risk, bs_no.tick_size),
            ],
            capital=f.capital, exp_profit=f.profit, roi=f.roi, days=days, confidence=0.78,
            notes=[logic_th, f"วาง GTC Maker Bid ที่ '{m_lbl}' (0% fee) เมื่อแมตช์แล้วส่ง FOK Taker ที่ '{t_lbl}'"],
            notes_en=[logic_en, f"Post GTC Maker Bid on '{m_lbl}' (0% fee), then sweep '{t_lbl}' via FOK Taker upon fill"],
            metrics={"sets": f.sets, "sub_type": sub_type, "edge_top": f.first_edge,
                     "payout": 1.0, "fee_rates": [0.0, strong.fee_rate] if maker_leg == 0 else [weak.fee_rate, 0.0],
                     "mode": "hybrid_maker_taker", "maker_leg": maker_leg},
        )
    return None


def scan(markets: list[Market], books: dict[str, OrderBook], cfg: Config) -> list[Opportunity]:
    opps: list[Opportunity] = []

    # 1. Cross-Event Temporal / Deadline Monotonicity
    by_stem: dict[tuple[str, int], list[Market]] = defaultdict(list)
    for m in markets:
        ts = _temporal_stem(m)
        if ts:
            by_stem[ts].append(m)

    for (stem, sign), ms in by_stem.items():
        if len(ms) < 2:
            continue
        ms.sort(key=lambda x: x.end_date.timestamp())
        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                a, b = ms[i], ms[j]
                if a.event_id == b.event_id:
                    continue  # intra-event ladders are already covered by ladder_arb
                dt_days = (b.end_date - a.end_date).total_seconds() / 86400.0
                if dt_days < 5.0:
                    continue
                strong, weak = (a, b) if sign > 0 else (b, a)
                o = _emit_pair_arb(
                    weak, strong, books, cfg, sub_type="CROSS-DATE",
                    logic_th=f"ตรรกะข้ามอีเวนต์: '{strong.question[:45]}' ⇒ '{weak.question[:45]}'",
                    logic_en=f"Cross-event deadline implication: '{strong.question[:45]}' ⇒ '{weak.question[:45]}'",
                )
                if o:
                    opps.append(o)

    # 2. Cross-Event Duplicate / Mirror Market Arbitrage
    by_q: dict[str, list[Market]] = defaultdict(list)
    for m in markets:
        if _is_yes_no(m) and m.end_date:
            by_q[_norm_question(m.question)].append(m)

    for _, ms in by_q.items():
        if len(ms) < 2:
            continue
        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                a, b = ms[i], ms[j]
                if a.id == b.id or abs((a.end_date - b.end_date).total_seconds()) > 86400:
                    continue
                for w, s in ((a, b), (b, a)):
                    o = _emit_pair_arb(
                        w, s, books, cfg, sub_type="MIRROR",
                        logic_th=f"ตลาดคู่แฝดข้ามอีเวนต์: คำถามและวันหมดอายุตรงกัน ซื้อ YES ฝั่งถูก + NO อีกตลาด",
                        logic_en=f"Cross-event mirror market: identical question & expiry; buy cheap YES + opposite NO",
                        double_payout=False,
                    )
                    if o:
                        opps.append(o)

    # 3. Intra-Event Range [X, Y] vs Cumulative Threshold (>= X or <= Y) Inclusion
    by_ev: dict[str, list[Market]] = defaultdict(list)
    for m in markets:
        if _is_yes_no(m):
            by_ev[m.event_id].append(m)

    for _, ms in by_ev.items():
        if len(ms) < 2:
            continue
        ranges: list[tuple[float, float, Market]] = []
        up_thresh: list[tuple[float, Market]] = []
        down_thresh: list[tuple[float, Market]] = []
        for m in ms:
            txt = f"{m.group_item_title} {m.question}"
            rm = RANGE_RE.search(txt)
            if rm:
                g = rm.groups()
                low = _num(g[0], g[1]) if g[0] else _num(g[4], g[5])
                high = _num(g[2], g[3]) if g[2] else _num(g[6], g[7])
                if low < high:
                    ranges.append((low, high, m))
                continue
            nums = list(NUM_RE.finditer(m.group_item_title or ""))
            if len(nums) == 1:
                val = _num(nums[0].group(1), nums[0].group(2))
                if UP_KW.search(txt) and not DOWN_KW.search(txt):
                    up_thresh.append((val, m))
                elif DOWN_KW.search(txt) and not UP_KW.search(txt):
                    down_thresh.append((val, m))

        for low, high, rm_mkt in ranges:
            for k, tm_mkt in up_thresh:
                if low >= k:  # [low, high] implies >= k
                    o = _emit_pair_arb(
                        tm_mkt, rm_mkt, books, cfg, sub_type="RANGE⊆THRESH",
                        logic_th=f"ตรรกะเซตย่อย: ช่วง '{rm_mkt.label}' ⊂ เกณฑ์ '{tm_mkt.label}'",
                        logic_en=f"Subset inclusion: range '{rm_mkt.label}' ⊂ threshold '{tm_mkt.label}'",
                    )
                    if o:
                        opps.append(o)
            for k, tm_mkt in down_thresh:
                if high <= k:  # [low, high] implies <= k
                    o = _emit_pair_arb(
                        tm_mkt, rm_mkt, books, cfg, sub_type="RANGE⊆THRESH",
                        logic_th=f"ตรรกะเซตย่อย: ช่วง '{rm_mkt.label}' ⊂ เกณฑ์ '{tm_mkt.label}'",
                        logic_en=f"Subset inclusion: range '{rm_mkt.label}' ⊂ threshold '{tm_mkt.label}'",
                    )
                    if o:
                        opps.append(o)

    return opps
