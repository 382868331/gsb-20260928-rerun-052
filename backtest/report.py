"""运行结果落账与交收账目核对。"""

from __future__ import annotations

import csv
import json
import os
from typing import Dict, List, Sequence

from .engine import EngineResult
from .models import OrderSide
from .settlement import ObligationStatus, SettlementAccount, SettlementEvent, SettlementObligation


def _write_csv(path: str, rows: List[dict]) -> None:
    fieldnames: List[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _value(entry, key):
    return entry[key] if isinstance(entry, dict) else getattr(entry, key)


def reconcile(result: EngineResult, tolerance: float = 1e-6) -> Dict:
    """从成交确认和交收事件重建可用/冻结现金与证券。"""
    fills_by_id = {fill.fill_id: fill for fill in result.fills}
    available_cash = {"USD": result.initial_cash}
    frozen_cash: Dict[str, float] = {}
    positions: Dict[str, float] = {}
    frozen_positions: Dict[str, float] = {}
    split_by_fill = {_value(o, "fill_id"): _value(o, "split_ratio") for o in result.settlement_obligations}

    for entry in result.settlement_events:
        fill = fills_by_id[_value(entry, "fill_id")]
        currency = _value(entry, "currency")
        symbol = _value(entry, "symbol")
        side = _value(entry, "side")
        quantity = _value(entry, "quantity")
        amount = _value(entry, "gross_amount")
        commission = _value(entry, "commission")
        action = _value(entry, "action")
        signed_split = split_by_fill.get(fill.fill_id, 1.0)
        if action == "CONFIRM":
            if side == OrderSide.BUY.value:
                available_cash[currency] = available_cash.get(currency, 0.0) - amount - commission
                frozen_cash[currency] = frozen_cash.get(currency, 0.0) + amount + commission
            else:
                positions[symbol] = positions.get(symbol, 0.0) - quantity
                frozen_positions[symbol] = frozen_positions.get(symbol, 0.0) + quantity
                available_cash[currency] = available_cash.get(currency, 0.0) - commission
        elif action == "SETTLE":
            if side == OrderSide.BUY.value:
                frozen_cash[currency] -= amount + commission
                positions[symbol] = positions.get(symbol, 0.0) + quantity * signed_split
            else:
                frozen_positions[symbol] -= quantity
                available_cash[currency] = available_cash.get(currency, 0.0) + amount
        elif action == "FAIL":
            if side == OrderSide.BUY.value:
                frozen_cash[currency] -= amount + commission
                available_cash[currency] = available_cash.get(currency, 0.0) + amount + commission
            else:
                frozen_positions[symbol] -= quantity
                positions[symbol] = positions.get(symbol, 0.0) + quantity
                available_cash[currency] = available_cash.get(currency, 0.0) + commission
        elif action == "RETRY_FREEZE":
            if side == OrderSide.BUY.value:
                available_cash[currency] -= amount + commission
                frozen_cash[currency] = frozen_cash.get(currency, 0.0) + amount + commission
            else:
                positions[symbol] -= quantity
                frozen_positions[symbol] = frozen_positions.get(symbol, 0.0) + quantity

    cash_diff = sum(available_cash.values()) - result.final_cash
    position_diffs = {
        symbol: positions.get(symbol, 0.0) - result.final_positions.get(symbol, 0.0)
        for symbol in set(positions) | set(result.final_positions)
        if abs(positions.get(symbol, 0.0) - result.final_positions.get(symbol, 0.0)) > tolerance
    }
    return {
        "passed": abs(cash_diff) <= tolerance and not position_diffs,
        "tolerance": tolerance,
        "recomputed_available_cash": available_cash,
        "recomputed_frozen_cash": frozen_cash,
        "recomputed_positions": positions,
        "recomputed_frozen_positions": frozen_positions,
        "engine_final_cash": result.final_cash,
        "cash_diff": cash_diff,
        "position_diffs": position_diffs,
        "n_fills": len(result.fills),
        "n_open_obligations": len([
            o for o in result.settlement_obligations
            if _value(o, "open_quantity") + _value(o, "failed_quantity") > 0
        ]),
    }


def save_run(result: EngineResult, output_dir: str, market_bars: Sequence = (), ledger: Sequence = (), open_orders: Sequence = ()) -> str:
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "params.json"), "w", encoding="utf-8") as f:
        json.dump(result.params, f, ensure_ascii=False, indent=2, sort_keys=True)
    if market_bars:
        _write_csv(os.path.join(output_dir, "market_data.csv"), [b.to_row() for b in market_bars])
    _write_csv(os.path.join(output_dir, "orders.csv"), [o.to_row() for o in result.orders])
    _write_csv(os.path.join(output_dir, "fills.csv"), [f.to_row() for f in result.fills])
    _write_csv(os.path.join(output_dir, "settlement_events.csv"), [e.to_row() for e in result.settlement_events])
    _write_csv(os.path.join(output_dir, "settlement_obligations.csv"), [o.to_row() for o in result.settlement_obligations])
    state = {
        "initial_cash": result.initial_cash,
        "base_currency": "USD",
        "available_cash": result.available_cash,
        "frozen_cash": result.frozen_cash,
        "positions": result.final_positions,
        "frozen_positions": result.frozen_positions,
        "obligations": [o.to_row() for o in result.settlement_obligations],
        "events": [e.to_row() for e in result.settlement_events],
    }
    with open(os.path.join(output_dir, "settlement_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    _write_csv(os.path.join(output_dir, "equity_curve.csv"), [p.to_row() for p in result.equity_curve])
    _write_csv(os.path.join(output_dir, "event_trace.csv"), [t.to_row() for t in result.event_trace])
    _write_csv(os.path.join(output_dir, "open_orders.csv"), [_open_order_row(o) for o in open_orders])
    if ledger:
        _write_csv(os.path.join(output_dir, "ledger.csv"), [e.to_row() for e in ledger])
    reconciliation = reconcile(result)
    with open(os.path.join(output_dir, "reconciliation.json"), "w", encoding="utf-8") as f:
        json.dump(reconciliation, f, ensure_ascii=False, indent=2, sort_keys=True)
    if not reconciliation["passed"]:
        raise AssertionError(f"账目复算核对失败: {reconciliation}")
    return output_dir


def _open_order_row(order) -> dict:
    row = order.to_row()
    row["remaining_quantity"] = order.remaining_quantity
    return row


def load_settlement_state(output_dir: str) -> SettlementAccount:
    """从落盘快照恢复交收账户，供人工失败补交后接续。"""
    with open(os.path.join(output_dir, "settlement_state.json"), encoding="utf-8") as f:
        state = json.load(f)
    obligations = [
        SettlementObligation(
            obligation_id=int(row["obligation_id"]),
            fill_id=int(row["fill_id"]),
            order_id=int(row["order_id"]),
            symbol=row["symbol"],
            currency=row["currency"],
            side=row["side"],
            trade_timestamp=int(row["trade_timestamp"]),
            due_date=int(row["due_date"]),
            original_quantity=float(row["original_quantity"]),
            open_quantity=float(row["open_quantity"]),
            settled_quantity=float(row["settled_quantity"]),
            failed_quantity=float(row["failed_quantity"]),
            price=float(row["price"]),
            commission=float(row["commission"]),
            status=ObligationStatus(row["status"]),
            split_ratio=float(row["split_ratio"]),
        )
        for row in state["obligations"]
    ]
    events = [SettlementEvent(**{k: (None if v == "" else v) for k, v in row.items()}) for row in state["events"]]
    return SettlementAccount.restore(
        initial_cash=float(state["initial_cash"]),
        available_cash={k: float(v) for k, v in state["available_cash"].items()},
        frozen_cash={k: float(v) for k, v in state["frozen_cash"].items()},
        positions={k: float(v) for k, v in state["positions"].items()},
        frozen_positions={k: float(v) for k, v in state["frozen_positions"].items()},
        obligations=obligations,
        events=events,
        base_currency=state.get("base_currency", "USD"),
    )
