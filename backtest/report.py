"""运行结果落账与账目复算核对。

每次运行写入一个独立目录（runs/<时间戳>_<tag>/）：
- params.json:        完整运行参数（数据源、策略、券商、初始资金、相撞顺序）
- market_data.csv:    本次使用的全部人工行情
- orders.csv:         全部委托及最终状态
- fills.csv:          全部成交流水（含每笔手续费）
- ledger.csv:         逐笔资金/持仓账目（含成交后现金、持仓余额）
- equity_curve.csv:   每根 bar 结算后的现金、市值、总权益
- event_trace.csv:    事件推进顺序（时刻、优先级、序号、明细）
- open_orders.csv:    结束时仍未完全成交的订单（单独保留）
- reconciliation.json: 独立复算结果与引擎账目的核对结论
"""

from __future__ import annotations

import csv
import json
import os
from typing import Dict, List, Sequence

from .account import recompute_from_fills
from .engine import EngineResult


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


def reconcile(result: EngineResult, tolerance: float = 1e-6) -> Dict:
    """仅凭 fills + 初始现金独立复算现金与持仓，与引擎内部状态核对。"""
    rec = recompute_from_fills(result.fills, initial_cash=result.initial_cash)

    position_diffs = {}
    symbols = set(result.final_positions) | set(rec.positions)
    for symbol in symbols:
        diff = result.final_positions.get(symbol, 0.0) - rec.positions.get(symbol, 0.0)
        if abs(diff) > tolerance:
            position_diffs[symbol] = diff

    cash_diff = result.final_cash - rec.cash
    fee_diff = result.commission_total - rec.commission_total

    # 再用 ledger 内部余额逐行自洽检查（每条余额必须等于前一条 + 增量）
    running_cash = result.initial_cash
    running_positions: Dict[str, float] = {}
    ledger_ok = True
    for entry in getattr(result, "ledger", []):
        row = entry if isinstance(entry, dict) else entry.__dict__
        running_cash += row["delta_cash"]
        running_positions[row["symbol"]] = (
            running_positions.get(row["symbol"], 0.0) + row["delta_position"]
        )
        ledger_ok &= abs(running_cash - row["cash_after"]) <= tolerance
        ledger_ok &= abs(running_positions[row["symbol"]] - row["position_after"]) <= tolerance
    passed = (
        abs(cash_diff) <= tolerance
        and abs(fee_diff) <= tolerance
        and not position_diffs
        and ledger_ok
    )
    return {
        "passed": passed,
        "tolerance": tolerance,
        "engine_final_cash": result.final_cash,
        "recomputed_final_cash": rec.cash,
        "cash_diff": cash_diff,
        "engine_final_positions": result.final_positions,
        "recomputed_final_positions": rec.positions,
        "position_diffs": position_diffs,
        "engine_commission_total": result.commission_total,
        "recomputed_commission_total": rec.commission_total,
        "commission_diff": fee_diff,
        "n_fills": len(result.fills),
    }


def save_run(
    result: EngineResult,
    output_dir: str,
    market_bars: Sequence = (),
    ledger: Sequence = (),
    open_orders: Sequence = (),
) -> str:
    os.makedirs(output_dir, exist_ok=True)

    with open(os.path.join(output_dir, "params.json"), "w", encoding="utf-8") as f:
        json.dump(result.params, f, ensure_ascii=False, indent=2, sort_keys=True)

    if market_bars:
        _write_csv(os.path.join(output_dir, "market_data.csv"), [b.to_row() for b in market_bars])

    _write_csv(os.path.join(output_dir, "orders.csv"), [o.to_row() for o in result.orders])
    _write_csv(os.path.join(output_dir, "fills.csv"), [f.to_row() for f in result.fills])
    _write_csv(
        os.path.join(output_dir, "equity_curve.csv"),
        [p.to_row() for p in result.equity_curve],
    )
    _write_csv(os.path.join(output_dir, "event_trace.csv"), [t.to_row() for t in result.event_trace])
    _write_csv(
        os.path.join(output_dir, "open_orders.csv"),
        [_open_order_row(o) for o in open_orders],
    )
    if ledger:
        rows = [e if isinstance(e, dict) else e.to_row() for e in ledger]
        _write_csv(os.path.join(output_dir, "ledger.csv"), rows)

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
