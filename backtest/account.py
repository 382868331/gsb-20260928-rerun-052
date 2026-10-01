"""多币种资金账户、冻结账目与独立复算。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional

from .models import CorporateAction, Fill, OrderSide


@dataclass
class LedgerEntry:
    entry_id: int
    timestamp: int
    event_type: str
    currency: str
    symbol: str
    delta_cash: float = 0.0
    delta_cash_frozen: float = 0.0
    delta_position: float = 0.0
    delta_position_frozen: float = 0.0
    fx_rate: float = 1.0
    gross_amount: float = 0.0
    commission: float = 0.0
    fill_id: Optional[int] = None
    order_id: Optional[int] = None
    ref_id: Optional[int] = None
    note: str = ""
    cash_after: float = 0.0
    cash_frozen_after: float = 0.0
    position_after: float = 0.0
    position_frozen_after: float = 0.0

    def to_row(self) -> dict:
        return self.__dict__.copy()


class Account:
    def __init__(self, initial_cash: float | Mapping[str, float] = 1_000_000.0, base_currency: str = "USD"):
        if isinstance(initial_cash, dict):
            balances = dict(initial_cash)
        else:
            if initial_cash <= 0:
                raise ValueError("初始现金必须为正")
            balances = {base_currency: initial_cash}
        if any(v < 0 for v in balances.values()):
            raise ValueError("初始现金不能为负")
        self.base_currency = base_currency
        self.initial_cash = float(balances.get(base_currency, 0.0))
        self.initial_balances = dict(balances)
        self.cash_balances: Dict[str, float] = dict(balances)
        self.frozen_cash: Dict[str, float] = {}
        self.positions: Dict[str, float] = {}
        self.frozen_positions: Dict[str, float] = {}
        self.ledger: List[LedgerEntry] = []
        self.commission_total: Dict[str, float] = {}
        self._next_entry_id = 1

    @property
    def cash(self) -> float:
        return self.cash_balances.get(self.base_currency, 0.0)

    def cash_available(self, currency: str) -> float:
        return self.cash_balances.get(currency, 0.0) - self.frozen_cash.get(currency, 0.0)

    def position(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def position_available(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0) - self.frozen_positions.get(symbol, 0.0)

    def _entry(self, timestamp, event_type, currency, symbol, **kwargs):
        entry = LedgerEntry(
            entry_id=self._next_entry_id,
            timestamp=timestamp,
            event_type=event_type,
            currency=currency,
            symbol=symbol,
            cash_after=self.cash_balances.get(currency, 0.0),
            cash_frozen_after=self.frozen_cash.get(currency, 0.0),
            position_after=self.positions.get(symbol, 0.0),
            position_frozen_after=self.frozen_positions.get(symbol, 0.0),
            **kwargs,
        )
        self._next_entry_id += 1
        self.ledger.append(entry)
        return entry

    def reserve_order(self, order, estimated_commission: float) -> Optional[str]:
        if order.side is OrderSide.BUY:
            reference_price = order.limit_price or getattr(order, "estimated_price", 0.0) or 0.0
            amount = order.remaining_quantity * reference_price
            required = amount + estimated_commission
            if self.cash_available(order.currency) + 1e-9 < required:
                return f"{order.currency} 可用现金不足: required={required:.4f} available={self.cash_available(order.currency):.4f}"
            self.frozen_cash[order.currency] = self.frozen_cash.get(order.currency, 0.0) + required
            order.reserved_cash = required
            order.reserved_fee = estimated_commission
            self._entry(order.submitted_at, "RESERVE", order.currency, order.symbol,
                        delta_cash_frozen=required, gross_amount=amount,
                        commission=estimated_commission, order_id=order.order_id, note="buy cash reserve")
            return None
        # 第一版约定允许卖空；卖单不预扣持仓，实际成交时才形成有符号持仓。
        return None

    def release_order(self, order, timestamp: int) -> None:
        if order.side is OrderSide.BUY:
            if order.filled_quantity <= 0 and order.status.value == "REJECTED":
                return
            amount = getattr(order, "reserved_cash", 0.0)
            if abs(amount) > 1e-10:
                self.frozen_cash[order.currency] = self.frozen_cash.get(order.currency, 0.0) - amount
                self._entry(timestamp, "RELEASE", order.currency, order.symbol,
                            delta_cash_frozen=-amount, order_id=order.order_id,
                            note="cancel unused buy cash")
                order.reserved_cash = 0.0
        else:
            order.reserved_position = 0.0

    def settle_fill(self, fill: Fill, order, remaining_fee: float = 0.0) -> None:
        entries = []
        if fill.side is OrderSide.BUY:
            old_reserve = float(order.reserved_cash)
            self.frozen_cash[fill.currency] = self.frozen_cash.get(fill.currency, 0.0) - old_reserve
            self.cash_balances[fill.currency] = self.cash_balances.get(fill.currency, 0.0) - fill.gross_amount
            self.positions[fill.symbol] = self.positions.get(fill.symbol, 0.0) + fill.quantity
            entries.append(self._entry(fill.timestamp, "FILL_GROSS", fill.currency, fill.symbol,
                                       delta_cash=-fill.gross_amount, delta_cash_frozen=-old_reserve,
                                       delta_position=fill.quantity, gross_amount=fill.gross_amount,
                                       fill_id=fill.fill_id, order_id=fill.order_id))
            reference_price = order.limit_price or getattr(order, "estimated_price", fill.price) or fill.price
            new_reserve = order.remaining_quantity * reference_price + remaining_fee
            if new_reserve > 1e-10:
                self.frozen_cash[fill.currency] = self.frozen_cash.get(fill.currency, 0.0) + new_reserve
                entries.append(self._entry(fill.timestamp, "RESERVE", fill.currency, fill.symbol,
                                           delta_cash_frozen=new_reserve, gross_amount=order.remaining_quantity * reference_price,
                                           commission=remaining_fee,
                                           order_id=order.order_id, note="remaining buy cash reserve"))
            order.reserved_cash = new_reserve
            order.reserved_fee = remaining_fee
        else:
            self.positions[fill.symbol] = self.positions.get(fill.symbol, 0.0) - fill.quantity
            self.cash_balances[fill.currency] = self.cash_balances.get(fill.currency, 0.0) + fill.gross_amount
            entries.append(self._entry(fill.timestamp, "FILL_GROSS", fill.currency, fill.symbol,
                                       delta_cash=fill.gross_amount,
                                       delta_position=-fill.quantity,
                                       gross_amount=fill.gross_amount, fill_id=fill.fill_id, order_id=fill.order_id))
        self.cash_balances[fill.currency] = self.cash_balances.get(fill.currency, 0.0) - fill.commission
        self.commission_total[fill.currency] = self.commission_total.get(fill.currency, 0.0) + fill.commission
        entries.append(self._entry(fill.timestamp, "COMMISSION", fill.currency, fill.symbol,
                                   delta_cash=-fill.commission, commission=fill.commission,
                                   fill_id=fill.fill_id, order_id=fill.order_id))
        if fill.side is OrderSide.SELL:
            order.reserved_position = 0.0

    def apply_fill(self, fill: Fill, *args, **kwargs):
        order = kwargs.get("order")
        if order is not None:
            self.settle_fill(fill, order, kwargs.get("remaining_fee", 0.0))
            return []
        currency = getattr(fill, "currency", self.base_currency)
        signed_qty = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        signed_cash = -(fill.gross_amount + fill.commission) if fill.side is OrderSide.BUY else fill.gross_amount - fill.commission
        self.cash_balances[currency] = self.cash_balances.get(currency, 0.0) + signed_cash
        self.positions[fill.symbol] = self.positions.get(fill.symbol, 0.0) + signed_qty
        self.commission_total[currency] = self.commission_total.get(currency, 0.0) + fill.commission
        self._entry(fill.timestamp, "FILL", currency, fill.symbol, delta_cash=signed_cash,
                    delta_position=signed_qty, gross_amount=fill.gross_amount, commission=fill.commission,
                    fill_id=fill.fill_id, order_id=fill.order_id)
        return []

    def apply_fx(self, timestamp: int, from_currency: str, to_currency: str, from_amount: float, rate: float, ref_id: Optional[int] = None, note: str = "FX"):
        if from_amount <= 0 or rate <= 0:
            raise ValueError("汇兑金额和汇率必须为正")
        if self.cash_available(from_currency) + 1e-9 < from_amount:
            raise ValueError(f"{from_currency} 可用现金不足")
        to_amount = from_amount * rate
        self.cash_balances[from_currency] -= from_amount
        self._entry(timestamp, "FX_OUT", from_currency, "", delta_cash=-from_amount,
                    fx_rate=rate, ref_id=ref_id, note=note)
        self.cash_balances[to_currency] = self.cash_balances.get(to_currency, 0.0) + to_amount
        self._entry(timestamp, "FX_IN", to_currency, "", delta_cash=to_amount,
                    fx_rate=rate, ref_id=ref_id, note=note)

    def apply_split(self, action: CorporateAction, orders=()) -> None:
        qty = self.positions.get(action.symbol, 0.0)
        frozen = self.frozen_positions.get(action.symbol, 0.0)
        split_delta = qty * (action.ratio - 1.0)
        split_frozen_delta = frozen * (action.ratio - 1.0)
        self.positions[action.symbol] = qty * action.ratio
        self.frozen_positions[action.symbol] = frozen * action.ratio
        for order in orders:
            if order.symbol != action.symbol or order.side is not OrderSide.BUY:
                continue
            old_reserve = float(order.reserved_cash)
            new_reserve = order.remaining_quantity * (order.limit_price or order.estimated_price) + order.reserved_fee
            reserve_delta = new_reserve - old_reserve
            self.frozen_cash[order.currency] = self.frozen_cash.get(order.currency, 0.0) + reserve_delta
            order.reserved_cash = new_reserve
            if abs(reserve_delta) > 1e-10:
                self._entry(action.effective_at, "RESERVE_ADJUST", order.currency, order.symbol,
                            delta_cash_frozen=reserve_delta, order_id=order.order_id, note="split reserve adjustment")
        self._entry(action.effective_at, "SPLIT", "", action.symbol,
                    delta_position=split_delta, delta_position_frozen=split_frozen_delta,
                    ref_id=None, note=f"ratio={action.ratio}")

    def apply_dividend(self, action: CorporateAction) -> None:
        qty = self.positions.get(action.symbol, 0.0)
        amount = qty * action.amount
        self.cash_balances[action.currency] = self.cash_balances.get(action.currency, 0.0) + amount
        self._entry(action.effective_at, "DIVIDEND", action.currency, action.symbol,
                    delta_cash=amount, gross_amount=amount, note="cash dividend")

    def market_value(self, marks: Dict[str, float], currency_rates: Optional[Dict[str, float]] = None) -> float:
        rates = currency_rates or {}
        return sum(qty * marks.get(symbol, 0.0) * rates.get(symbol, 1.0) for symbol, qty in self.positions.items())

    def equity(self, marks, rates=None):
        return self.cash + self.market_value(marks, rates)


@dataclass
class RecomputedAccount:
    cash: float
    positions: Dict[str, float]
    commission_total: Dict[str, float]
    cash_balances: Dict[str, float]
    cash_rows: List[dict]


def recompute_from_ledger(ledger: Iterable[LedgerEntry], initial_balances: Mapping[str, float], base_currency: str = "USD"):
    cash = dict(initial_balances)
    frozen_cash = {}
    positions = {}
    frozen_positions = {}
    fees = {}
    rows = []
    for e in sorted(ledger, key=lambda x: x.entry_id):
        if e.currency:
            cash[e.currency] = cash.get(e.currency, 0.0) + e.delta_cash
            frozen_cash[e.currency] = frozen_cash.get(e.currency, 0.0) + e.delta_cash_frozen
        if e.symbol:
            positions[e.symbol] = positions.get(e.symbol, 0.0) + e.delta_position
            frozen_positions[e.symbol] = frozen_positions.get(e.symbol, 0.0) + e.delta_position_frozen
        if e.event_type == "COMMISSION" and e.currency:
            fees[e.currency] = fees.get(e.currency, 0.0) + e.commission
        rows.append({"entry_id": e.entry_id, "cash": cash.copy(), "positions": positions.copy()})
    return RecomputedAccount(cash.get(base_currency, 0.0), positions, fees, cash, rows)


def recompute_from_fills(fills, initial_cash=1_000_000.0, base_currency="USD"):
    initial = {base_currency: initial_cash} if not isinstance(initial_cash, dict) else initial_cash
    cash = dict(initial)
    positions = {}
    fees = {}
    rows = []
    for fill in sorted(fills, key=lambda f: (f.timestamp, f.fill_id)):
        signed_qty = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        signed_cash = -(fill.gross_amount + fill.commission) if fill.side is OrderSide.BUY else fill.gross_amount - fill.commission
        currency = getattr(fill, "currency", base_currency)
        cash[currency] = cash.get(currency, 0.0) + signed_cash
        positions[fill.symbol] = positions.get(fill.symbol, 0.0) + signed_qty
        fees[currency] = fees.get(currency, 0.0) + fill.commission
        rows.append({"fill_id": fill.fill_id, "cash": cash.copy(), "position_after": positions[fill.symbol]})
    return RecomputedAccount(cash.get(base_currency, 0.0), positions, fees, cash, rows)
