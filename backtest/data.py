"""人工生成的行情数据，不接任何外部数据源。

行情是一份按时刻排序的 Bar 表；引擎每次只把“已到达”的一根 bar
交给策略，策略无法看到未来。
"""

from __future__ import annotations

from .models import Bar


def make_bars(
    bars: list[tuple[int, float, float, float, float, int]],
    symbol: str = "TEST",
) -> list[Bar]:
    """由 (ts, open, high, low, close, volume) 元组列表构造 bar。"""
    return [Bar(symbol, ts, o, h, lo, c, v)
            for (ts, o, h, lo, c, v) in bars]


def deterministic_series(
    n: int = 20,
    start: float = 100.0,
    symbol: str = "DEMO",
) -> list[Bar]:
    """生成一段确定性的合成行情，方便手工核对结果。

    价格在 start 附近做锯齿波动，成交量固定，保证同一结果可重复。
    """
    out: list[Bar] = []
    prev_close = start
    for i in range(n):
        ts = i + 1
        direction = 1 if i % 2 == 0 else -1
        o = prev_close
        c = round(start + direction * (2.0 + 0.1 * (i % 4)), 2)
        h = round(max(o, c) + 0.5, 2)
        lo = round(min(o, c) - 0.5, 2)
        v = 1000
        out.append(Bar(symbol, ts, o, h, lo, c, v))
        prev_close = c
    return out
