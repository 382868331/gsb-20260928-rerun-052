"""策略接口与信息隔离。

策略只能通过 Context 获取信息与下单：
- 只能看到当前已到达及更早的 bar，无法窥视未来行情；
- 只能查询订单状态、提交/撤销委托，不能直接操作账户与订单簿；
- 策略请求通过事件队列在同一时刻按固定优先级处理（撤单先于撮合）。
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

from .events import CancelRequest, SubmitRequest
from .models import Bar, Order, OrderSide, OrderType


class Context:
    """暴露给策略的受限上下文。"""

    def __init__(
        self,
        broker,
        submit,
        cancel,
        get_position=None,
        history_size: Optional[int] = None,
    ):
        self._broker = broker
        self._submit = submit
        self._cancel = cancel
        self._get_position = get_position
        self._history_size = history_size
        self._history: Dict[str, Deque[Bar]] = defaultdict(lambda: deque(maxlen=history_size))
        self.now: Optional[int] = None
        self.current_bar: Optional[Bar] = None

    def _on_bar(self, bar: Bar) -> None:
        self.now = bar.timestamp
        self.current_bar = bar
        self._history[bar.symbol].append(bar)

    # ----- 行情信息（仅含已到达的 bar）-----

    def bars(self, symbol: str) -> List[Bar]:
        """返回该标的截至当前时刻（含）的 bar 列表，按时间升序。"""
        return list(self._history.get(symbol, deque()))

    def closes(self, symbol: str) -> List[float]:
        return [b.close for b in self.bars(symbol)]

    # ----- 订单信息（只读视图）-----

    def get_order(self, order_id: int) -> Optional[Order]:
        return self._broker.get_order(order_id)

    def active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        return self._broker.active_orders(symbol)

    def position_view(self, symbol: str) -> float:
        """只读持仓视图；引擎未提供时始终返回 0。"""
        if self._get_position is None:
            return 0.0
        return self._get_position(symbol)

    # ----- 委托请求 -----

    def submit_limit(
        self, symbol: str, side: OrderSide, quantity: float, limit_price: float
    ) -> SubmitRequest:
        order = self._submit(
            symbol=symbol,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            limit_price=limit_price,
        )
        return order

    def submit_market(self, symbol: str, side: OrderSide, quantity: float) -> SubmitRequest:
        return self._submit(
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
            limit_price=None,
        )

    def cancel_order(self, order_id: int) -> CancelRequest:
        return self._cancel(order_id)


class Strategy:
    """策略基类。新策略继承本类并实现 on_bar 即可，账户与撮合无需改动。"""

    name = "Strategy"

    def on_bar(self, ctx: Context, bar: Bar) -> None:  # pragma: no cover - 接口约定
        raise NotImplementedError

    def on_fill(self, ctx: Context, fill) -> None:
        """成交回报回调，默认不做任何事，可按需重写。"""

    def params(self) -> dict:
        return {"name": self.name}


class MACrossStrategy(Strategy):
    """演示策略：短均线上穿长均线买入，下穿卖出（限价单挂在收盘价附近）。

    第一版仅用于演示事件推进与账户分离，不构成任何真实交易建议。
    """

    name = "MACrossStrategy"

    def __init__(self, symbol: str = "TEST", fast: int = 5, slow: int = 20, quantity: float = 100.0):
        if fast >= slow:
            raise ValueError("fast 必须小于 slow")
        self.symbol = symbol
        self.fast = fast
        self.slow = slow
        self.quantity = quantity
        self._last_signal = 0

    def params(self) -> dict:
        return {
            "name": self.name,
            "symbol": self.symbol,
            "fast": self.fast,
            "slow": self.slow,
            "quantity": self.quantity,
        }

    @staticmethod
    def _mean(values: List[float]) -> float:
        return sum(values) / len(values)

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        if bar.symbol != self.symbol:
            return
        closes = ctx.closes(self.symbol)
        if len(closes) < self.slow:
            return
        fast_ma = self._mean(closes[-self.fast :])
        slow_ma = self._mean(closes[-self.slow :])
        signal = 1 if fast_ma > slow_ma else (-1 if fast_ma < slow_ma else 0)
        if signal == 1 and self._last_signal <= 0 and ctx.position_view(self.symbol) <= 0:
            ctx.submit_limit(self.symbol, OrderSide.BUY, self.quantity, bar.close)
        elif signal == -1 and self._last_signal >= 0 and ctx.position_view(self.symbol) >= 0:
            ctx.submit_limit(self.symbol, OrderSide.SELL, self.quantity, bar.close)
        self._last_signal = signal
