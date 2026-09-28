"""回测引擎第一版验收测试。

覆盖要点：
- 事件推进顺序（行情 -> 策略 -> 撤单 -> 新单 -> 撮合 -> 成交回报）
- 同一时刻撤单与成交相撞：默认撤单先于成交，可配置反转
- 挂单不等于成交；部分成交；手续费最低值
- 策略只能看到当前已到达的 bar
- 现金与持仓仅凭 fills 独立复算一致
- 结束时未成交单单独保留
"""

from __future__ import annotations

import unittest

from backtest.account import recompute_from_fills
from backtest.broker import CommissionModel
from backtest.data import ManualDataFeed
from backtest.engine import BacktestEngine
from backtest.events import Priority
from backtest.models import Bar, OrderSide, OrderStatus, OrderType
from backtest.report import reconcile, save_run
from backtest.strategy import Context, Strategy


def make_bars():
    # t=10 到 t=40 四根 bar，成交量逐根放大
    return [
        Bar("TEST", 10, 100, 101, 99, 100, 1000),
        Bar("TEST", 20, 100, 101, 99, 100, 1000),
        Bar("TEST", 30, 100, 101, 99, 100, 1000),
        Bar("TEST", 40, 100, 101, 99, 100, 1000),
    ]


class CollisionStrategy(Strategy):
    """构造相撞场景：
    t=10 挂限价买单 100 股 @100（t=10 当根不撮合）；
    t=20 同时：提交撤单（撤 o1）+ 新挂限价买单 o2 100 股 @100。
    默认顺序下 o1 在撮合前被撤、不成交；o2 当根不撮合，t=30 起才成交。
    """

    name = "CollisionStrategy"

    def __init__(self):
        self.first_id = None

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        if bar.timestamp == 10:
            req = ctx.submit_limit("TEST", OrderSide.BUY, 100.0, 100.0)
            self.first_id = req.order.order_id
        elif bar.timestamp == 20:
            ctx.cancel_order(self.first_id)
            ctx.submit_limit("TEST", OrderSide.BUY, 100.0, 100.0)


class TestEventOrdering(unittest.TestCase):
    def test_cancel_beats_fill_at_same_timestamp(self):
        engine = BacktestEngine(
            ManualDataFeed(make_bars()),
            CollisionStrategy(),
            initial_cash=1_000_000.0,
            cancel_before_match=True,
        )
        result = engine.run()

        o1, o2 = result.orders[0], result.orders[1]
        # o1 在 t=20 撮合之前被撤：没有任何成交
        self.assertEqual(o1.status, OrderStatus.CANCELLED)
        self.assertEqual(o1.filled_quantity, 0.0)
        self.assertEqual(o1.cancelled_at, 20)
        # o2 在 t=20 提交，当根不撮合；t=30 才首次成交
        self.assertTrue(all(f.order_id != o1.order_id for f in result.fills))
        o2_fills = [f for f in result.fills if f.order_id == o2.order_id]
        self.assertTrue(o2_fills)
        self.assertEqual(min(f.bar_timestamp for f in o2_fills), 30)

        # 事件轨迹中 t=20 的撤单事件必须早于撮合事件
        t20 = [t for t in result.event_trace if t.timestamp == 20]
        kinds = [t.kind for t in t20]
        self.assertLess(kinds.index("cancel"), kinds.index("match"))
        self.assertEqual(Priority.CANCEL, Priority.MATCH - 20)

    def test_match_before_cancel_when_configured(self):
        engine = BacktestEngine(
            ManualDataFeed(make_bars()),
            CollisionStrategy(),
            cancel_before_match=False,
        )
        result = engine.run()
        o1 = result.orders[0]
        # 反向配置：t=20 先撮合，o1 在被撤之前成交，撤单随后失败
        o1_fills = [f for f in result.fills if f.order_id == o1.order_id]
        self.assertTrue(o1_fills)
        self.assertEqual(o1.status, OrderStatus.FILLED)
        t20 = [t for t in result.event_trace if t.timestamp == 20]
        kinds = [t.kind for t in t20]
        self.assertLess(kinds.index("match"), kinds.index("cancel"))


class PartialFillStrategy(Strategy):
    name = "PartialFillStrategy"

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        if bar.timestamp == 10:
            # 500 股；max_participation=0.1、每根 bar 量 1000，故每根最多成交 100
            ctx.submit_limit("TEST", OrderSide.BUY, 500.0, 100.0)


