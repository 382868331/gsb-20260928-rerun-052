"""示例策略：均线交叉，展示新策略如何复用同一账户与引擎。"""

from __future__ import annotations

from collections import deque

from .models import Bar, OrderType
from .strategy import Strategy, StrategyContext


class MovingAverageCross(Strategy):
    """短均线上穿长均线买入一手，下穿卖出一手（人工合成数据演示用）。"""

    def __init__(self, symbol: str = "DEMO", short: int = 3,
                 long: int = 5, qty: int = 100) -> None:
        if short >= long:
            raise ValueError("short 必须小于 long")
        self.symbol = symbol
        self.short = short
        self.long = long
        self.qty = qty
        self._closes: deque[float] = deque(maxlen=long)
        self._prev_short = self._prev_long = None

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        self._closes.append(bar.close)
        if len(self._closes) < self.long:
            return
        closes = list(self._closes)
        ma_short = sum(closes[-self.short:]) / self.short
        ma_long = sum(closes) / self.long
        if self._prev_short is not None:
            crossed_up = (self._prev_short <= self._prev_long
                          and ma_short > ma_long)
            crossed_down = (self._prev_short >= self._prev_long
                            and ma_short < ma_long)
            if crossed_up and ctx.position(self.symbol) == 0:
                ctx.buy(self.symbol, self.qty, OrderType.LIMIT,
                        limit_price=bar.close + 1.0)
            elif crossed_down and ctx.position(self.symbol) > 0:
                ctx.sell(self.symbol, self.qty, OrderType.MARKET)
        self._prev_short, self._prev_long = ma_short, ma_long
