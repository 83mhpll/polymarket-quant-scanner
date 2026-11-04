"""Strategy 7 — Cross-Event Temporal Monotonicity, Duplicate Mirror & Range-Threshold Synthesis Arbitrage.

Captures three structural cross-market inefficiencies across the Polymarket CLOB with strict logic verification:
  1. Cross-Event Temporal / Deadline Monotonicity Arbitrage:
     Matches binary Yes/No propositions across distinct events whose normalized question stems
     are 100% identical except for an explicit deadline clause ("by <DATE>" or "through <DATE>").
  2. Cross-Event Duplicate / Mirror Market Arbitrage:
     Detects identical full-sentence binary propositions (>= 6 words) listed across two distinct markets
     with resolution timestamps within 24 hours.
  3. Intra-Event Range [X, Y] vs Cumulative Threshold (>= X or <= Y) Inclusion:
     Links non-negRisk range markets [X, Y] with cumulative threshold markets sharing the exact same subject template.
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..config import Config
from ..execution_math import basket_walk
from ..models import Market, OrderBook
from .base import ARB, Leg, Opportunity
from .ladder_arb import BUCKET_TRAP_KW, parse_explicit_date

NAME = "cross_market_arb"

_MONTH_NAMES = (
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
# Require explicit deadline/persistence preposition immediately preceding the date
_DEADLINE_CLAUSE_RE = re.compile(
    rf"\b(by|before|on or before|in or before|through|until)\s+"
    rf"((?:{_MONTH_NAMES})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*,?\s*20\d{{2}})?|"
    rf"(?:{_MONTH_NAMES})\.?\s+20\d{{2}})\b",
    re.I,
)
UP_KW = re.compile(
    r"\b(reach|reaches|hit|hits|above|exceed|exceeds|greater than|more than|at least|higher than|or more|or higher|surpass|top)\b|↑|>=",
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
GENERIC_SUBMARKET_RE = re.compile(
    r"\b(any other score|exact score|both teams to score|over/under|spread|draw|halftime|fulltime)\b",
    re.I,
)


def _num(tok: str, suf: str | None) -> float:
    v = float(tok.replace(",", ""))
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get((suf or "").lower(), 1.0)
    return v * mult


def _is_yes_no(m: Market) -> bool:
    if not m.is_binary or not m.accepting_orders or m.neg_risk:
        return False
    if m.days_to_end is not None and m.days_to_end < 0:
        return False
    return m.outcomes[0].name.strip().lower() == "yes" and m.outcomes[1].name.strip().lower() == "no"


def _norm_question(q: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s$%<>=-]", "", q.lower())).strip()


def _temporal_stem(m: Market) -> tuple[str, int, float] | None:
    """Return (stem_with_DATE, sign, parsed_ts) where sign=+1 means P(YES) increases with deadline."""
    if not _is_yes_no(m):
        return None
    q = m.question.strip()
    if BUCKET_TRAP_KW.search(q) or GENERIC_SUBMARKET_RE.search(q):
        return None
    matches = list(_DEADLINE_CLAUSE_RE.finditer(q))
    if len(matches) != 1:
        return None
    prep = matches[0].group(1).lower()
    date_str = matches[0].group(2)
    parsed_ts = parse_explicit_date(date_str, m.end_date)
    if parsed_ts is None:
        return None
    stem = (q[: matches[0].start()] + f" {prep} <DATE> " + q[matches[0].end() :]).lower()
    stem = re.sub(r"\s+", " ", re.sub(r"[?.,!]", "", stem)).strip()
    if len(stem.split()) < 5:
        return None
    sign = -1 if prep in ("through", "until") else +1
    return stem, sign, parsed_ts


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
    # Guard against >25% inversion gaps which indicate distinct resolution rules
    inv_gap = 1.0 - (bs_no.asks[0].price + bw_yes.asks[0].price)
    if inv_gap > 0.25:
        return None

    days = max([d for d in (strong.days_to_end, weak.days_to_end) if d is not None], default=None)
    min_sh = max(bs_no.min_order_size, bw_yes.min_order_size)

    # (a) Pure Offer-Side Taker-Taker
    if bs_no.asks[0].price + bw_yes.asks[0].price < 1.0:
        f = basket_walk(
            [bw_yes.asks, bs_no.asks], [weak.fee_rate, strong.fee_rate],
            side="buy", set_value=1.0, min_edge=cfg.min_arb_edge,
            max_capital=cfg.max_capital_per_trade, slippage=cfg.slippage_buffer,
        )
        if f.sets >= min_sh and f.profit >= cfg.min_profit_usd:
            return Opportunity(
                strategy=NAME, kind=ARB,
                title=f"[{sub_type}] YES[{weak.label[:28]}] + NO[{strong.label[:28]}] = {sum(f.avg_prices):.3f} (Offer Book)",
                event_title=f"{weak.event_title[:40]} ↔ {strong.event_title[:40]}" if weak.event_id != strong.event_id else weak.event_title,
                url=weak.url,
                legs=[
                    Leg(weak.yes.token_id, f"{weak.label} [Yes]", weak.question, "BUY",
                        f.worst_prices[0], f.avg_prices[0], f.sets, weak.neg_risk, bw_yes.tick_size,
                        order_type="FOK", best_bid=bw_yes.best_bid, best_ask=bw_yes.best_ask, spread=bw_yes.spread),
                    Leg(strong.no.token_id, f"{strong.label} [No]", strong.question, "BUY",
                        f.worst_prices[1], f.avg_prices[1], f.sets, strong.neg_risk, bs_no.tick_size,
                        order_type="FOK", best_bid=bs_no.best_bid, best_ask=bs_no.best_ask, spread=bs_no.spread),
                ],
                capital=f.capital, exp_profit=f.profit, roi=f.roi, days=days, confidence=0.88,
                notes=[logic_th, f"คำนวณจากฝั่ง Offer (Ask Book) จริงทั้ง 2 ขา หักค่าธรรมเนียม (${f.fees:.2f}) แล้วล็อกกำไรทันที"]
                + (["โอกาสรับ $2.00 ต่อชุดหากผลตกในช่วงกลาง (โบนัสกำไร 2 เด้ง)"] if double_payout else []),
                notes_en=[logic_en, f"Calculated 100% from real Offer (Ask) order book depth net of fees (${f.fees:.2f})"]
                + (["Pays $2.00 per set if outcome lands between the two bounds (2x bonus)"] if double_payout else []),
                metrics={"sets": f.sets, "sub_type": sub_type, "edge_top": f.first_edge,
                         "payout": 1.0, "fee_rates": [weak.fee_rate, strong.fee_rate], "mode": "taker",
                         "strong_label": strong.label, "weak_label": weak.label},
            )

    # (b) Hybrid Maker-Taker
    if not getattr(cfg, "enable_hybrid_arb", True) or not bs_no.bids or not bw_yes.bids:
        return None
    if (bs_no.spread or 1.0) > 0.08 or (bw_yes.spread or 1.0) > 0.08:
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
                Leg(weak.yes.token_id, f"{weak.label} [Yes]" + (" (Maker Bid)" if maker_leg == 0 else " (Offer Taker)"),
                    weak.question, "BUY", f.worst_prices[0], f.avg_prices[0], f.sets, weak.neg_risk, bw_yes.tick_size,
                    order_type="GTC" if maker_leg == 0 else "FOK", best_bid=bw_yes.best_bid, best_ask=bw_yes.best_ask, spread=bw_yes.spread),
                Leg(strong.no.token_id, f"{strong.label} [No]" + (" (Maker Bid)" if maker_leg == 1 else " (Offer Taker)"),
                    strong.question, "BUY", f.worst_prices[1], f.avg_prices[1], f.sets, strong.neg_risk, bs_no.tick_size,
                    order_type="GTC" if maker_leg == 1 else "FOK", best_bid=bs_no.best_bid, best_ask=bs_no.best_ask, spread=bs_no.spread),
            ],
            capital=f.capital, exp_profit=f.profit, roi=f.roi, days=days, confidence=0.78,
            notes=[logic_th, f"โหมด Hybrid: ตั้งรับ GTC Maker Bid ที่ '{m_lbl}' (0% fee) เมื่อแมตช์แล้วเคาะฝั่ง Offer ที่ '{t_lbl}'"],
            notes_en=[logic_en, f"Hybrid Mode: post GTC Maker Bid on '{m_lbl}' (0% fee), then sweep Offer on '{t_lbl}' upon fill"],
            metrics={"sets": f.sets, "sub_type": sub_type, "edge_top": f.first_edge,
                     "payout": 1.0, "fee_rates": [0.0, strong.fee_rate] if maker_leg == 0 else [weak.fee_rate, 0.0],
                     "mode": "hybrid_maker_taker", "maker_leg": maker_leg,
                     "strong_label": strong.label, "weak_label": weak.label},
        )
    return None


def scan(markets: list[Market], books: dict[str, OrderBook], cfg: Config) -> list[Opportunity]:
    opps: list[Opportunity] = []

    # 1. Cross-Event Temporal / Deadline Monotonicity
    by_stem: dict[tuple[str, int], list[tuple[float, Market]]] = defaultdict(list)
    for m in markets:
        ts = _temporal_stem(m)
        if ts:
            by_stem[(ts[0], ts[1])].append((ts[2], m))

    for (stem, sign), items in by_stem.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: x[0])
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                ta, a = items[i]
                tb, b = items[j]
                if a.event_id == b.event_id:
                    continue
                dt_days = (tb - ta) / 86400.0
                if dt_days < 5.0:
                    continue
                strong, weak = (a, b) if sign > 0 else (b, a)
                o = _emit_pair_arb(
                    weak, strong, books, cfg, sub_type="CROSS-DATE",
                    logic_th=f"พิสูจน์ตรรกะข้ามอีเวนต์: '{strong.question[:48]}' (เกิดยากกว่า) ⇒ '{weak.question[:48]}' (เกิดง่ายกว่า)",
                    logic_en=f"Cross-event deadline proof: '{strong.question[:48]}' (harder) ⇒ '{weak.question[:48]}' (easier)",
                )
                if o:
                    opps.append(o)

    # 2. Cross-Event Duplicate / Mirror Market Arbitrage (Full self-contained questions >= 6 words only)
    by_q: dict[str, list[Market]] = defaultdict(list)
    for m in markets:
        if not _is_yes_no(m) or not m.end_date or GENERIC_SUBMARKET_RE.search(m.question):
            continue
        nq = _norm_question(m.question)
        if len(nq.split()) < 6:
            continue
        by_q[nq].append(m)

    for _, ms in by_q.items():
        if len(ms) < 2:
            continue
        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                a, b = ms[i], ms[j]
                if a.id == b.id or a.event_id == b.event_id or abs((a.end_date - b.end_date).total_seconds()) > 86400:
                    continue
                for w, s in ((a, b), (b, a)):
                    o = _emit_pair_arb(
                        w, s, books, cfg, sub_type="MIRROR",
                        logic_th="ตลาดคู่แฝดข้ามอีเวนต์: คำถามและวันหมดอายุตรงกัน ซื้อ YES ฝั่งถูก + NO อีกตลาด",
                        logic_en="Cross-event mirror market: identical question & expiry; buy cheap YES + opposite NO",
                        double_payout=False,
                    )
                    if o:
                        opps.append(o)

    # 3. Intra-Event Range [X, Y] vs Cumulative Threshold (>= X or <= Y) Inclusion (Non-negRisk, identical subject stem)
    by_ev: dict[str, list[Market]] = defaultdict(list)
    for m in markets:
        if _is_yes_no(m):
            by_ev[m.event_id].append(m)

    for _, ms in by_ev.items():
        if len(ms) < 2:
            continue
        ranges: list[tuple[str, float, float, Market]] = []
        up_thresh: list[tuple[str, float, Market]] = []
        down_thresh: list[tuple[str, float, Market]] = []
        for m in ms:
            q = m.question.strip()
            rm = RANGE_RE.search(q)
            if rm:
                g = rm.groups()
                low = _num(g[0], g[1]) if g[0] else _num(g[4], g[5])
                high = _num(g[2], g[3]) if g[2] else _num(g[6], g[7])
                stem = _norm_question(q[:rm.start()] + " VAL " + q[rm.end():])
                if low < high and len(stem.split()) >= 4:
                    ranges.append((stem, low, high, m))
                continue
            nums = list(NUM_RE.finditer(m.group_item_title or ""))
            if len(nums) == 1:
                val = _num(nums[0].group(1), nums[0].group(2))
                qm = [x for x in NUM_RE.finditer(q) if abs(_num(x.group(1), x.group(2)) - val) < 1e-9]
                if len(qm) != 1:
                    continue
                stem = _norm_question(re.sub(r"\b(reach|reaches|hit|hits|above|exceed|exceeds|below|under|dip|dips)\b", "CMP", q[:qm[0].start()] + " VAL " + q[qm[0].end():], flags=re.I))
                if UP_KW.search(q) and not DOWN_KW.search(q):
                    up_thresh.append((stem, val, m))
                elif DOWN_KW.search(q) and not UP_KW.search(q):
                    down_thresh.append((stem, val, m))

        for r_stem, low, high, rm_mkt in ranges:
            for t_stem, k, tm_mkt in up_thresh:
                if r_stem == t_stem and low >= k:
                    o = _emit_pair_arb(
                        tm_mkt, rm_mkt, books, cfg, sub_type="RANGE⊆THRESH",
                        logic_th=f"พิสูจน์ตรรกะเซตย่อย: ช่วง '{rm_mkt.label}' ⊂ เกณฑ์สะสม '{tm_mkt.label}'",
                        logic_en=f"Subset inclusion proof: range '{rm_mkt.label}' ⊂ cumulative threshold '{tm_mkt.label}'",
                    )
                    if o:
                        opps.append(o)
            for t_stem, k, tm_mkt in down_thresh:
                if r_stem == t_stem and high <= k:
                    o = _emit_pair_arb(
                        tm_mkt, rm_mkt, books, cfg, sub_type="RANGE⊆THRESH",
                        logic_th=f"พิสูจน์ตรรกะเซตย่อย: ช่วง '{rm_mkt.label}' ⊂ เกณฑ์สะสม '{tm_mkt.label}'",
                        logic_en=f"Subset inclusion proof: range '{rm_mkt.label}' ⊂ cumulative threshold '{tm_mkt.label}'",
                    )
                    if o:
                        opps.append(o)
    return opps