class TestFillsAndFees(unittest.TestCase):
    def test_partial_fills_across_bars_and_commission(self):
        engine = BacktestEngine(
            ManualDataFeed(make_bars()),
            PartialFillStrategy(),
            commission=CommissionModel(rate=0.0003, min_commission=5.0),
            max_participation=0.1,
        )
        result = engine.run()
        order = result.orders[0]

        # 新单 t=10 不撮合；t=20/30/40 各成交 100，共 300，剩余 200 未成交
        self.assertEqual(len(result.fills), 3)
        self.assertEqual([f.quantity for f in result.fills], [100.0, 100.0, 100.0])
        self.assertEqual(order.filled_quantity, 300.0)
        self.assertEqual(order.status, OrderStatus.PARTIALLY_FILLED)

        # 每笔成交金额 100*100=10000，手续费 max(5, 10000*0.0003=3)=5
        self.assertEqual([f.commission for f in result.fills], [5.0, 5.0, 5.0])

        # 结束时未成交单单独保留：剩余数量 200
        open_orders = engine.broker.open_orders_snapshot()
        self.assertEqual(len(open_orders), 1)
        self.assertEqual(open_orders[0].remaining_quantity, 200.0)

        # 账户：现金减少 300*100 + 15 手续费；持仓 +300
        self.assertAlmostEqual(result.final_cash, 1_000_000.0 - 30_000.0 - 15.0, places=6)
        self.assertEqual(result.final_positions, {"TEST": 300.0})

        # 独立复算
        rec = recompute_from_fills(result.fills, initial_cash=1_000_000.0)
        self.assertAlmostEqual(rec.cash, result.final_cash, places=9)
        self.assertEqual(rec.positions, result.final_positions)
        self.assertTrue(reconcile(result)["passed"])


class InfoBarrierStrategy(Strategy):
    """记录每个时刻可见的 bar 数量，并尝试越权访问（通过普通属性无法拿到未来 bar）。"""

    name = "InfoBarrierStrategy"

    def __init__(self):
        self.visible = {}

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        self.visible[bar.timestamp] = [b.timestamp for b in ctx.bars("TEST")]


class TestInformationBoundary(unittest.TestCase):
    def test_strategy_sees_only_arrived_bars(self):
        strategy = InfoBarrierStrategy()
        engine = BacktestEngine(ManualDataFeed(make_bars()), strategy)
        engine.run()
        self.assertEqual(strategy.visible[10], [10])
        self.assertEqual(strategy.visible[20], [10, 20])
        self.assertEqual(strategy.visible[30], [10, 20, 30])
        self.assertEqual(strategy.visible[40], [10, 20, 30, 40])

    def test_pending_order_is_not_a_fill(self):
        # 限价 50 的买单永远不触达；挂单存在但账户分文未动
        class NeverFill(Strategy):
            name = "NeverFill"

            def on_bar(self, ctx: Context, bar: Bar) -> None:
                if bar.timestamp == 10:
                    ctx.submit_limit("TEST", OrderSide.BUY, 10.0, 50.0)

        engine = BacktestEngine(ManualDataFeed(make_bars()), NeverFill())
        result = engine.run()
        self.assertEqual(result.fills, [])
        self.assertEqual(result.final_cash, result.initial_cash)
        self.assertEqual(result.final_positions, {})
        self.assertEqual(result.orders[0].status, OrderStatus.OPEN)
        self.assertEqual(len(engine.broker.open_orders_snapshot()), 1)


class TestLedgerPersistence(unittest.TestCase):
    def test_save_run_files_and_reconciliation(self):
        import tempfile

        engine = BacktestEngine(
            ManualDataFeed(make_bars()),
            PartialFillStrategy(),
            commission=CommissionModel(rate=0.0003, min_commission=5.0),
        )
        bars = make_bars()
        result = engine.run()
        with tempfile.TemporaryDirectory() as tmp:
            out = save_run(
                result,
                tmp,
                market_bars=bars,
                ledger=result.ledger,
                open_orders=engine.broker.open_orders_snapshot(),
            )
            import json
            import os

            for name in (
                "params.json",
                "market_data.csv",
                "orders.csv",
                "fills.csv",
                "ledger.csv",
                "equity_curve.csv",
                "event_trace.csv",
                "open_orders.csv",
                "reconciliation.json",
            ):
                self.assertTrue(os.path.exists(os.path.join(out, name)), name)
            with open(os.path.join(out, "reconciliation.json"), encoding="utf-8") as f:
                recon = json.load(f)
            self.assertTrue(recon["passed"])


if __name__ == "__main__":
    unittest.main()
