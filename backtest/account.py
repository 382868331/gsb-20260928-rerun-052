"""账户与资金账目：只管钱、持仓和流水，不懂策略。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Fill, LedgerEntry, Side


@dataclass
class Account:
    initial_cash: float
    cash: float = 0.0
    positions: dict[str, int] = field(default_factory=dict)
    ledger: list[LedgerEntry] = field(default_factory=list)
    _entry_seq: int = 0

    def __post_init__(self) -> None:
        if self.initial_cash < 0:
            raise ValueError("初始资金不能为负")
        self.deposit(self.initial_cash, ts=0, note="初始资金")

    def position(self, symbol: str) -> int:
        return self.positions.get(symbol, 0)

    def deposit(self, amount: float, ts: int, note: str = "") -> None:
        if amount < 0:
            raise ValueError("入金金额不能为负")
        self.cash += amount
        self._entry_seq += 1
        self.ledger.append(LedgerEntry(
            entry_id=self._entry_seq, ts=ts, kind="DEPOSIT", symbol="",
            qty_delta=0, cash_delta=amount, fee=0.0,
            fill_id=None, order_id=None,
            cash_after=self.cash, position_after=0, note=note,
        ))

    def apply_fill(self, fill: Fill) -> None:
        """把一笔成交记入现金、持仓与流水。

        买入：现金 -= 成交额 + 手续费；卖出：现金 += 成交额 - 手续费。
        """
        gross = fill.qty * fill.price
        if fill.side is Side.BUY:
            qty_delta = fill.qty
            cash_delta = -(gross + fill.fee)
        else:
            qty_delta = -fill.qty
            cash_delta = gross - fill.fee
        new_cash = self.cash + cash_delta
        if new_cash < -1e-9:
            raise ValueError("现金不足，成交不应被撮合")
        new_pos = self.position(fill.symbol) + qty_delta
        if new_pos < -1e-9:
            raise ValueError("持仓不足，成交不应被撮合")
        self.cash = new_cash
        self.positions[fill.symbol] = new_pos
        self._entry_seq += 1
        self.ledger.append(LedgerEntry(
            entry_id=self._entry_seq, ts=fill.ts, kind="FILL",
            symbol=fill.symbol, qty_delta=qty_delta,
            cash_delta=cash_delta, fee=fill.fee,
            fill_id=fill.fill_id, order_id=fill.order_id,
            cash_after=self.cash, position_after=new_pos,
        ))


def replay_ledger(
    ledger: list[LedgerEntry],
) -> tuple[float, dict[str, int]]:
    """只依据流水复算现金与持仓，用于独立对账。"""
    cash = 0.0
    positions = {}
    for e in ledger:
        cash += e.cash_delta
        if e.symbol:
            positions[e.symbol] = positions.get(e.symbol, 0) + e.qty_delta
    return cash, positions
