"""Strategy 3 — Logical / monotonicity ("ladder") arbitrage across related markets.

If event A implies event B (A ⊆ B) then P(A) <= P(B). Typical ladders on Polymarket:
  * price thresholds   "Will BTC reach $100k"  ⇒  "Will BTC reach $90k"
  * dip thresholds     "dip to $60k"           ⇒  "dip to $70k"
  * deadlines          "X by March 31, 2026"   ⇒  "X by June 30, 2026"
  * persistence        "continues through Dec" ⇒  "continues through Nov"
  * sports lines       "Spread: Team (-2.5)"   ⇒  "Spread: Team (-1.5)";  "O/U 3.5 Over" ⇒ "O/U 2.5 Over"

Trade: BUY YES(B, weaker/easier) + BUY NO(A, stronger/harder). Payoff in every state >= $1:
   A true  -> B true : 1 + 0 = $1.00
   A false, B true   : 1 + 1 = $2.00  (Double Payout Bonus!)
   both false        : 0 + 1 = $1.00
"""
from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import datetime, timezone

from ..config import Config
from ..execution_math import basket_walk
from ..models import Market, OrderBook
from .base import ARB, Leg, Opportunity

NAME = "ladder_arb"

NUM_RE = re.compile(r"\$?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kKmMbB])?\b")
MONTH_MAP = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
DATE_TEXT_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,\s*|\s+)?(20\d{2})?\b",
    re.I,
)
MONTH_YEAR_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+(20\d{2})\b",
    re.I,
)
UP_KW = re.compile(r"\b(reach|reaches|hit|hits|above|exceed|exceeds|greater than|more than|at least|higher than|or more|or higher|surpass|top)\b|↑", re.I)
DOWN_KW = re.compile(r"\b(dip|dips|below|under|less than|fall to|falls to|drop to|drops to|lower than|or less|or lower)\b|↓", re.I)
BY_KW = re.compile(r"\b(by|before|in or before|on or before)\b", re.I)
THROUGH_KW = re.compile(r"\b(through|remain|remains|continue|continues|still in office|stay in)\b", re.I)
BUCKET_TRAP_KW = re.compile(r"\b(between|which month|which quarter|when will|what month|exactly)\b", re.I)


def _num(tok: str, suf: str | None) -> float:
    v = float(tok.replace(",", ""))
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get((suf or "").lower(), 1.0)
    return v * mult


