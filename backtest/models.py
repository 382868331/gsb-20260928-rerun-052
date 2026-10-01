"""核心数据结构：行情、订单、成交。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    PENDING = "PENDING"      # 已提交但尚未进入撮合
    OPEN = "OPEN"            # 挂单中（含部分成交后继续挂单）
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"  # 撤单成功（可能带有部分成交）
    REJECTED = "REJECTED"


class CorporateActionType(str, Enum):
    SPLIT = "SPLIT"
    DIVIDEND = "DIVIDEND"
    HALT = "HALT"
    RESUME = "RESUME"


@dataclass(frozen=True)
class Bar:
    """一根 OHLCV K 线。策略只可读取已经到达的 bar。"""

    symbol: str
    timestamp: int           # 统一使用整数时间戳（纳秒/秒均可，由数据源定义）
    open: float
    high: float
    low: float
    close: float
    volume: float
    currency: str = "USD"

    def to_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "currency": self.currency,
        }


@dataclass
class Order:
    """委托单。挂单不等于成交，只有收到 Fill 才改变账户。"""

    order_id: int
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float                  # 委托总量（必须为正）
    submitted_at: int                # 策略提交时刻
    limit_price: Optional[float] = None  # 限价单价格
    filled_quantity: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    filled_at: Optional[int] = None
    cancelled_at: Optional[int] = None
    reject_reason: Optional[str] = None
    currency: str = "USD"
    reserved_cash: float = 0.0
    reserved_position: float = 0.0
    reserved_fee: float = 0.0
    estimated_price: float = 0.0
    halted: bool = False
    halted_at: Optional[int] = None
    resumed_at: Optional[int] = None

    @property
    def remaining_quantity(self) -> float:
        return self.quantity - self.filled_quantity

    @property
    def is_active(self) -> bool:
        return self.status in (
            OrderStatus.PENDING,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        )

    def to_row(self) -> dict:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": self.quantity,
            "limit_price": "" if self.limit_price is None else self.limit_price,
            "filled_quantity": self.filled_quantity,
            "status": self.status.value,
            "submitted_at": self.submitted_at,
            "filled_at": "" if self.filled_at is None else self.filled_at,
            "cancelled_at": "" if self.cancelled_at is None else self.cancelled_at,
            "reject_reason": "" if self.reject_reason is None else self.reject_reason,
            "currency": self.currency,
            "reserved_cash": self.reserved_cash,
            "reserved_position": self.reserved_position,
            "reserved_fee": self.reserved_fee,
            "estimated_price": self.estimated_price,
            "halted": self.halted,
            "halted_at": "" if self.halted_at is None else self.halted_at,
            "resumed_at": "" if self.resumed_at is None else self.resumed_at,
        }


@dataclass(frozen=True)
class Fill:
    """一笔成交回报（一个订单在一根 bar 上可能多次部分成交）。"""

    fill_id: int
    order_id: int
    symbol: str
    side: OrderSide
    timestamp: int
    quantity: float
    price: float
    commission: float
    bar_timestamp: int              # 撮合发生的 bar 时刻
    currency: str = "USD"

    @property
    def gross_amount(self) -> float:
        """成交金额（不含手续费）。"""
        return self.price * self.quantity

    def to_row(self) -> dict:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "timestamp": self.timestamp,
            "quantity": self.quantity,
            "price": self.price,
            "gross_amount": self.gross_amount,
            "commission": self.commission,
            "bar_timestamp": self.bar_timestamp,
            "currency": self.currency,
        }


@dataclass(frozen=True)
class CorporateAction:
    """在 effective_at 恰好生效的公司行为或交易状态变化。"""

    action_type: CorporateActionType
    symbol: str
    effective_at: int
    ratio: float = 1.0
    amount: float = 0.0
    currency: str = "USD"

    def to_row(self) -> dict:
        return {
            "action_type": self.action_type.value,
            "symbol": self.symbol,
            "effective_at": self.effective_at,
            "ratio": self.ratio,
            "amount": self.amount,
            "currency": self.currency,
        }
