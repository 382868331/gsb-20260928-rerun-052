"""事件定义。

回测主循环是一个严格按 (timestamp, priority, seq) 排序的事件队列。
同一时刻的处理顺序完全由 priority 决定，从而让
"撤单与成交相撞" 的结果可复现、可测试。

优先级约定（数字越小越早处理）：
10 MARKET      行情到达，行情先发布，策略只能基于已到达的信息决策
20 STRATEGY    策略回调 on_bar，在本事件内产生下单/撤单请求
30 CANCEL      撤单请求，先于撮合处理（撤单先于成交，可配置反转，见 engine）
40 SUBMIT      新委托入账，新单不参与本根 bar 的撮合，避免使用当根未来信息
50 MATCH       撮合：对撤单/新单处理完毕后仍有效的存量订单撮合
60 FILL        成交回报：撮合结果通知账户与策略
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from .models import Bar, Fill, Order


class Priority(IntEnum):
    MARKET = 10
    FX = 12
    CORPORATE_ACTION = 15
    STRATEGY = 20
    CANCEL = 30
    SUBMIT = 40
    MATCH = 50
    FILL = 60


@dataclass(order=True)
class Event:
    """事件队列元素。seq 为全局自增序号，保证同刻同优先级确定序。"""

    timestamp: int
    priority: int
    seq: int
    kind: str = field(compare=False)
    payload: Any = field(default=None, compare=False)


@dataclass(frozen=True)
class CancelRequest:
    order_id: int
    requested_at: int


@dataclass(frozen=True)
class SubmitRequest:
    order: Order


@dataclass(frozen=True)
class MatchBatch:
    """一次撮合的输入：当前 bar（成交结果 payload 用 Fill 列表）。"""

    bar: Bar


@dataclass(frozen=True)
class FxRequest:
    timestamp: int
    from_currency: str
    to_currency: str
    amount: float
    rate: float


def make_event(timestamp: int, priority: int, seq: int, kind: str, payload: Any = None) -> Event:
    return Event(timestamp=timestamp, priority=priority, seq=seq, kind=kind, payload=payload)


def fill_event(timestamp: int, seq: int, fill: Fill) -> Event:
    return make_event(timestamp, Priority.FILL, seq, "fill", fill)