def parse_explicit_date(label: str, fallback_dt: datetime | None = None) -> float | None:
    """Parse calendar date directly from label text, handling UTC-vs-ET midnight year rollovers."""
    s = label.strip()
    m = DATE_TEXT_RE.search(s)
    if m:
        mo = MONTH_MAP[m.group(1).lower().rstrip(".")]
        day = int(m.group(2))
        yr_str = m.group(3)
        if yr_str:
            try:
                return datetime(int(yr_str), mo, day, 23, 59, 59, tzinfo=timezone.utc).timestamp()
            except ValueError:
                return None
        if fallback_dt:
            best_dt = None
            best_diff = float("inf")
            for yr in (fallback_dt.year - 1, fallback_dt.year, fallback_dt.year + 1):
                try:
                    cand = datetime(yr, mo, day, 23, 59, 59, tzinfo=timezone.utc)
                    diff = abs((cand - fallback_dt).total_seconds())
                    if diff < best_diff:
                        best_diff, best_dt = diff, cand
                except ValueError:
                    continue
            if best_dt is not None:
                return best_dt.timestamp()
        return None

    m2 = MONTH_YEAR_RE.search(s)
    if m2:
        mo = MONTH_MAP[m2.group(1).lower().rstrip(".")]
        yr = int(m2.group(2))
        last_day = calendar.monthrange(yr, mo)[1]
        return datetime(yr, mo, last_day, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    return None


def classify(m: Market) -> tuple[str, float, int] | None:
    """Return (template, key, sign) where P(YES) is monotone in key with direction `sign` (+1 increasing)."""
    if not m.is_binary:
        return None
    q = m.question.strip()
    g = (m.group_item_title or "").strip()
    o0 = m.outcomes[0].name.strip().lower()

    if BUCKET_TRAP_KW.search(q):
        return None

    # sports spread: "Spread: Team (-2.5)" -> covering a bigger line is harder
    sp = re.search(r"\(([-+]?\d+(?:\.\d+)?)\)", q)
    if q.lower().startswith("spread") and sp:
        team = q.split(":", 1)[-1].split("(")[0].strip().lower()
        if (team and team.split()[0] in o0) or o0 in team:
            val = float(sp.group(1))
            if val < 0:
                return (f"spread:{team}", abs(val), -1)
        return None

    # totals: "... O/U 2.5" with outcomes [Over, Under]
    ou = re.search(r"O/U\s*(\d+(?:\.\d+)?)", q)
    if ou:
        if o0 != "over":
            return None
        return (q[:ou.start()] + "O/U #" + q[ou.end():], float(ou.group(1)), -1)

    if o0 != "yes":
        return None

    # date ladders: parse date from question context around groupItemTitle
    if g and g in q:
        idx = q.find(g)
        # Include up to 10 chars after g in case the 4-digit year follows g in question
        date_slice = q[idx : idx + len(g) + 10]
        parsed_ts = parse_explicit_date(date_slice, m.end_date)
        if parsed_ts is not None:
            # Replace date_slice (if year followed g) or g with <DATE>
            tmpl = re.sub(re.escape(g) + r"(?:\s*,?\s*20\d{2})?", "<DATE>", q, count=1)
            has_through = bool(THROUGH_KW.search(tmpl))
            has_by = bool(BY_KW.search(tmpl))
            if has_through and not has_by:
                return (tmpl, parsed_ts, -1)
            if has_by and not has_through:
                return (tmpl, parsed_ts, +1)
            return None

    # numeric thresholds: prefer number from groupItemTitle
    src = g if g and NUM_RE.search(g) else ""
    nums = list(NUM_RE.finditer(src)) if src else []
    if len(nums) == 1:
        tok, suf = nums[0].group(1), nums[0].group(2)
        val = _num(tok, suf)
        qm = [x for x in NUM_RE.finditer(q) if abs(_num(x.group(1), x.group(2)) - val) < 1e-9]
        if len(qm) != 1:
            return None
        tmpl = q[:qm[0].start()] + "#" + q[qm[0].end():]
        up, down = bool(UP_KW.search(g + " " + q)), bool(DOWN_KW.search(g + " " + q))
        if " between " in q.lower() or "-" in g.replace(",", "").strip("↑↓ $") and not g.strip().startswith("-"):
            return None
        if up and not down:
            return (tmpl, val, -1)
        if down and not up:
            return (tmpl, val, +1)
    return None


def scan(markets: list[Market], books: dict[str, OrderBook], cfg: Config) -> list[Opportunity]:
    groups: dict[tuple[str, str], list[tuple[float, int, Market]]] = defaultdict(list)
    for m in markets:
        if m.neg_risk:  # negRisk legs are mutually exclusive, not nested
            continue
        c = classify(m)
        if c:
            tmpl, key, sign = c
            groups[(m.event_id, tmpl)].append((key, sign, m))

    opps: list[Opportunity] = []
    for (_, tmpl), items in groups.items():
        if len(items) < 2 or len({s for _, s, _ in items}) != 1:
            continue
        items.sort(key=lambda x: x[0])
        sign = items[0][1]
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                ka, _, a = items[i]
                kb, _, b = items[j]
                if ka == kb:
                    continue
                # If neither label had an explicit 4-digit year, require m.end_date order to agree with ka < kb
                if "<DATE>" in tmpl and a.end_date and b.end_date:
                    has_yr_a = bool(re.search(r"20\d{2}", (a.group_item_title or "") + " " + a.question))
                    has_yr_b = bool(re.search(r"20\d{2}", (b.group_item_title or "") + " " + b.question))
                    if not (has_yr_a and has_yr_b) and a.end_date.timestamp() >= b.end_date.timestamp():
                        continue
                # sign=+1: P increases with key -> lower key (earlier deadline) implies higher key (later deadline)
                strong, weak = (a, b) if sign > 0 else (b, a)
                bs_no = books.get(strong.no.token_id)
                bw_yes = books.get(weak.yes.token_id)
                if not bs_no or not bw_yes or not bs_no.asks or not bw_yes.asks:
                    continue
                # Guard against unflagged mutually-exclusive bucket markets masquerading as >25% inversions
                inv_gap = 1.0 - (bs_no.asks[0].price + bw_yes.asks[0].price)
                if inv_gap > 0.25:
                    continue

                days = max([d for d in (strong.days_to_end, weak.days_to_end) if d is not None], default=None)
                min_sh = max(bs_no.min_order_size, bw_yes.min_order_size)

                # (a) Pure Offer-Side Taker-Taker Arbitrage (both legs buy immediately from Offer/Ask book)
                if bs_no.asks[0].price + bw_yes.asks[0].price < 1.0:
                    f = basket_walk([bw_yes.asks, bs_no.asks], [weak.fee_rate, strong.fee_rate],
                                    side="buy", set_value=1.0, min_edge=cfg.min_arb_edge,
                                    max_capital=cfg.max_capital_per_trade, slippage=cfg.slippage_buffer)
                    if f.sets >= min_sh and f.profit >= cfg.min_profit_usd:
                        opps.append(Opportunity(
                            strategy=NAME, kind=ARB,
                            title=f"YES[{weak.label}] + NO[{strong.label}] = {sum(f.avg_prices):.3f} (Offer Book)",
                            event_title=weak.event_title, url=weak.url,
                            legs=[
                                Leg(weak.yes.token_id, f"{weak.label} [{weak.yes.name}]", weak.question, "BUY",
                                    f.worst_prices[0], f.avg_prices[0], f.sets, weak.neg_risk, bw_yes.tick_size,
                                    order_type="FOK", best_bid=bw_yes.best_bid, best_ask=bw_yes.best_ask, spread=bw_yes.spread),
                                Leg(strong.no.token_id, f"{strong.label} [{strong.no.name}]", strong.question, "BUY",
                                    f.worst_prices[1], f.avg_prices[1], f.sets, strong.neg_risk, bs_no.tick_size,
                                    order_type="FOK", best_bid=bs_no.best_bid, best_ask=bs_no.best_ask, spread=bs_no.spread),
                            ],
                            capital=f.capital, exp_profit=f.profit, roi=f.roi, days=days,
                            confidence=0.92,
                            notes=[
                                f"พิสูจน์ตรรกะ: '{strong.label}' (เกิดยากกว่า) ⇒ '{weak.label}' (เกิดง่ายกว่า) ดังนั้น P({strong.label}) ≤ P({weak.label})",
                                f"คำนวณจากฝั่ง Offer (Ask Book) จริงทั้ง 2 ขา หัก Taker fee (${f.fees:.2f}) แล้วล็อกกำไรทันที",
                                f"โบนัส 2 เด้ง: ถ้า '{weak.label}' เป็นจริง แต่ '{strong.label}' ไม่เกิด จะได้รับเงินคืน $2.00 ต่อชุด!",
                            ],
                            notes_en=[
                                f"Logic Proof: '{strong.label}' (harder) ⇒ '{weak.label}' (easier), so P({strong.label}) ≤ P({weak.label})",
                                f"Calculated 100% from real Offer (Ask) order book depth net of Taker fees (${f.fees:.2f})",
                                f"Double Payout Bonus: if '{weak.label}' happens but '{strong.label}' does not, pays $2.00 per set!",
                            ],
                            metrics={"sets": f.sets, "template": tmpl, "edge_top": f.first_edge,
                                     "payout": 1.0, "fee_rates": [weak.fee_rate, strong.fee_rate], "mode": "taker",
                                     "strong_label": strong.label, "weak_label": weak.label},
                        ))
                        continue

                # (b) Hybrid Maker+Taker arbitrage: post 0-fee Maker bid on leg 1, then Taker buy leg 2 on fill
                if not getattr(cfg, "enable_hybrid_arb", True) or not bs_no.bids or not bw_yes.bids:
                    continue
                if (bs_no.spread or 1.0) > 0.08 or (bw_yes.spread or 1.0) > 0.08:
                    continue  # Reject wide-spread books for Hybrid mode
                opt1_lv = [bw_yes.bids[:1], bs_no.asks]
                opt2_lv = [bw_yes.asks, bs_no.bids[:1]]
                f1 = basket_walk(opt1_lv, [0.0, strong.fee_rate], side="buy", set_value=1.0,
                                 min_edge=cfg.min_arb_edge, max_capital=cfg.max_capital_per_trade)
                f2 = basket_walk(opt2_lv, [weak.fee_rate, 0.0], side="buy", set_value=1.0,
                                 min_edge=cfg.min_arb_edge, max_capital=cfg.max_capital_per_trade)
                f, maker_leg = (f1, 0) if f1.profit >= f2.profit else (f2, 1)
                if f.sets >= min_sh and f.profit >= cfg.min_profit_usd:
                    m_lbl = f"{weak.label} YES" if maker_leg == 0 else f"{strong.label} NO"
                    t_lbl = f"{strong.label} NO" if maker_leg == 0 else f"{weak.label} YES"
                    opps.append(Opportunity(
                        strategy=NAME, kind=ARB,
                        title=f"HYBRID Maker-Taker: YES[{weak.label}] + NO[{strong.label}] = {sum(f.avg_prices):.3f}",
                        event_title=weak.event_title, url=weak.url,
                        legs=[
                            Leg(weak.yes.token_id, f"{weak.label} [{weak.yes.name}]" + (" (Maker Bid)" if maker_leg == 0 else " (Offer Taker)"),
                                weak.question, "BUY", f.worst_prices[0], f.avg_prices[0], f.sets, weak.neg_risk, bw_yes.tick_size,
                                order_type="GTC" if maker_leg == 0 else "FOK", best_bid=bw_yes.best_bid, best_ask=bw_yes.best_ask, spread=bw_yes.spread),
                            Leg(strong.no.token_id, f"{strong.label} [{strong.no.name}]" + (" (Maker Bid)" if maker_leg == 1 else " (Offer Taker)"),
                                strong.question, "BUY", f.worst_prices[1], f.avg_prices[1], f.sets, strong.neg_risk, bs_no.tick_size,
                                order_type="GTC" if maker_leg == 1 else "FOK", best_bid=bs_no.best_bid, best_ask=bs_no.best_ask, spread=bs_no.spread),
                        ],
                        capital=f.capital, exp_profit=f.profit, roi=f.roi, days=days,
                        confidence=0.78,
                        notes=[f"พิสูจน์ตรรกะ: '{strong.label}' ⇒ '{weak.label}' (ราคากลับหัว P(strong) > P(weak))",
                               f"วิธีเข้า (Hybrid): ตั้งรับ GTC Maker Bid ที่ '{m_lbl}' (0% fee) เมื่อมีคนขายให้แล้วเคาะซื้อฝั่ง Offer ที่ '{t_lbl}'",
                               f"ประหยัดค่าธรรมเนียมขา Maker ทำให้อาร์บิทราจมีกำไรสุทธิ +${f.profit:.2f}"],
                        notes_en=[f"Logic Proof: '{strong.label}' ⇒ '{weak.label}' (inverted pricing P(strong) > P(weak))",
                                  f"Hybrid Execution: post GTC Maker Bid on '{m_lbl}' (0% fee), then sweep Offer on '{t_lbl}' upon fill",
                                  f"Saving the Maker leg fee yields net locked profit of +${f.profit:.2f}"],
                        metrics={"sets": f.sets, "template": tmpl, "edge_top": f.first_edge,
                                 "payout": 1.0, "fee_rates": [0.0, strong.fee_rate] if maker_leg == 0 else [weak.fee_rate, 0.0],
                                 "mode": "hybrid_maker_taker", "maker_leg": maker_leg,
                                 "strong_label": strong.label, "weak_label": weak.label},
                    ))
    return opps
