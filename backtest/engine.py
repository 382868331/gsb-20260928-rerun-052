"""按确定性事件顺序推进的多币种回测引擎。"""

from __future__ import annotations

import heapq
import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional

from .account import Account
from .broker import Broker, CommissionModel
from .data import DataFeed, SyntheticDataFeed
from .events import (
    CancelRequest,
    Event,
    FxRequest,
    MatchBatch,
    Priority,
    SubmitRequest,
    make_event,
)
from .models import Bar, CorporateAction, CorporateActionType, Fill, Order, OrderSide, OrderStatus, OrderType
from .strategy import Context, Strategy


@dataclass
class EventTrace:
    seq: int
    timestamp: int
    priority: int
    kind: str
    detail: str

    def to_row(self): return self.__dict__.copy()


@dataclass
class EquityPoint:
    bar_timestamp: int
    cash: float
    cash_balances: Dict[str, float]
    market_value: float
    equity: float

    def to_row(self): return self.__dict__.copy()


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
    commission_total: Dict[str, float]
    params: dict
    initial_balances: Dict[str, float]
    final_cash_balances: Dict[str, float]


class BacktestEngine:
    def __init__(self, data_feed: DataFeed, strategy: Strategy,
                 initial_cash: float | Mapping[str, float] = 1_000_000.0,
                 commission: Optional[CommissionModel] = None,
                 max_participation: float = 0.1,
                 cancel_before_match: bool = True,
                 run_tag: str = "run",
                 base_currency: str = "USD",
                 corporate_actions: Optional[Iterable[CorporateAction]] = None,
                 fx_rates: Optional[Mapping[str, float]] = None):
        self.data_feed = data_feed
        self.strategy = strategy
        self.base_currency = base_currency
        self.broker = Broker(commission=commission, max_participation=max_participation)
        if isinstance(initial_cash, dict):
            self.account = Account(dict(initial_cash), base_currency=base_currency)
        else:
            self.account = Account(initial_cash, base_currency=base_currency)
        self.cancel_before_match = cancel_before_match
        self.run_tag = run_tag
        self.actions = sorted(corporate_actions or [], key=lambda a: (a.effective_at, a.action_type.value, a.symbol))
        self.fx_rates = dict(fx_rates or {})
        self._queue: List[Event] = []
        self._seq = 0
        self._next_order_id = 1
        self._fills: List[Fill] = []
        self._trace: List[EventTrace] = []
        self._equity_curve: List[EquityPoint] = []
        self._last_prices: Dict[str, float] = {}
        self._last_timestamp: Optional[int] = None
        self.ctx: Optional[Context] = None

    def _push(self, timestamp, priority, kind, payload=None):
        self._seq += 1
        heapq.heappush(self._queue, make_event(timestamp, priority, self._seq, kind, payload))

    def _record(self, event, detail=""):
        self._trace.append(EventTrace(event.seq, event.timestamp, event.priority, event.kind, detail))

    def _submit(self, symbol, side, order_type, quantity, limit_price, currency="USD"):
        price = limit_price
        if price is None:
            price = self._last_prices.get(symbol, 0.0)
        order = Order(self._next_order_id, symbol, side, order_type, quantity,
                      self.ctx.now, limit_price=limit_price, currency=currency,
                      estimated_price=price or 0.0)
        self._next_order_id += 1
        req = SubmitRequest(order=order)
        self._push(self.ctx.now, Priority.SUBMIT, "submit", req)
        return req

    def _cancel(self, order_id):
        req = CancelRequest(order_id, self.ctx.now)
        priority = Priority.CANCEL if self.cancel_before_match else Priority.MATCH + 5
        self._push(self.ctx.now, priority, "cancel", req)
        return req

    def _convert(self, from_currency, to_currency, amount, rate=None):
        if rate is None:
            rate = self.fx_rates.get(f"{from_currency}/{to_currency}") or self.fx_rates.get(f"{to_currency}/{from_currency}")
            if rate is None:
                raise ValueError("缺少汇兑汇率")
            if f"{to_currency}/{from_currency}" in self.fx_rates:
                rate = 1.0 / self.fx_rates[f"{to_currency}/{from_currency}"]
        req = FxRequest(self.ctx.now, from_currency, to_currency, amount, rate)
        self._push(self.ctx.now, Priority.FX, "fx", req)
        return req

    def run(self, until_timestamp: Optional[int] = None):
        self.ctx = Context(self.broker, self._submit, self._cancel, self.account.position,
                           convert=self._convert, account=self.account, base_currency=self.base_currency)
        bars = list(self.data_feed.bars())
        self._rebuild_history(bars)
        start_after = self._last_timestamp
        for bar in bars:
            if start_after is not None and bar.timestamp <= start_after:
                continue
            self._push(bar.timestamp, Priority.MARKET, "market", bar)
            self._push(bar.timestamp, Priority.STRATEGY, "strategy", bar)
            match_priority = Priority.MATCH if self.cancel_before_match else Priority.CANCEL - 1
            self._push(bar.timestamp, match_priority, "match", MatchBatch(bar))
        for action in self.actions:
            if start_after is not None and action.effective_at <= start_after:
                continue
            self._push(action.effective_at, Priority.CORPORATE_ACTION, "corporate_action", action)
        while self._queue:
            event = heapq.heappop(self._queue)
            if until_timestamp is not None and event.timestamp > until_timestamp:
                heapq.heappush(self._queue, event)
                break
            handler = {
                "market": self._on_market,
                "strategy": self._on_strategy,
                "cancel": self._on_cancel,
                "submit": self._on_submit,
                "match": self._on_match,
                "fill": self._on_fill,
                "corporate_action": self._on_corporate_action,
                "fx": self._on_fx,
            }[event.kind]
            handler(event)
            if not self._queue or self._queue[0].timestamp != event.timestamp:
                self._settle_timestamp(event.timestamp)
                self._last_timestamp = event.timestamp
        return self._build_result()

    def _rebuild_history(self, bars):
        if self._last_timestamp is None:
            return
        for bar in bars:
            if bar.timestamp <= self._last_timestamp:
                self._last_prices[bar.symbol] = bar.close
                self.ctx._on_bar(bar)

    def _on_market(self, event):
        bar = event.payload
        self._last_prices[bar.symbol] = bar.close
        self.broker.on_bar_start(bar)
        self._record(event, f"{bar.symbol}@{bar.timestamp} close={bar.close} currency={getattr(bar, 'currency', 'USD')}")

    def _on_strategy(self, event):
        bar = event.payload
        self.ctx._on_bar(bar)
        self._record(event, f"on_bar {bar.symbol}@{bar.timestamp}")
        self.strategy.on_bar(self.ctx, bar)

    def _on_cancel(self, event):
        req = event.payload
        order = self.broker.get_order(req.order_id)
        ok, reason = self.broker.cancel(req.order_id, event.timestamp)
        if ok and order is not None:
            self.account.release_order(order, event.timestamp)
        detail = f"cancel order={req.order_id} ok={ok}"
        if reason: detail += f" reason={reason}"
        self._record(event, detail)

    def _on_submit(self, event):
        order = event.payload.order
        reason = self.broker.register(order)
        if reason is None:
            gross = order.quantity * (order.limit_price if order.limit_price is not None else order.estimated_price)
            reason = self.account.reserve_order(order, self.broker.commission.commission(gross))
            if reason is not None:
                self.broker.reject(order, reason)
        else:
            self.broker.reject(order, reason)
        self._record(event, f"submit order={order.order_id} status={order.status.value} reason={order.reject_reason or ''}")

    def _on_match(self, event):
        bar = event.payload.bar
        fills = self.broker.match(bar, event.timestamp)
        for fill in fills:
            self._record(event, f"matched order={fill.order_id} qty={fill.quantity} price={fill.price} fee={fill.commission:.4f}")
            self._push(event.timestamp, Priority.FILL, "fill", fill)
        if not fills:
            self._record(event, f"bar {bar.symbol}@{bar.timestamp} matched 0 fill(s)")

    def _on_fill(self, event):
        fill = event.payload
        order = self.broker.get_order(fill.order_id)
        ref_price = order.limit_price if order.limit_price is not None else fill.price
        remaining_fee = 0.0
        if order.remaining_quantity > 0:
            remaining_fee = self.broker.commission.commission(order.remaining_quantity * ref_price)
        self.account.apply_fill(fill, order=order, remaining_fee=remaining_fee)
        self._fills.append(fill)
        self._record(event, f"fill order={fill.order_id} qty={fill.quantity} currency={fill.currency}")
        self.strategy.on_fill(self.ctx, fill)

    def _on_corporate_action(self, event):
        action = event.payload
        if action.action_type is CorporateActionType.SPLIT:
            self.broker.apply_corporate_action(action)
            self.account.apply_split(action, self.broker.active_orders(action.symbol))
        elif action.action_type is CorporateActionType.DIVIDEND:
            self.account.apply_dividend(action)
        else:
            self.broker.apply_corporate_action(action)
        self._record(event, f"{action.action_type.value} {action.symbol}")

    def _on_fx(self, event):
        req = event.payload
        self.account.apply_fx(req.timestamp, req.from_currency, req.to_currency, req.amount, req.rate)
        self._record(event, f"fx {req.amount}{req.from_currency}->{req.to_currency} rate={req.rate}")

    def _settle_timestamp(self, timestamp):
        marks = {s: p for s, p in self._last_prices.items()}
        market_value = self.account.market_value(marks)
        self._equity_curve.append(EquityPoint(timestamp, self.account.cash,
                                              dict(self.account.cash_balances),
                                              market_value,
                                              self.account.cash + market_value))

    def save_state(self, path):
        from .persistence import save_engine_state
        save_engine_state(self, path)

    @classmethod
    def restore(cls, path, data_feed, strategy):
        from .persistence import restore_engine_state
        return restore_engine_state(cls, path, data_feed, strategy)

    def _build_result(self):
        params = {
            "run_tag": self.run_tag,
            "base_currency": self.base_currency,
            "initial_balances": self.account.initial_balances,
            "cancel_before_match": self.cancel_before_match,
            "broker": self.broker.params(),
            "data": self.data_feed.params() if isinstance(self.data_feed, SyntheticDataFeed) else {"kind": type(self.data_feed).__name__},
            "strategy": self.strategy.params(),
            "corporate_actions": [a.to_row() for a in self.actions],
        }
        return EngineResult(self.account.initial_cash, self.account.cash, dict(self.account.positions),
                            self._fills, self.broker.all_orders(), list(self.account.ledger),
                            self._trace, self._equity_curve, dict(self.account.commission_total), params,
                            dict(self.account.initial_balances), dict(self.account.cash_balances))
