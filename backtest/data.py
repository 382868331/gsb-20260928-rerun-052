"""行情数据源。全部为人工生成数据，不接任何券商或外部行情。

提供两种数据源：
- SyntheticDataFeed: 固定随机种子的可复现合成行情（随机游走 + 正弦波动）
- ManualDataFeed:    手工构造 bar 列表，用于精确构造测试场景

数据按 (timestamp, symbol) 有序产出，引擎据此推进事件。
"""

from __future__ import annotations

import math
import random
from typing import Iterable, Iterator, List

from .models import Bar


class DataFeed:
    """数据源基类。子类只需实现 bars()，按时间非降序产出 bar。"""

    def bars(self) -> Iterator[Bar]:  # pragma: no cover - 接口约定
        raise NotImplementedError

    def all_bars(self) -> List[Bar]:
        return list(self.bars())


class ManualDataFeed(DataFeed):
    """手工构造的数据源，直接给出 bar 列表（会按时间排序后使用）。"""

    def __init__(self, bars: Iterable[Bar]):
        self._bars = sorted(bars, key=lambda b: (b.timestamp, b.symbol))

    def bars(self) -> Iterator[Bar]:
        return iter(self._bars)


class SyntheticDataFeed(DataFeed):
    """固定随机种子的合成 OHLCV 行情，保证每次运行可复现。

    参数与随机种子会被引擎记入运行参数文件。
    """

    def __init__(
        self,
        symbol: str = "TEST",
        n_bars: int = 60,
        start_price: float = 100.0,
        bar_interval: int = 1,
        start_timestamp: int = 1,
        volatility: float = 0.02,
        drift: float = 0.0005,
        wave_amplitude: float = 0.01,
        wave_period: int = 20,
        base_volume: float = 10000.0,
        seed: int = 42,
    ):
        if n_bars <= 0:
            raise ValueError("n_bars 必须为正")
        self.symbol = symbol
        self.n_bars = n_bars
        self.start_price = start_price
        self.bar_interval = bar_interval
        self.start_timestamp = start_timestamp
        self.volatility = volatility
        self.drift = drift
        self.wave_amplitude = wave_amplitude
        self.wave_period = wave_period
        self.base_volume = base_volume
        self.seed = seed

    def params(self) -> dict:
        return {
            "kind": "SyntheticDataFeed",
            "symbol": self.symbol,
            "n_bars": self.n_bars,
            "start_price": self.start_price,
            "bar_interval": self.bar_interval,
            "start_timestamp": self.start_timestamp,
            "volatility": self.volatility,
            "drift": self.drift,
            "wave_amplitude": self.wave_amplitude,
            "wave_period": self.wave_period,
            "base_volume": self.base_volume,
            "seed": self.seed,
        }

    def bars(self) -> Iterator[Bar]:
        rng = random.Random(self.seed)
        price = self.start_price
        for i in range(self.n_bars):
            wave = math.sin(2.0 * math.pi * i / self.wave_period) * self.wave_amplitude
            close = max(0.01, price * (1.0 + self.drift + wave + rng.gauss(0.0, self.volatility)))
            open_ = price
            high = max(open_, close) * (1.0 + abs(rng.gauss(0.0, self.volatility * 0.4)))
            low = min(open_, close) * (1.0 - abs(rng.gauss(0.0, self.volatility * 0.4)))
            volume = max(1.0, self.base_volume * (1.0 + rng.uniform(-0.3, 0.3)))
            timestamp = self.start_timestamp + i * self.bar_interval
            yield Bar(
                symbol=self.symbol,
                timestamp=timestamp,
                open=round(open_, 4),
                high=round(high, 4),
                low=round(low, 4),
                close=round(close, 4),
                volume=round(volume, 2),
            )
            price = close
