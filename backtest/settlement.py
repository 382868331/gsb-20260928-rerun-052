"""成交确认与实际交收分离的账本。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from .models import Fill, OrderSide


class ObligationStatus(str, Enum):
    PENDING = "PENDING"
    PARTIALLY_SETTLED = "PARTIALLY_SETTLED"
    SETTLED = "SETTLED"
    FAILED = "FAILED"
    PARTIALLY_FAILED = "PARTIALLY_FAILED"


@dataclass
class CorporateAction:
    timestamp: int
    symbol: str
    split_ratio: float = 1.0
    cash_per_share: float = 0.0
    currency: str = "USD"


@dataclass
class SettlementEvent:
    event_id: int
    obligation_id: int
    fill_id: int
    timestamp: int
    symbol: str
    currency: str
    side: str
    quantity: float
    price: float
    gross_amount: float
    commission: float
    action: str
    retry_of_event_id: Optional[int] = None

    def to_row(self) -> dict:
        return self.__dict__.copy()


@dataclass
class SettlementObligation:
    obligation_id: int
    fill_id: int
    order_id: int
    symbol: str
    currency: str
    side: str
    trade_timestamp: int
    due_date: int
    original_quantity: float
    open_quantity: float
    settled_quantity: float
    failed_quantity: float
    price: float
    commission: float
    status: ObligationStatus
    split_ratio: float = 1.0

    def to_row(self) -> dict:
        row = self.__dict__.copy()
        row["status"] = self.status.value
        return row


class SettlementAccount:
    """记录冻结、已交割现金/证券及全部未结交收义务。"""

    def __init__(self, initial_cash: float, currency: str = "USD"):
        if initial_cash <= 0:
            raise ValueError("初始现金必须为正")
        self.base_currency = currency
        self.available_cash: Dict[str, float] = {currency: initial_cash}
        self.frozen_cash: Dict[str, float] = {}
        self.positions: Dict[str, float] = {}
        self.frozen_positions: Dict[str, float] = {}
        self.obligations: List[SettlementObligation] = []
        self.events: List[SettlementEvent] = []
        self._next_obligation_id = 1
        self._next_event_id = 1
        self.initial_cash = initial_cash
        self.commission_total = 0.0

    @classmethod
    def restore(
        cls,
        initial_cash: float,
        available_cash: Dict[str, float],
        frozen_cash: Dict[str, float],
        positions: Dict[str, float],
        frozen_positions: Dict[str, float],
        obligations: List[SettlementObligation],
        events: List[SettlementEvent],
        base_currency: str = "USD",
    ) -> "SettlementAccount":
        account = object.__new__(cls)
        account.base_currency = base_currency
        account.initial_cash = initial_cash
        account.available_cash = available_cash
        account.frozen_cash = frozen_cash
        account.positions = positions
        account.frozen_positions = frozen_positions
        account.obligations = obligations
        account.events = events
        account.commission_total = 0.0
        account._next_obligation_id = max((o.obligation_id for o in obligations), default=0) + 1
        account._next_event_id = max((e.event_id for e in events), default=0) + 1
        return account

    def apply_fill(self, fill: Fill) -> SettlementObligation:
        currency = fill.currency
        if fill.side is OrderSide.BUY:
            required = fill.gross_amount + fill.commission
            available = self.available_cash.get(currency, 0.0)
            if available + 1e-9 < required:
                raise ValueError(f"{currency} 可用现金不足")
            self.available_cash[currency] = available - required
            self.frozen_cash[currency] = self.frozen_cash.get(currency, 0.0) + required
        else:
            available_position = self.positions.get(fill.symbol, 0.0)
            if available_position + 1e-9 < fill.quantity:
                raise ValueError(f"{fill.symbol} 可用持仓不足")
            self.positions[fill.symbol] = available_position - fill.quantity
            self.frozen_positions[fill.symbol] = self.frozen_positions.get(fill.symbol, 0.0) + fill.quantity
            self.available_cash[currency] = self.available_cash.get(currency, 0.0) - fill.commission

        obligation = SettlementObligation(
            obligation_id=self._next_obligation_id,
            fill_id=fill.fill_id,
            order_id=fill.order_id,
            symbol=fill.symbol,
            currency=currency,
            side=fill.side.value,
            trade_timestamp=fill.timestamp,
            due_date=fill.settlement_date if fill.settlement_date is not None else fill.timestamp,
            original_quantity=fill.quantity,
            open_quantity=fill.quantity,
            settled_quantity=0.0,
            failed_quantity=0.0,
            price=fill.price,
            commission=fill.commission,
            status=ObligationStatus.PENDING,
        )
        self._next_obligation_id += 1
        self.obligations.append(obligation)
        self._event(obligation, fill.quantity, "CONFIRM")
        return obligation

    def process_due(self, timestamp: int, failures: Optional[Dict[int, float]] = None) -> List[SettlementEvent]:
        failures = failures or {}
        events: List[SettlementEvent] = []
        for obligation in self.obligations:
            if obligation.due_date != timestamp or obligation.open_quantity <= 0.0:
                continue
            fail_quantity = min(obligation.open_quantity, max(0.0, failures.get(obligation.obligation_id, 0.0)))
            settle_quantity = obligation.open_quantity - fail_quantity
            if fail_quantity > 0.0:
                if fail_quantity < obligation.open_quantity:
                    events.append(self.settle(obligation.obligation_id, timestamp, settle_quantity))
                events.append(self.fail(obligation.obligation_id, timestamp, fail_quantity))
            elif settle_quantity > 0.0:
                events.append(self.settle(obligation.obligation_id, timestamp, settle_quantity))
        return events

    def settle(self, obligation_id: int, timestamp: int, quantity: float) -> SettlementEvent:
        obligation = self._obligation(obligation_id)
        quantity = min(quantity, obligation.open_quantity)
        if quantity <= 0.0:
            raise ValueError("交收数量必须为正")
        adjusted_qty = quantity * obligation.split_ratio
        amount = obligation.price * quantity
        fee = obligation.commission * quantity / obligation.original_quantity
        if obligation.side == OrderSide.BUY.value:
            frozen = amount + fee
            current_frozen = self.frozen_cash.get(obligation.currency, 0.0)
            if current_frozen + 1e-9 < frozen:
                raise ValueError("冻结资金与待交收义务不匹配")
            self.frozen_cash[obligation.currency] = current_frozen - frozen
            self.positions[obligation.symbol] = self.positions.get(obligation.symbol, 0.0) + adjusted_qty
        else:
            current_frozen = self.frozen_positions.get(obligation.symbol, 0.0)
            if current_frozen + 1e-9 < adjusted_qty:
                raise ValueError("冻结证券与待交收义务不匹配")
            self.frozen_positions[obligation.symbol] = current_frozen - adjusted_qty
            self.available_cash[obligation.currency] = self.available_cash.get(obligation.currency, 0.0) + amount
        obligation.open_quantity -= quantity
        obligation.settled_quantity += quantity
        self._refresh_status(obligation)
        self.commission_total += fee
        return self._event(obligation, quantity, "SETTLE")

    def fail(self, obligation_id: int, timestamp: int, quantity: float) -> SettlementEvent:
        obligation = self._obligation(obligation_id)
        quantity = min(quantity, obligation.open_quantity)
        if quantity <= 0.0:
            raise ValueError("失败数量必须为正")
        amount = obligation.price * quantity
        fee = obligation.commission * quantity / obligation.original_quantity
        if obligation.side == OrderSide.BUY.value:
            self.frozen_cash[obligation.currency] -= amount + fee
            self.available_cash[obligation.currency] = self.available_cash.get(obligation.currency, 0.0) + amount + fee
        else:
            adjusted = quantity * obligation.split_ratio
            self.frozen_positions[obligation.symbol] = self.frozen_positions.get(obligation.symbol, 0.0) - adjusted
            self.positions[obligation.symbol] = self.positions.get(obligation.symbol, 0.0) + adjusted
            self.available_cash[obligation.currency] = self.available_cash.get(obligation.currency, 0.0) + fee
        obligation.open_quantity -= quantity
        obligation.failed_quantity += quantity
        self._refresh_status(obligation)
        return self._event(obligation, quantity, "FAIL")

    def retry(self, obligation_id: int, timestamp: int, quantity: float) -> SettlementEvent:
        """失败后补交；冻结恢复后按原成交价格交收，不产生新交易盈亏。"""
        obligation = self._obligation(obligation_id)
        quantity = min(quantity, obligation.failed_quantity)
        if quantity <= 0.0:
            raise ValueError("补交数量必须为正")
        amount = obligation.price * quantity
        fee = obligation.commission * quantity / obligation.original_quantity
        if obligation.side == OrderSide.BUY.value:
            if self.available_cash.get(obligation.currency, 0.0) + 1e-9 < amount + fee:
                raise ValueError("补交时可用资金不足")
            self.available_cash[obligation.currency] -= amount + fee
            self.frozen_cash[obligation.currency] = self.frozen_cash.get(obligation.currency, 0.0) + amount + fee
        else:
            adjusted = quantity * obligation.split_ratio
            if self.positions.get(obligation.symbol, 0.0) + 1e-9 < adjusted:
                raise ValueError("补交时可用证券不足")
            self.positions[obligation.symbol] -= adjusted
            self.frozen_positions[obligation.symbol] = self.frozen_positions.get(obligation.symbol, 0.0) + adjusted
        obligation.failed_quantity -= quantity
        obligation.open_quantity += quantity
        self._refresh_status(obligation)
        freeze_event = self._event(obligation, quantity, "RETRY_FREEZE")
        settle_event = self.settle(obligation_id, timestamp, quantity)
        settle_event.retry_of_event_id = freeze_event.event_id
        return settle_event

    def apply_corporate_action(self, action: CorporateAction) -> None:
        """现金分配归属当前已交收持仓；拆分同步调整未完成买方应收数量。"""
        if action.split_ratio <= 0:
            raise ValueError("拆分比例必须为正")
        if action.split_ratio != 1.0:
            self.positions[action.symbol] = self.positions.get(action.symbol, 0.0) * action.split_ratio
            for obligation in self.obligations:
                if obligation.symbol != action.symbol or obligation.open_quantity + obligation.failed_quantity <= 0:
                    continue
                if obligation.side == OrderSide.SELL.value:
                    unsettled_base = obligation.open_quantity + obligation.failed_quantity
                    old_adjusted = unsettled_base * obligation.split_ratio
                    new_adjusted = old_adjusted * action.split_ratio
                    self.frozen_positions[action.symbol] = self.frozen_positions.get(action.symbol, 0.0) + new_adjusted - old_adjusted
                obligation.split_ratio *= action.split_ratio
        if action.cash_per_share:
            distribution = self.positions.get(action.symbol, 0.0) * action.cash_per_share / action.split_ratio
            self.available_cash[action.currency] = self.available_cash.get(action.currency, 0.0) + distribution

    def open_obligations(self) -> List[SettlementObligation]:
        return [o for o in self.obligations if o.open_quantity > 0.0 or o.failed_quantity > 0.0]

    def position(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def _obligation(self, obligation_id: int) -> SettlementObligation:
        for obligation in self.obligations:
            if obligation.obligation_id == obligation_id:
                return obligation
        raise KeyError(f"交收义务 {obligation_id} 不存在")

    def _refresh_status(self, obligation: SettlementObligation) -> None:
        total_unsettled = obligation.open_quantity + obligation.failed_quantity
        if obligation.open_quantity > 0 and obligation.failed_quantity > 0:
            obligation.status = ObligationStatus.PARTIALLY_FAILED
        elif total_unsettled > 0 and obligation.settled_quantity > 0:
            obligation.status = ObligationStatus.PARTIALLY_SETTLED
        elif obligation.open_quantity > 0:
            obligation.status = ObligationStatus.PENDING
        elif obligation.failed_quantity > 0:
            obligation.status = ObligationStatus.FAILED
        else:
            obligation.status = ObligationStatus.SETTLED

    def _event(self, obligation: SettlementObligation, quantity: float, action: str) -> SettlementEvent:
        event = SettlementEvent(
            event_id=self._next_event_id,
            obligation_id=obligation.obligation_id,
            fill_id=obligation.fill_id,
            timestamp=obligation.due_date if action != "CONFIRM" else obligation.trade_timestamp,
            symbol=obligation.symbol,
            currency=obligation.currency,
            side=obligation.side,
            quantity=quantity,
            price=obligation.price,
            gross_amount=obligation.price * quantity,
            commission=obligation.commission * quantity / obligation.original_quantity,
            action=action,
        )
        self._next_event_id += 1
        self.events.append(event)
        return event
