import unittest
from datetime import datetime, timedelta, timezone

from polyscan.execution_math import basket_walk, taker_fee, walk_single
from polyscan.models import Level, Market, OrderBook, Outcome
from polyscan.strategies import binary_arb, ladder_arb, negrisk_arb
from polyscan.config import Config
from polyscan.strategies.negrisk_arb import EventGroup


def mk(q, yes_tok, no_tok, event="e1", gtitle="", neg=False, outcomes=("Yes", "No"), days=5):
    return Market(
        id=yes_tok, condition_id="c", question=q, slug="s", event_id=event, event_slug="ev", event_title="E",
        end_date=datetime.now(timezone.utc) + timedelta(days=days),
        outcomes=[Outcome(outcomes[0], yes_tok, 0.5), Outcome(outcomes[1], no_tok, 0.5)],
        neg_risk=neg, neg_risk_other=False, neg_risk_augmented=False, fee_rate=0.0, volume_24h=1e5,
        liquidity=1e5, best_bid=None, best_ask=None, one_hour_change=0, one_day_change=0, rewards_daily=0,
        rewards_min_size=0, rewards_max_spread=0, tick=0.01, min_size=5, accepting_orders=True,
        group_item_title=gtitle, group_item_threshold="")


def ob(tok, bids=(), asks=()):
    return OrderBook(tok, [Level(p, s) for p, s in bids], [Level(p, s) for p, s in asks])


class TestMath(unittest.TestCase):
    def test_fee(self):
        self.assertAlmostEqual(taker_fee(0.5, 0.07), 0.0175)
        self.assertAlmostEqual(taker_fee(0.9, 0.07), 0.0063)

    def test_basket_buy_stops_at_breakeven(self):
        f = basket_walk([[Level(0.40, 100), Level(0.55, 100)], [Level(0.50, 150)]], [0, 0],
                        side="buy", set_value=1.0)
        self.assertAlmostEqual(f.sets, 100)          # second YES level makes 1.05 > 1
        self.assertAlmostEqual(f.profit, 10.0)
        self.assertAlmostEqual(f.capital, 90.0)

    def test_basket_capital_cap(self):
        f = basket_walk([[Level(0.40, 1000)], [Level(0.50, 1000)]], [0, 0], side="buy", set_value=1.0, max_capital=45)
        self.assertAlmostEqual(f.capital, 45)
        self.assertAlmostEqual(f.sets, 50)

    def test_basket_sell(self):
        f = basket_walk([[Level(0.55, 10)], [Level(0.50, 20)]], [0, 0], side="sell", set_value=1.0)
        self.assertAlmostEqual(f.profit, 0.5)

    def test_walk_single(self):
        sh, cost, _ = walk_single([Level(0.95, 10), Level(0.97, 10)], max_capital=1e9, price_limit=0.96, fee_rate=0)
        self.assertEqual(sh, 10)
        self.assertAlmostEqual(cost, 9.5)


