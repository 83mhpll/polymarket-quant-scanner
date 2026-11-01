"""Strategy 3 — Logical / monotonicity ("ladder") arbitrage across related markets.

If event A implies event B (A ⊆ B) then P(A) <= P(B). Typical ladders on Polymarket:
  * price thresholds   "Will BTC reach $100k"  ⇒  "Will BTC reach $90k"
  * dip thresholds     "dip to $60k"           ⇒  "dip to $70k"
  * deadlines          "X by March 31"         ⇒  "X by June 30"
  * persistence        "continues through Dec" ⇒  "continues through Nov"
  * sports lines       "Spread: Team (-2.5)"   ⇒  "Spread: Team (-1.5)";  "O/U 3.5 Over" ⇒ "O/U 2.5 Over"

Trade: BUY YES(B, weaker) + BUY NO(A, stronger). Payoff in every state >= $1:
   A true  -> B true : 1 + 0 = 1
   A false, B true   : 1 + 1 = 2   (bonus)
   both false        : 0 + 1 = 1
So if ask_YES(B) + ask_NO(A) + fees < 1 the profit is locked (subject to correct rule parsing).
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..config import Config
from ..execution_math import basket_walk
from ..models import Market, OrderBook
from .base import ARB, Leg, Opportunity

NAME = "ladder_arb"

NUM_RE = re.compile(r"\$?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kKmMbB])?\b")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december", "jan ", "feb ", "mar ", "apr ",
          "jun ", "jul ", "aug ", "sep ", "sept ", "oct ", "nov ", "dec ", "q1", "q2", "q3", "q4")
UP_KW = re.compile(r"\b(reach|reaches|hit|hits|above|exceed|exceeds|greater than|more than|at least|higher than|or more|or higher|surpass|top)\b|↑", re.I)
DOWN_KW = re.compile(r"\b(dip|dips|below|under|less than|fall to|falls to|drop to|drops to|lower than|or less|or lower)\b|↓", re.I)
BY_KW = re.compile(r"\b(by|before|in or before)\b", re.I)
THROUGH_KW = re.compile(r"\b(through|until|remain|remains|continue|continues|still)\b", re.I)


def _num(tok: str, suf: str | None) -> float:
    v = float(tok.replace(",", ""))
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get((suf or "").lower(), 1.0)
    return v * mult


def classify(m: Market) -> tuple[str, float, int] | None:
    """Return (template, key, sign) where P(YES) is monotone in key with direction `sign` (+1 increasing)."""
    if not m.is_binary:
        return None
    q = m.question.strip()
    g = (m.group_item_title or "").strip()
    o0 = m.outcomes[0].name.strip().lower()

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

    # date ladders: groupItemTitle is a date-like string contained in the question
    gl = g.lower()
    if g and g in q and any(mo in gl + " " for mo in MONTHS) and m.end_date:
        tmpl = q.replace(g, "<DATE>")
        if THROUGH_KW.search(tmpl):
            return (tmpl, m.end_date.timestamp(), -1)
        if BY_KW.search(tmpl):
            return (tmpl, m.end_date.timestamp(), +1)
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
            return None  # range buckets are not ladders
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
                # sign=+1: P increases with key -> lower key implies higher key
                strong, weak = (a, b) if sign > 0 else (b, a)
                bs_no = books.get(strong.no.token_id)
                bw_yes = books.get(weak.yes.token_id)
                if not bs_no or not bw_yes or not bs_no.asks or not bw_yes.asks:
                    continue
                days = max([d for d in (strong.days_to_end, weak.days_to_end) if d is not None], default=None)
                min_sh = max(bs_no.min_order_size, bw_yes.min_order_size)

                # (a) Pure Taker-Taker arbitrage (both legs cross the ask book immediately)
                if bs_no.asks[0].price + bw_yes.asks[0].price < 1.0:
                    f = basket_walk([bw_yes.asks, bs_no.asks], [weak.fee_rate, strong.fee_rate],
                                    side="buy", set_value=1.0, min_edge=cfg.min_arb_edge,
                                    max_capital=cfg.max_capital_per_trade, slippage=cfg.slippage_buffer)
                    if f.sets >= min_sh and f.profit >= cfg.min_profit_usd:
                        opps.append(Opportunity(
                            strategy=NAME, kind=ARB,
                            title=f"YES[{weak.label}] + NO[{strong.label}] = {sum(f.avg_prices):.3f} (Taker)",
                            event_title=weak.event_title, url=weak.url,
                            legs=[
                                Leg(weak.yes.token_id, f"{weak.label} [{weak.yes.name}]", weak.question, "BUY",
                                    f.worst_prices[0], f.avg_prices[0], f.sets, weak.neg_risk, bw_yes.tick_size),
                                Leg(strong.no.token_id, f"{strong.label} [{strong.no.name}]", strong.question, "BUY",
                                    f.worst_prices[1], f.avg_prices[1], f.sets, strong.neg_risk, bs_no.tick_size),
                            ],
                            capital=f.capital, exp_profit=f.profit, roi=f.roi, days=days,
                            confidence=0.88,
                            notes=[f"ตรรกะ: '{strong.label}' ⇒ '{weak.label}' จึงต้องมี P(strong) ≤ P(weak)",
                                   f"หัก Taker fee ทั้งสองขาแล้ว (${f.fees:.2f}) กำไรขั้นต่ำล็อกทันที",
                                   "ถ้า strong=NO และ weak=YES จะได้ $2 ต่อชุด (upside พิเศษ)"],
                            notes_en=[f"Logic: '{strong.label}' ⇒ '{weak.label}', so P(strong) ≤ P(weak) must hold",
                                      f"Net of two-sided Taker fees (${f.fees:.2f}), minimum profit is locked",
                                      "If strong=NO and weak=YES, payout is $2.00 per set (free upside)"],
                            metrics={"sets": f.sets, "template": tmpl, "edge_top": f.first_edge,
                                     "payout": 1.0, "fee_rates": [weak.fee_rate, strong.fee_rate], "mode": "taker"},
                        ))
                        continue

                # (b) Hybrid Maker+Taker arbitrage: post 0-fee Maker bid on leg 1, then Taker buy leg 2 on fill
                if not getattr(cfg, "enable_hybrid_arb", True) or not bs_no.bids or not bw_yes.bids:
                    continue
                # Pick which leg to rest as Maker (0 fee) and which to cross as Taker
                # Option 1: Maker bid on weak.YES (@ best_bid), Taker ask on strong.NO
                # Option 2: Maker bid on strong.NO (@ best_bid), Taker ask on weak.YES
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
                            Leg(weak.yes.token_id, f"{weak.label} [{weak.yes.name}]" + (" (Maker)" if maker_leg == 0 else " (Taker)"),
                                weak.question, "BUY", f.worst_prices[0], f.avg_prices[0], f.sets, weak.neg_risk, bw_yes.tick_size),
                            Leg(strong.no.token_id, f"{strong.label} [{strong.no.name}]" + (" (Maker)" if maker_leg == 1 else " (Taker)"),
                                strong.question, "BUY", f.worst_prices[1], f.avg_prices[1], f.sets, strong.neg_risk, bs_no.tick_size),
                        ],
                        capital=f.capital, exp_profit=f.profit, roi=f.roi, days=days,
                        confidence=0.78,
                        notes=[f"ตรรกะ: '{strong.label}' ⇒ '{weak.label}' (ราคากลับหัว P(strong) > P(weak))",
                               f"วิธีเข้า: วาง GTC Maker Bid ที่ '{m_lbl}' (ค่าธรรมเนียม 0%) เมื่อแมตช์แล้วส่ง FOK Taker ที่ '{t_lbl}'",
                               f"ประหยัดค่าธรรมเนียมขา Maker ทำให้อาร์บิทราจมีกำไรสุทธิ +${f.profit:.2f}"],
                        notes_en=[f"Logic: '{strong.label}' ⇒ '{weak.label}' (inverted pricing P(strong) > P(weak))",
                                  f"Execution: post GTC Maker Bid on '{m_lbl}' (0% fee), then send FOK Taker on '{t_lbl}' upon fill",
                                  f"Saving the Maker leg fee yields net locked profit of +${f.profit:.2f}"],
                        metrics={"sets": f.sets, "template": tmpl, "edge_top": f.first_edge,
                                 "payout": 1.0, "fee_rates": [0.0, strong.fee_rate] if maker_leg == 0 else [weak.fee_rate, 0.0],
                                 "mode": "hybrid_maker_taker", "maker_leg": maker_leg},
                    ))
    return opps
