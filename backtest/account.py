"""资金账户：现金、持仓与账目记录。

设计原则：
- 账户只承认 Fill。挂单、撤单本身绝不改变现金与持仓。
- 每一笔成交产生一条流水，现金与持仓均可由
  (初始现金 + 全部 Fill 流水) 独立复算（见 recompute_from_fills）。
- 支持多标的、允许卖空（持仓数量为有符号数：正为多头，负为空头）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List

from .models import Fill, OrderSide


@dataclass
class LedgerEntry:
    """资金/持仓流水。delta_cash 与 delta_position 均为成交导致的增量。"""

    fill_id: int
    order_id: int
    timestamp: int
    bar_timestamp: int
    symbol: str
    side: str
    quantity: float
    price: float
    gross_amount: float
    commission: float
    delta_cash: float
    delta_position: float
    cash_after: float
    position_after: float

    def to_row(self) -> dict:
        return self.__dict__.copy()


class Account:
    def __init__(self, initial_cash: float = 1_000_000.0):
        if initial_cash <= 0:
            raise ValueError("初始现金必须为正")
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: Dict[str, float] = {}
        self.ledger: List[LedgerEntry] = []
        self.commission_total = 0.0

    def apply_fill(self, fill: Fill) -> LedgerEntry:
        """根据一笔成交更新现金与持仓，并写入流水。"""
        signed_qty = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        if fill.side is OrderSide.BUY:
            delta_cash = -(fill.gross_amount + fill.commission)
        else:
            delta_cash = fill.gross_amount - fill.commission

        self.cash += delta_cash
        self.positions[fill.symbol] = self.positions.get(fill.symbol, 0.0) + signed_qty
        self.commission_total += fill.commission
        entry = LedgerEntry(
            fill_id=fill.fill_id,
            order_id=fill.order_id,
            timestamp=fill.timestamp,
            bar_timestamp=fill.bar_timestamp,
            symbol=fill.symbol,
            side=fill.side.value,
            quantity=fill.quantity,
            price=fill.price,
            gross_amount=fill.gross_amount,
            commission=fill.commission,
            delta_cash=delta_cash,
            delta_position=signed_qty,
            cash_after=self.cash,
            position_after=self.positions[fill.symbol],
        )
        self.ledger.append(entry)
        return entry

    def position(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def market_value(self, marks: Dict[str, float]) -> float:
        """按给定的各标的最新价计算持仓市值（允许卖空，可为负）。"""
        return sum(qty * marks.get(symbol, 0.0) for symbol, qty in self.positions.items())

    def equity(self, marks: Dict[str, float]) -> float:
        return self.cash + self.market_value(marks)


@dataclass
class RecomputedAccount:
    cash: float
    positions: Dict[str, float]
    commission_total: float
    cash_rows: List[dict]


def recompute_from_fills(
    fills: Iterable[Fill], initial_cash: float = 1_000_000.0
) -> RecomputedAccount:
    """完全独立于 Account 的复算：仅凭初始现金与成交流水重建现金和持仓。

    用于核对引擎账目：复算结果必须与 Account 内部状态逐笔一致。
    """
    cash = initial_cash
    positions: Dict[str, float] = {}
    commission_total = 0.0
    rows: List[dict] = []
    for fill in sorted(fills, key=lambda f: (f.timestamp, f.fill_id)):
        signed_qty = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        if fill.side is OrderSide.BUY:
            delta_cash = -(fill.gross_amount + fill.commission)
        else:
            delta_cash = fill.gross_amount - fill.commission
        cash += delta_cash
        positions[fill.symbol] = positions.get(fill.symbol, 0.0) + signed_qty
        commission_total += fill.commission
        rows.append(
            {
                "fill_id": fill.fill_id,
                "cash_after": cash,
                "position_after": positions[fill.symbol],
                "commission": fill.commission,
            }
        )
    return RecomputedAccount(
        cash=cash,
        positions=positions,
        commission_total=commission_total,
        cash_rows=rows,
    )
