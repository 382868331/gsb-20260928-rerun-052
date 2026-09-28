"""策略基类：策略只通过上下文下单/撤单，不直接碰账户。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .models import Bar, OrderType, Side


class StrategyContext(Protocol):
    def buy(self, symbol: str, qty: int,
            order_type: OrderType = OrderType.MARKET,
            limit_price: float | None = None) -> int: ...

    def sell(self, symbol: str, qty: int,
             order_type: OrderType = OrderType.MARKET,
             limit_price: float | None = None) -> int: ...

    def cancel(self, order_id: int) -> bool: ...

    def position(self, symbol: str) -> int: ...

    @property
    def cash(self) -> float: ...

    @property
    def open_orders(self) -> dict[int, Any]: ...


class Strategy:
    """新策略继承本类并实现 on_bar；on_start/on_finish 可选。"""

    def on_start(self, ctx: StrategyContext) -> None:
        pass

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        raise NotImplementedError

    def on_finish(self, ctx: StrategyContext) -> None:
        pass