class TestStrategies(unittest.TestCase):
    cfg = Config(min_profit_usd=0.01, min_arb_edge=0.0)

    def test_binary_arb(self):
        m = mk("Q?", "y", "n")
        books = {"y": ob("y", asks=[(0.45, 50)]), "n": ob("n", asks=[(0.50, 50)])}
        opps = binary_arb.scan([m], books, self.cfg)
        self.assertEqual(len(opps), 1)
        self.assertAlmostEqual(opps[0].exp_profit, 2.5)

    def test_negrisk_no_basket(self):
        ms = [mk(f"Will {c} win?", f"y{c}", f"n{c}", neg=True) for c in "ABC"]
        books = {}
        for c, (ya, na) in zip("ABC", [(0.5, 0.45), (0.3, 0.65), (0.25, 0.70)]):
            books[f"y{c}"] = ob(f"y{c}", asks=[(ya, 100)], bids=[(ya - 0.01, 100)])
            books[f"n{c}"] = ob(f"n{c}", asks=[(na, 100)])
        g = EventGroup("e1", "E", "u", ms, complete=True)
        opps = negrisk_arb.scan([g], books, self.cfg)
        kinds = {o.title.split()[0] for o in opps}
        self.assertIn("NO-basket", kinds)        # 0.45+0.65+0.70 = 1.80 < 2
        self.assertIn("SELL", kinds)             # bids 0.49+0.29+0.24 = 1.02 > 1

    def test_ladder_classify_and_arb(self):
        a = mk("Will Bitcoin reach $100,000 by December 31, 2026?", "ya", "na", gtitle="↑ 100,000")
        b = mk("Will Bitcoin reach $90,000 by December 31, 2026?", "yb", "nb", gtitle="↑ 90,000")
        self.assertEqual(ladder_arb.classify(a)[2], -1)
        books = {"ya": ob("ya", asks=[(0.50, 100)]), "na": ob("na", asks=[(0.52, 100)]),
                 "yb": ob("yb", asks=[(0.40, 100)]), "nb": ob("nb", asks=[(0.62, 100)])}
        opps = ladder_arb.scan([a, b], books, self.cfg)  # YES(90k)=0.40 + NO(100k)=0.52 = 0.92
        self.assertEqual(len(opps), 1)
        self.assertAlmostEqual(opps[0].exp_profit, 8.0)

    def test_ladder_dates(self):
        e1 = mk("Ceasefire continues through November 30?", "y1", "n1", gtitle="November 30", days=29)
        e2 = mk("Ceasefire continues through December 31?", "y2", "n2", gtitle="December 31", days=60)
        t1, t2 = ladder_arb.classify(e1), ladder_arb.classify(e2)
        self.assertEqual(t1[0], t2[0])
        self.assertEqual(t1[2], -1)  # later date harder for persistence

    def test_spread_ladder(self):
        s1 = mk("Spread: Browns (-1.5)", "a", "b", outcomes=("Browns", "Steelers"))
        s2 = mk("Spread: Browns (-2.5)", "c", "d", outcomes=("Browns", "Steelers"))
        s_other_team = mk("Spread: Steelers (-2.5)", "e", "f", outcomes=("Browns", "Steelers"))
        self.assertEqual(ladder_arb.classify(s1)[0], ladder_arb.classify(s2)[0])
        self.assertIsNone(ladder_arb.classify(s_other_team))

    def test_hybrid_ladder_arb_beats_taker_fee(self):
        # 7% fee rate blocks taker-taker (0.109 + 0.884 = 0.993, fees = 0.0139 -> 1.0069 > 1.0)
        # but hybrid Maker(0% fee) + Taker clears with positive profit
        a = mk("Metamask FDV above $100M one day after launch?", "ya", "na", gtitle="$100M")
        b = mk("Metamask FDV above $300M one day after launch?", "yb", "nb", gtitle="$300M")
        a.fee_rate = b.fee_rate = 0.07
        books = {
            "ya": ob("ya", bids=[(0.106, 200)], asks=[(0.109, 100)]),
            "na": ob("na", bids=[(0.891, 100)], asks=[(0.894, 200)]),
            "yb": ob("yb", bids=[(0.116, 60)], asks=[(0.118, 100)]),
            "nb": ob("nb", bids=[(0.882, 100)], asks=[(0.884, 60)]),
        }
        opps = ladder_arb.scan([a, b], books, self.cfg)
        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0].metrics["mode"], "hybrid_maker_taker")
        self.assertGreater(opps[0].exp_profit, 0.0)

    def test_negrisk_augmented_without_other_blocks_yes_basket(self):
        ms = [mk(f"Will {c} win?", f"y{c}", f"n{c}", neg=True) for c in "AB"]
        books = {"yA": ob("yA", asks=[(0.4, 100)]), "nA": ob("nA", asks=[(0.6, 100)]),
                 "yB": ob("yB", asks=[(0.4, 100)]), "nB": ob("nB", asks=[(0.6, 100)])}
        g = EventGroup("e1", "E", "u", ms, complete=True, augmented=True, has_other=False)
        opps = negrisk_arb.scan([g], books, self.cfg)
        self.assertEqual(len(opps), 0)


if __name__ == "__main__":
    unittest.main()
