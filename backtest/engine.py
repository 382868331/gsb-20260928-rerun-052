"""回测引擎：基于优先级事件队列逐 bar 推进。

每个行情时刻严格按以下顺序处理（由 backtest.events.Priority 定义）：

1. MARKET   bar 到达，记录行情；
2. STRATEGY 策略 on_bar 决策，可提交撤单请求与新委托；
3. CANCEL   处理撤单请求（撤单先于成交；见 cancel_before_match）；
4. SUBMIT   新委托登记入订单簿（本根 bar 不参与撮合）；
5. MATCH    对存量订单撮合，可能产生多笔部分成交；
6. FILL     逐笔成交回报，更新账户并回调策略 on_fill。

因此同一时刻撤单与成交相撞时：撤单请求先于撮合执行，
被撤订单不会在该时刻成交。该顺序可通过参数显式配置并记入运行参数。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Dict, List, Optional

from .account import Account
from .broker import Broker, CommissionModel
from .data import DataFeed, SyntheticDataFeed
from .events import (
    CancelRequest,
    Event,
    MatchBatch,
    Priority,
    SubmitRequest,
    make_event,
)
from .models import Bar, Fill, Order, OrderSide, OrderStatus, OrderType
from .strategy import Context, Strategy


@dataclass
class EventTrace:
    seq: int
    timestamp: int
    priority: int
    kind: str
    detail: str

    def to_row(self) -> dict:
        return self.__dict__.copy()


@dataclass
class EquityPoint:
    bar_timestamp: int
    cash: float
    market_value: float
    equity: float

    def to_row(self) -> dict:
        return self.__dict__.copy()


@dataclass
class EngineResult:
    initial_cash: float
    final_cash: float
    final_positions: Dict[str, float]
    fills: List[Fill]
    orders: List[Order]
    ledger: List[object]
    event_trace: List[EventTrace]
    equity_curve: List[EquityPoint]
    commission_total: float
    params: dict


class BacktestEngine:
    def __init__(
        self,
        data_feed: DataFeed,
        strategy: Strategy,
        initial_cash: float = 1_000_000.0,
        commission: Optional[CommissionModel] = None,
        max_participation: float = 0.1,
        cancel_before_match: bool = True,
        run_tag: str = "run",
    ):
        """cancel_before_match=True 时撤单先于同刻撮合；
        False 时撮合先于撤单（用于验证相撞顺序的可配置性）。
        """
        self.data_feed = data_feed
        self.strategy = strategy
        self.broker = Broker(commission=commission, max_participation=max_participation)
        self.account = Account(initial_cash=initial_cash)
        self.cancel_before_match = cancel_before_match
        self.run_tag = run_tag

        self._queue: List[Event] = []
        self._seq = 0
        self._next_order_id = 1
        self._fills: List[Fill] = []
        self._trace: List[EventTrace] = []
        self._equity_curve: List[EquityPoint] = []
        self._last_prices: Dict[str, float] = {}
        self.ctx: Optional[Context] = None

    def _push(self, timestamp: int, priority: int, kind: str, payload=None) -> None:
        self._seq += 1
        heapq.heappush(
            self._queue,
            make_event(timestamp, priority, self._seq, kind, payload),
        )

    def _record(self, event: Event, detail: str = "") -> None:
        self._trace.append(
            EventTrace(
                seq=event.seq,
                timestamp=event.timestamp,
                priority=event.priority,
                kind=event.kind,
                detail=detail,
            )
        )

    # ----- 策略请求入口 -----

    def _submit(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        limit_price: Optional[float],
    ) -> SubmitRequest:
        order = Order(
            order_id=self._next_order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            submitted_at=self.ctx.now if self.ctx is not None else 0,
            limit_price=limit_price,
        )
        self._next_order_id += 1
        req = SubmitRequest(order=order)
        self._push(self.ctx.now, Priority.SUBMIT, "submit", req)
        return req

    def _cancel(self, order_id: int) -> CancelRequest:
        req = CancelRequest(order_id=order_id, requested_at=self.ctx.now)
        priority = Priority.CANCEL if self.cancel_before_match else Priority.MATCH + 5
        self._push(self.ctx.now, priority, "cancel", req)
        return req

    # ----- 主循环 -----

    def run(self) -> EngineResult:
        self.ctx = Context(
            broker=self.broker,
            submit=self._submit,
            cancel=self._cancel,
            get_position=self.account.position,
        )
        for bar in self.data_feed.bars():
            self._push(bar.timestamp, Priority.MARKET, "market", bar)
            self._push(bar.timestamp, Priority.STRATEGY, "strategy", bar)
            match_priority = Priority.MATCH if self.cancel_before_match else Priority.CANCEL - 1
            self._push(bar.timestamp, match_priority, "match", MatchBatch(bar=bar))

        while self._queue:
            event = heapq.heappop(self._queue)
            handler = {
                "market": self._on_market,
                "strategy": self._on_strategy,
                "cancel": self._on_cancel,
                "submit": self._on_submit,
                "match": self._on_match,
                "fill": self._on_fill,
            }[event.kind]
            handler(event)

            # 该时刻最后一个事件处理完毕（下一个事件属于更晚的 bar）即结算本根 bar。
            if not self._queue or self._queue[0].timestamp != event.timestamp:
                bar = self.ctx.current_bar
                if bar is not None and bar.timestamp == event.timestamp:
                    self._on_bar_settled(bar)

        return self._build_result()

    def _on_market(self, event: Event) -> None:
        bar: Bar = event.payload
        self._last_prices[bar.symbol] = bar.close
        # 新一根 bar 开始：上一根 bar 提交的订单自本根起参与撮合。
        # 本根 bar 策略随后提交的新单会再次进入该集合，故不参与本根撮合。
        self.broker.on_bar_start()
        self._record(event, f"{bar.symbol}@{bar.timestamp} close={bar.close} volume={bar.volume}")

    def _on_strategy(self, event: Event) -> None:
        bar: Bar = event.payload
        self._record(event, f"on_bar {bar.symbol}@{bar.timestamp}")
        self.ctx._on_bar(bar)
        self.strategy.on_bar(self.ctx, bar)

    def _on_cancel(self, event: Event) -> None:
        req: CancelRequest = event.payload
        ok, reason = self.broker.cancel(req.order_id, event.timestamp)
        detail = f"cancel order={req.order_id} ok={ok}"
        if reason:
            detail += f" reason={reason}"
        self._record(event, detail)

    def _on_submit(self, event: Event) -> None:
        req: SubmitRequest = event.payload
        order = req.order
        reason = self.broker.register(order)
        detail = (
            f"submit order={order.order_id} {order.side.value} "
            f"{order.order_type.value} qty={order.quantity} price={order.limit_price} accepted"
        )
        if reason:
            order.status = OrderStatus.REJECTED
            order.reject_reason = reason
            detail = f"submit order={order.order_id} rejected reason={reason}"
        self._record(event, detail)

    def _on_match(self, event: Event) -> None:
        batch: MatchBatch = event.payload
        bar = batch.bar
        fills = self.broker.match(bar, event.timestamp)
        for fill in fills:
            self._record(
                event,
                f"matched order={fill.order_id} qty={fill.quantity} price={fill.price} fee={fill.commission:.4f}",
            )
            self._push(event.timestamp, Priority.FILL, "fill", fill)
        self._record(event, f"bar {bar.symbol}@{bar.timestamp} matched {len(fills)} fill(s)")

    def _on_bar_settled(self, bar: Bar) -> None:
        """该时刻所有事件处理完毕后记录权益点。"""
        market_value = self.account.market_value({bar.symbol: bar.close})
        self._equity_curve.append(
            EquityPoint(
                bar_timestamp=bar.timestamp,
                cash=self.account.cash,
                market_value=market_value,
                equity=self.account.cash + market_value,
            )
        )

    def _on_fill(self, event: Event) -> None:
        fill: Fill = event.payload
        self.account.apply_fill(fill)
        self._fills.append(fill)
        self._record(event, f"fill order={fill.order_id} qty={fill.quantity} cash={self.account.cash:.2f}")
        self.strategy.on_fill(self.ctx, fill)

    def _build_result(self) -> EngineResult:
        orders = self.broker.all_orders()
        params = {
            "run_tag": self.run_tag,
            "initial_cash": self.account.initial_cash,
            "cancel_before_match": self.cancel_before_match,
            "broker": self.broker.params(),
            "data": self.data_feed.params() if isinstance(self.data_feed, SyntheticDataFeed) else {"kind": type(self.data_feed).__name__},
            "strategy": self.strategy.params(),
        }
        return EngineResult(
            initial_cash=self.account.initial_cash,
            final_cash=self.account.cash,
            final_positions=dict(self.account.positions),
            fills=self._fills,
            orders=orders,
            ledger=list(self.account.ledger),
            event_trace=self._trace,
            equity_curve=self._equity_curve,
            commission_total=self.account.commission_total,
            params=params,
        )
