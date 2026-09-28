"""回测引擎第一版的关键行为测试。"""

from backtest.account import replay_ledger
from backtest.engine import BacktestEngine
from backtest.fees import FixedRateFee
from backtest.models import Bar, OrderStatus, OrderType
from backtest.strategy import Strategy


FEE = FixedRateFee(rate=0.0, min_fee=1.0)


class ScriptedStrategy(Strategy):
    """按 (ts -> 动作列表) 脚本驱动，便于精确构造场景。"""

    def __init__(self, actions: dict[int, list]):
        self.actions = actions
        self.ids: dict[str, int] = {}

    def on_bar(self, ctx, bar) -> None:
        for act in self.actions.get(bar.ts, []):
            kind = act[0]
            if kind == "buy_limit":
                _, tag, qty, px = act
                self.ids[tag] = ctx.buy("T", qty, OrderType.LIMIT,
                                        limit_price=px)
            elif kind == "sell_market":
                _, tag, qty = act
                self.ids[tag] = ctx.sell("T", qty, OrderType.MARKET)
            elif kind == "buy_market":
                _, tag, qty = act
                self.ids[tag] = ctx.buy("T", qty, OrderType.MARKET)
            elif kind == "cancel":
                ctx.cancel(self.ids[act[1]])


def make_bars3(volumes=(1000, 1000, 1000)):
    return [Bar("T", i, 100, 101, 99, 100, v)
            for i, v in enumerate(volumes, start=1)]


def run(actions, fee=FEE, cash=1_000_000, bars=None):
    strat = ScriptedStrategy(actions)
    result = BacktestEngine(bars or make_bars3(), strat,
                            initial_cash=cash, fee_fn=fee).run()
    return result, strat


def test_order_not_filled_on_submit_bar():
    """挂单不等于成交：本根 bar 挂的单最早下一根 bar 成交。"""
    result, strat = run({1: [("buy_limit", "a", 100, 99.0)]})
    order = result.orders[strat.ids["a"]]
    assert order.status is OrderStatus.FILLED
    assert [f.bar_idx for f in result.fills] == [1]
    assert result.fills[0].price == 99.0


def test_partial_fill_across_bars():
    bars = make_bars3((30, 50, 100))
    strat = ScriptedStrategy({1: [("buy_limit", "a", 100, 99.0)]})
    result = BacktestEngine(bars, strat, initial_cash=1_000_000,
                            fee_fn=FEE).run()
    qtys = [f.qty for f in result.fills]
    # bar1 挂单，bar2 起才成交：bar2 给 50，bar3 给剩余 50
    assert [f.bar_idx for f in result.fills] == [1, 2]
    assert qtys == [50, 50]
    order = result.orders[strat.ids["a"]]
    assert order.status is OrderStatus.FILLED
    assert order.filled_qty == 100


def test_cancel_wins_over_fill_at_same_moment():
    """同一时刻撤单与成交相撞：撤单优先，订单零成交。

    ts=2 挂限价买单（在 ts=3 本可成交）；ts=3 的 on_bar 同时看到行情并撤单，
    撤单在 ts=3 撮合前生效，因此 ts=3 不产生任何成交。
    """
    strat = ScriptedStrategy({
        2: [("buy_limit", "a", 100, 99.0)],
        3: [("cancel", "a")],
    })
    result, strat = run({2: [("buy_limit", "a", 100, 99.0)],
                         3: [("cancel", "a")]},
                        bars=make_bars3())
    order = result.orders[strat.ids["a"]]
    assert order.status is OrderStatus.CANCELED
    assert order.filled_qty == 0
    assert result.fills == []


def test_cancel_after_partial_fill_keeps_remainder_canceled():
    bars = [Bar("T", 1, 100, 101, 99, 100, 1000),
            Bar("T", 2, 100, 101, 99, 100, 30),
            Bar("T", 3, 100, 101, 99, 100, 1000)]
    strat = ScriptedStrategy({
        1: [("buy_limit", "a", 100, 99.0)],
        3: [("cancel", "a")],
    })
    result = BacktestEngine(bars, strat, initial_cash=1_000_000,
                            fee_fn=FEE).run()
    order = result.orders[strat.ids["a"]]
    assert order.status is OrderStatus.CANCELED
    # bar2 部分成交 30，bar3 on_bar 撤单先于 bar3 撮合，剩余 70 被撤
    assert order.filled_qty == 30
    assert [f.qty for f in result.fills] == [30]


def test_open_orders_preserved_at_end():
    """结束时未成交单不消失，可单独枚举。"""
    result, strat = run({3: [("buy_limit", "a", 100, 50.0)]})
    assert len(result.open_orders) == 1
    assert result.open_orders[0].order_id == strat.ids["a"]
    assert result.fills == []


def test_fee_and_cash_and_replay():
    """手续费入账，现金/持仓可由流水独立复算。"""
    result, _ = run({1: [("buy_limit", "a", 100, 100.0)]})
    fill = result.fills[0]
    assert fill.price == 100.0
    assert fill.fee == 1.0  # 最低手续费
    assert result.account.cash == 1_000_000 - 100 * 100 - 1.0
    assert result.account.position("T") == 100
    cash, positions = replay_ledger(result.account.ledger)
    assert abs(cash - result.account.cash) < 1e-9
    assert positions == {"T": 100}


def test_sell_cannot_exceed_position():
    """不允许裸卖空：卖单超过持仓的部分被拒，不成交超量。"""
    result, strat = run({1: [("sell_market", "a", 100)]})
    order = result.orders[strat.ids["a"]]
    assert order.status is OrderStatus.REJECTED
    assert result.fills == []
    assert result.account.position("T") == 0


def test_buy_limited_by_cash():
    """现金不足时按可负担数量部分成交，而非透支。"""
    result, strat = run({1: [("buy_limit", "a", 100, 100.0)]},
                        cash=5_000)
    # 5000 元可买 49 股（49*100+1<=5000）
    assert result.fills[0].qty == 49
    assert result.account.cash >= 0
    assert result.orders[strat.ids["a"]].status is (
        OrderStatus.PARTIALLY_FILLED)
