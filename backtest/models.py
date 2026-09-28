from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    NEW = "NEW"                      # 已挂出，尚未成交
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"            # 撤单成功（含同 bar 与成交相撞被撤）
    REJECTED = "REJECTED"            # 参数非法 / 资金或持仓不足后的剩余部分


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: int                  # 单调递增的离散时刻（如分钟序号）
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self) -> None:
        if not (self.low <= self.open <= self.high and
                self.low <= self.close <= self.high):
            raise ValueError(f"非法 OHLC: {self}")
        if self.volume < 0:
            raise ValueError("成交量不能为负")


@dataclass
class Order:
    order_id: int
    symbol: str
    side: Side
    qty: int                 # 委托总量
    order_type: OrderType
    submit_ts: int
    submit_bar_idx: int      # 挂单所在 bar，下一根 bar 起才可成交
    limit_price: float | None = None
    filled_qty: int = 0
    status: OrderStatus = OrderStatus.NEW
    cancel_requested: bool = False
    reject_reason: str = ""

    @property
    def remaining(self) -> int:
        return self.qty - self.filled_qty


@dataclass
class Fill:
    fill_id: int
    order_id: int
    symbol: str
    side: Side
    qty: int
    price: float
    fee: float
    ts: int
    bar_idx: int


@dataclass
class LedgerEntry:
    """一笔不可变账目流水。现金/持仓均可由流水独立复算。"""

    entry_id: int
    ts: int
    kind: str                # DEPOSIT / FILL
    symbol: str              # DEPOSIT 时为空串
    qty_delta: int           # 带符号持仓变动：买 + 卖 -
    cash_delta: float        # 带符号现金变动（手续费已计入）
    fee: float
    fill_id: int | None
    order_id: int | None
    cash_after: float
    position_after: int
    note: str = ""
