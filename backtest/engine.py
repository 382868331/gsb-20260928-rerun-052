"""事件驱动回测引擎。

时间语义（关键）：

* 行情按 bar 逐根到达，策略在 on_bar 中只能看到当前这一根 bar。
* on_bar 内挂出的订单，submit_bar_idx 记为当前 bar，**下一根 bar 起**
  才允许成交，杜绝用当前 bar 的已知价格立即成交。
* 每根 bar 撮合前先统一处理撤单。因此“同一时刻撤单与成交相撞”时
  撤单优先：只要撤单请求在撮合前到达（含本根 bar 的 on_bar），
  该订单在本根 bar 不会成交。
* 同一根 bar 的撮合顺序：市价单优先，其余按 order_id FIFO；
  bar 的成交量按该顺序在订单间分配，单根 bar 量不够即为部分成交，
  剩余部分留待后续 bar。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .account import Account
from .models import Bar, Fill, Order, OrderStatus, OrderType, Side
from .strategy import Strategy


FeeFn = Callable[[int, float], float]


@dataclass
class Result:
    account: Account
    orders: dict[int, Order]
    fills: list[Fill]
    bars: list[Bar]

    @property
    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values()
                if o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)]


class _Context:
    """策略可见的操作面：只暴露已到达信息，账户不可直接修改。"""

    def __init__(self, engine: "BacktestEngine", bar_idx: int, bar: Bar):
        self._engine = engine
        self._bar_idx = bar_idx
        self._bar = bar

    def _submit(self, side: Side, symbol: str, qty: int,
                order_type: OrderType,
                limit_price: float | None) -> int:
        return self._engine._submit(
            side, symbol, qty, order_type, limit_price,
            submit_bar_idx=self._bar_idx, submit_ts=self._bar.ts,
        )

    def buy(self, symbol: str, qty: int,
            order_type: OrderType = OrderType.MARKET,
            limit_price: float | None = None) -> int:
        return self._submit(Side.BUY, symbol, qty, order_type, limit_price)

    def sell(self, symbol: str, qty: int,
             order_type: OrderType = OrderType.MARKET,
             limit_price: float | None = None) -> int:
        return self._submit(Side.SELL, symbol, qty, order_type, limit_price)

    def cancel(self, order_id: int) -> bool:
        return self._engine._request_cancel(order_id)

    def position(self, symbol: str) -> int:
        return self._engine.account.position(symbol)

    @property
    def cash(self) -> float:
        return self._engine.account.cash

    @property
    def open_orders(self) -> dict[int, Order]:
        return {oid: o for oid, o in self._engine.orders.items()
                if o.status in (OrderStatus.NEW,
                                OrderStatus.PARTIALLY_FILLED)}


class BacktestEngine:
    def __init__(self, bars: Iterable[Bar], strategy: Strategy,
                 initial_cash: float = 1_000_000.0,
                 fee_fn: FeeFn | None = None) -> None:
        self.bars = list(bars)
        if not self.bars:
            raise ValueError("行情为空")
        for a, b in zip(self.bars, self.bars[1:]):
            if b.ts <= a.ts:
                raise ValueError("行情时刻必须严格递增")
        self.strategy = strategy
        self.account = Account(initial_cash=initial_cash)
        self.fee_fn: FeeFn = fee_fn if fee_fn is not None else (lambda q, p: 0.0)
        self.orders: dict[int, Order] = {}
        self.fills: list[Fill] = []
        self._order_seq = 0
        self._fill_seq = 0

    def run(self) -> Result:
        self.strategy.on_start(_top_level_ctx(self))
        for i, bar in enumerate(self.bars):
            ctx = _Context(self, bar_idx=i, bar=bar)
            # 1) 策略只看到当前已到达的 bar；此阶段可挂单/撤单。
            self.strategy.on_bar(ctx, bar)
            # 2) 撤单先于撮合生效（同刻相撞，撤单优先）。
            self._apply_cancels(bar)
            # 3) 对本根 bar 撮合（订单最早来自上一根 bar）。
            self._match(bar_idx=i, bar=bar)
        self.strategy.on_finish(_top_level_ctx(self))
        return Result(self.account, self.orders, self.fills, self.bars)

    # ---- 下单 / 撤单 --------------------------------------------------

    def _submit(self, side: Side, symbol: str, qty: int,
                order_type: OrderType, limit_price: float | None,
                submit_bar_idx: int, submit_ts: int) -> int:
        if not isinstance(qty, int) or qty <= 0:
            raise ValueError("委托数量必须为正整数")
        if order_type is OrderType.LIMIT:
            if limit_price is None or limit_price <= 0:
                raise ValueError("限价单需要正的 limit_price")
        elif limit_price is not None:
            raise ValueError("市价单不能指定价格")
        self._order_seq += 1
        order = Order(
            order_id=self._order_seq, symbol=symbol, side=side, qty=qty,
            order_type=order_type, submit_ts=submit_ts,
            submit_bar_idx=submit_bar_idx, limit_price=limit_price,
        )
        self.orders[order.order_id] = order
        return order.order_id

    def _request_cancel(self, order_id: int) -> bool:
        order = self.orders.get(order_id)
        if order is None:
            return False
        if order.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
            order.cancel_requested = True
            return True
        return False

    def _apply_cancels(self, bar: Bar) -> None:
        for order in self.orders.values():
            if (order.cancel_requested and
                    order.status in (OrderStatus.NEW,
                                     OrderStatus.PARTIALLY_FILLED)):
                order.status = OrderStatus.CANCELED
                order.cancel_requested = False

    # ---- 撮合 ---------------------------------------------------------

    def _match(self, bar_idx: int, bar: Bar) -> None:
        active = [o for o in self.orders.values()
                  if o.submit_bar_idx < bar_idx
                  and o.symbol == bar.symbol
                  and o.status in (OrderStatus.NEW,
                                   OrderStatus.PARTIALLY_FILLED)]
        # 市价优先，其余按 FIFO。
        active.sort(key=lambda o: (o.order_type is not OrderType.MARKET,
                                   o.order_id))
        volume_left = bar.volume
        sellable = self.account.position(bar.symbol)
        for order in active:
            price = self._fill_price(order, bar)
            if price is None:
                continue
            want = min(order.remaining, max(volume_left, 0))
            if want <= 0:
                continue  # 本根 bar 成交量耗尽，订单留待后续 bar
            if order.side is Side.BUY:
                qty = self._affordable_qty(want, price)
                if qty <= 0 and order.filled_qty == 0:
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = "现金不足"
                    continue
                if qty <= 0:
                    continue  # 已部分成交、现金耗尽，剩余继续挂着
            else:
                qty = min(want, max(sellable, 0))
                if qty <= 0 and order.filled_qty == 0:
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = "持仓不足"
                    continue
                if qty <= 0:
                    continue
                sellable -= qty
            self._make_fill(order, qty, price, bar, bar_idx)
            volume_left -= qty

    @staticmethod
    def _fill_price(order: Order, bar: Bar) -> float | None:
        if order.order_type is OrderType.MARKET:
            return bar.open
        assert order.limit_price is not None
        if order.side is Side.BUY:
            # 开盘即优于限价按开盘；盘中触到限价则按限价成交。
            if order.limit_price >= bar.low:
                return min(bar.open, order.limit_price)
        else:
            if order.limit_price <= bar.high:
                return max(bar.open, order.limit_price)
        return None

    def _affordable_qty(self, want: int, price: float) -> int:
        cash = self.account.cash
        qty = want
        while qty > 0 and qty * price + self.fee_fn(qty, price) > cash + 1e-9:
            qty -= 1
        return qty

    def _make_fill(self, order: Order, qty: int, price: float,
                   bar: Bar, bar_idx: int) -> None:
        fee = self.fee_fn(qty, price)
        self._fill_seq += 1
        fill = Fill(
            fill_id=self._fill_seq, order_id=order.order_id,
            symbol=order.symbol, side=order.side, qty=qty,
            price=price, fee=fee, ts=bar.ts, bar_idx=bar_idx,
        )
        self.account.apply_fill(fill)
        self.fills.append(fill)
        order.filled_qty += qty
        order.status = (OrderStatus.FILLED if order.remaining == 0
                        else OrderStatus.PARTIALLY_FILLED)


def _top_level_ctx(engine: BacktestEngine) -> _Context:
    # on_start/on_finish 不在任何 bar 上：用 -1，挂出的单第一根 bar 即可成交。
    first = engine.bars[0]
    return _Context(engine, bar_idx=-1, bar=Bar(
        first.symbol, ts=0, open=first.open, high=first.high,
        low=first.low, close=first.close, volume=0))
