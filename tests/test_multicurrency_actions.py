"""多币种、公司行为、暂停与恢复的验收测试。"""

from __future__ import annotations

import tempfile
import unittest

from backtest.broker import CommissionModel
from backtest.data import ManualDataFeed
from backtest.engine import BacktestEngine
from backtest.models import Bar, CorporateAction, CorporateActionType, OrderSide, OrderStatus
from backtest.report import reconcile
from backtest.strategy import Context, Strategy


class ScriptedStrategy(Strategy):
    name = "ScriptedStrategy"

    def __init__(self, script):
        self.script = script

    def on_bar(self, ctx: Context, bar: Bar):
        action = self.script.get(bar.timestamp)
        if action is None:
            return
        action(ctx, bar)


class TestMulticurrencyAndActions(unittest.TestCase):
    def test_fx_buy_split_dividend_halt_and_partial_cancel(self):
        bars = [
            Bar("AAA", 10, 10, 10, 10, 10, 1000, currency="EUR"),
            Bar("AAA", 20, 10, 10, 10, 10, 1000, currency="EUR"),
            Bar("AAA", 30, 5, 5, 5, 5, 500, currency="EUR"),
            Bar("AAA", 40, 5, 5, 5, 5, 100, currency="EUR"),
            Bar("AAA", 50, 5, 5, 5, 5, 1000, currency="EUR"),
        ]
        actions = [
            CorporateAction(CorporateActionType.SPLIT, "AAA", 30, ratio=2.0),
            CorporateAction(CorporateActionType.DIVIDEND, "AAA", 40, amount=1.0, currency="EUR"),
            CorporateAction(CorporateActionType.HALT, "AAA", 40),
            CorporateAction(CorporateActionType.RESUME, "AAA", 50),
        ]
        order_ids = []

        def t10(ctx, bar):
            req = ctx.submit_limit("AAA", OrderSide.BUY, 100.0, 10.0, currency="EUR")
            order_ids.append(req.order.order_id)
            self.assertEqual(ctx.cash_available("EUR"), 0.0)
            ctx.convert_currency("USD", "EUR", 2000.0, 0.9)

        def t20(ctx, bar):
            if not order_ids:
                order_ids.append(1)
            self.assertAlmostEqual(ctx.cash_available("EUR"), 800.0)
            req = ctx.submit_limit("AAA", OrderSide.BUY, 50.0, 10.0, currency="EUR")
            order_ids.append(req.order.order_id)

        def t30(ctx, bar):
            pass

        def t50(ctx, bar):
            self.assertAlmostEqual(ctx.position_view("AAA"), 250.0)

        def t40(ctx, bar):
            ctx.cancel_order(order_ids[1])

        engine = BacktestEngine(
            ManualDataFeed(bars),
            ScriptedStrategy({10: t10, 20: t20, 30: t30, 40: t40, 50: t50}),
            initial_cash={"USD": 10000.0, "EUR": 0.0},
            corporate_actions=actions,
            commission=CommissionModel(rate=0.0, min_commission=0.0),
        )
        result = engine.run()
        events = [(t.kind, t.timestamp) for t in result.event_trace]
        self.assertLess(events.index(("corporate_action", 30)), events.index(("match", 30)))
        split_order = result.orders[1]
        self.assertEqual(split_order.quantity, 100.0)
        self.assertEqual(split_order.limit_price, 5.0)
        self.assertEqual(split_order.filled_quantity, 50.0)
        self.assertEqual(split_order.status, OrderStatus.CANCELLED)
        self.assertEqual([f.quantity for f in result.fills], [100.0, 50.0])
        self.assertEqual([f.price for f in result.fills], [10.0, 5.0])
        self.assertEqual(result.final_positions, {"AAA": 250.0})
        self.assertAlmostEqual(result.final_cash_balances["USD"], 8000.0)
        self.assertAlmostEqual(result.final_cash_balances["EUR"], 800.0)
        types = [e.event_type for e in result.ledger]
        self.assertIn("FX_OUT", types)
        self.assertIn("FX_IN", types)
        self.assertIn("COMMISSION", types)
        self.assertIn("SPLIT", types)
        self.assertIn("DIVIDEND", types)
        self.assertTrue(reconcile(result, tolerance=1e-9)["passed"])

    def test_insufficient_foreign_cash_rejects_without_cash_change(self):
        bars = [Bar("AAA", 10, 10, 10, 10, 10, 100, currency="EUR")]

        def buy(ctx, bar):
            ctx.submit_limit("AAA", OrderSide.BUY, 10.0, 10.0, currency="EUR")

        engine = BacktestEngine(
            ManualDataFeed(bars), ScriptedStrategy({10: buy}),
            initial_cash={"USD": 1000.0, "EUR": 5.0},
            commission=CommissionModel(rate=0.0, min_commission=0.0),
        )
        result = engine.run()
        self.assertEqual(result.orders[0].status, OrderStatus.REJECTED)
        self.assertEqual(result.fills, [])
        self.assertEqual(result.final_cash_balances, {"USD": 1000.0, "EUR": 5.0})
        self.assertFalse(any(e.event_type == "RESERVE" for e in result.ledger))

    def test_save_and_resume_matches_continuous_run(self):
        bars = [
            Bar("AAA", 10, 10, 10, 10, 10, 1000, currency="EUR"),
            Bar("AAA", 20, 10, 10, 10, 10, 1000, currency="EUR"),
            Bar("AAA", 30, 10, 10, 10, 10, 1000, currency="EUR"),
            Bar("AAA", 40, 10, 10, 10, 10, 1000, currency="EUR"),
        ]

        def buy(ctx, bar):
            if bar.timestamp == 10:
                ctx.submit_limit("AAA", OrderSide.BUY, 500.0, 10.0, currency="EUR")
            if bar.timestamp == 30:
                ctx.cancel_order(1)

        continuous = BacktestEngine(
            ManualDataFeed(bars), ScriptedStrategy({10: buy, 30: buy}),
            initial_cash={"EUR": 10000.0},
            commission=CommissionModel(rate=0.0, min_commission=1.0),
        ).run()

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/state.json"
            first = BacktestEngine(
                ManualDataFeed(bars), ScriptedStrategy({10: buy, 30: buy}),
                initial_cash={"EUR": 10000.0},
                commission=CommissionModel(rate=0.0, min_commission=1.0),
            )
            first.run(until_timestamp=20)
            first.save_state(path)
            resumed = BacktestEngine.restore(
                path, ManualDataFeed(bars), ScriptedStrategy({10: buy, 30: buy})
            )
            resumed_result = resumed.run()

        self.assertEqual(
            [f.to_row() for f in resumed_result.fills],
            [f.to_row() for f in continuous.fills],
        )
        self.assertEqual(
            [(e.entry_id, e.timestamp, e.event_type, e.delta_cash, e.delta_cash_frozen,
              e.delta_position, e.delta_position_frozen) for e in resumed_result.ledger],
            [(e.entry_id, e.timestamp, e.event_type, e.delta_cash, e.delta_cash_frozen,
              e.delta_position, e.delta_position_frozen) for e in continuous.ledger],
        )
        self.assertEqual(resumed_result.final_cash_balances, continuous.final_cash_balances)
        self.assertEqual(resumed_result.final_positions, continuous.final_positions)


if __name__ == "__main__":
    unittest.main()
