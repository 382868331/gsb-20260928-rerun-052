"""回测中途状态保存与恢复。"""

from __future__ import annotations

import json
from typing import List

from .broker import CommissionModel
from .account import LedgerEntry
from .models import Fill, Order, OrderSide, OrderStatus, OrderType


def _enum(enum_cls, value):
    return enum_cls(value)


def order_from_row(row: dict) -> Order:
    def opt_float(name):
        value = row.get(name, "")
        return None if value in ("", None) else float(value)

    def opt_int(name):
        value = row.get(name, "")
        return None if value in ("", None) else int(value)

    return Order(
        order_id=int(row["order_id"]),
        symbol=row["symbol"],
        side=_enum(OrderSide, row["side"]),
        order_type=_enum(OrderType, row["order_type"]),
        quantity=float(row["quantity"]),
        submitted_at=int(row["submitted_at"]),
        limit_price=opt_float("limit_price"),
        filled_quantity=float(row.get("filled_quantity", 0.0)),
        status=_enum(OrderStatus, row["status"]),
        filled_at=opt_int("filled_at"),
        cancelled_at=opt_int("cancelled_at"),
        reject_reason=row.get("reject_reason") or None,
        currency=row.get("currency", "USD"),
        reserved_cash=float(row.get("reserved_cash", 0.0) or 0.0),
        reserved_position=float(row.get("reserved_position", 0.0) or 0.0),
        reserved_fee=float(row.get("reserved_fee", 0.0) or 0.0),
        estimated_price=float(row.get("estimated_price", 0.0) or 0.0),
        halted=str(row.get("halted", "False")).lower() == "true",
        halted_at=opt_int("halted_at"),
        resumed_at=opt_int("resumed_at"),
    )


def fill_from_row(row: dict) -> Fill:
    return Fill(
        fill_id=int(row["fill_id"]),
        order_id=int(row["order_id"]),
        symbol=row["symbol"],
        side=_enum(OrderSide, row["side"]),
        timestamp=int(row["timestamp"]),
        quantity=float(row["quantity"]),
        price=float(row["price"]),
        commission=float(row["commission"]),
        bar_timestamp=int(row["bar_timestamp"]),
        currency=row.get("currency", "USD"),
    )


def entry_from_row(row: dict) -> LedgerEntry:
    values = {key: row.get(key) for key in LedgerEntry.__dataclass_fields__}
    for key in (
        "entry_id", "timestamp", "fill_id", "order_id", "ref_id",
    ):
        if values[key] not in (None, ""):
            values[key] = int(values[key])
    for key in (
        "delta_cash", "delta_cash_frozen", "delta_position", "delta_position_frozen",
        "fx_rate", "gross_amount", "commission", "cash_after", "cash_frozen_after",
        "position_after", "position_frozen_after",
    ):
        values[key] = float(values.get(key) or 0.0)
    for key in ("fill_id", "order_id", "ref_id"):
        if values[key] == "":
            values[key] = None
    return LedgerEntry(**values)


def save_engine_state(engine, path: str) -> None:
    payload = {
        "last_timestamp": engine._last_timestamp,
        "seq": engine._seq,
        "next_order_id": engine._next_order_id,
        "base_currency": engine.base_currency,
        "cancel_before_match": engine.cancel_before_match,
        "run_tag": engine.run_tag,
        "fx_rates": engine.fx_rates,
        "broker": engine.broker.params(),
        "account": {
            "initial_balances": engine.account.initial_balances,
            "cash_balances": engine.account.cash_balances,
            "frozen_cash": engine.account.frozen_cash,
            "positions": engine.account.positions,
            "frozen_positions": engine.account.frozen_positions,
            "commission_total": engine.account.commission_total,
            "next_entry_id": engine.account._next_entry_id,
        },
        "orders": [o.to_row() for o in engine.broker.all_orders()],
        "next_fill_id": engine.broker._next_fill_id,
        "halted_symbols": sorted(engine.broker._halted),
        "next_bar_active": sorted(engine.broker._next_bar_active),
        "fills": [f.to_row() for f in engine._fills],
        "ledger": [e.to_row() for e in engine.account.ledger],
        "event_trace": [t.to_row() for t in engine._trace],
        "equity_curve": [p.to_row() for p in engine._equity_curve],
        "last_prices": engine._last_prices,
        "actions": [a.to_row() for a in engine.actions],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)


def restore_engine_state(cls, path: str, data_feed, strategy):
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    from .models import CorporateAction, CorporateActionType

    actions = [
        CorporateAction(
            action_type=CorporateActionType(row["action_type"]),
            symbol=row["symbol"],
            effective_at=int(row["effective_at"]),
            ratio=float(row["ratio"]),
            amount=float(row["amount"]),
            currency=row.get("currency", "USD"),
        )
        for row in payload.get("actions", [])
    ]
    engine = cls(
        data_feed,
        strategy,
        initial_cash=payload["account"]["initial_balances"],
        base_currency=payload.get("base_currency", "USD"),
        cancel_before_match=payload.get("cancel_before_match", True),
        run_tag=payload.get("run_tag", "run"),
        corporate_actions=actions,
        fx_rates=payload.get("fx_rates", {}),
        commission=CommissionModel(
            rate=float(payload.get("broker", {}).get("commission_rate", 0.0003)),
            min_commission=float(payload.get("broker", {}).get("min_commission", 1.0)),
        ),
        max_participation=float(payload.get("broker", {}).get("max_participation", 0.1)),
    )
    account_data = payload["account"]
    engine.account.cash_balances = {k: float(v) for k, v in account_data["cash_balances"].items()}
    engine.account.frozen_cash = {k: float(v) for k, v in account_data.get("frozen_cash", {}).items()}
    engine.account.positions = {k: float(v) for k, v in account_data["positions"].items()}
    engine.account.frozen_positions = {k: float(v) for k, v in account_data.get("frozen_positions", {}).items()}
    engine.account.commission_total = {k: float(v) for k, v in account_data.get("commission_total", {}).items()}
    engine.account._next_entry_id = int(account_data.get("next_entry_id", 1))
    engine.account.ledger = [entry_from_row(row) for row in payload.get("ledger", [])]

    for row in payload.get("orders", []):
        order = order_from_row(row)
        engine.broker._orders[order.order_id] = order
    engine.broker._next_fill_id = int(payload.get("next_fill_id", 1))
    engine.broker._halted = set(payload.get("halted_symbols", []))
    engine.broker._next_bar_active = set(int(x) for x in payload.get("next_bar_active", []))
    engine._fills = [fill_from_row(row) for row in payload.get("fills", [])]
    from .engine import EquityPoint, EventTrace
    engine._trace = [EventTrace(**row) for row in payload.get("event_trace", [])]
    engine._equity_curve = [EquityPoint(**row) for row in payload.get("equity_curve", [])]
    engine._last_prices = payload.get("last_prices", {})
    engine._last_timestamp = None if payload.get("last_timestamp") is None else int(payload["last_timestamp"])
    engine._seq = int(payload.get("seq", 0))
    engine._next_order_id = int(payload.get("next_order_id", max([0] + [o.order_id for o in engine.broker.all_orders()]) + 1))
    engine.ctx = engine.ctx or None
    return engine
