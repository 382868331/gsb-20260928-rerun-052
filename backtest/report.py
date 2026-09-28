"""每次运行落盘完整账目与参数：结果可独立复算、可审计。"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .account import replay_ledger
from .engine import Result
from .models import OrderStatus


def save_run(
    result: Result,
    out_dir: str | Path,
    params: dict | None = None,
) -> Path:
    """写出一次运行的全部资料，返回运行目录。

    目录内容：
      params.json   运行参数与元信息
      bars.csv      使用的人工行情
      ledger.csv    资金与持仓流水（唯一事实来源）
      fills.csv     逐笔成交（含手续费）
      orders.csv    全部委托（含未成交/部分成交/撤单/拒单）
      open_orders.csv 结束时仍未成交的委托，单独保留
      reconciliation.json 由流水独立复算的对账结果
    """
    run_dir = Path(out_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    with (run_dir / "params.json").open("w", encoding="utf-8") as f:
        json.dump({
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "params": params or {},
        }, f, ensure_ascii=False, indent=2, default=str)

    _write_csv(run_dir / "bars.csv", [
        {"symbol": b.symbol, "ts": b.ts, "open": b.open, "high": b.high,
         "low": b.low, "close": b.close, "volume": b.volume}
        for b in result.bars])

    _write_csv(run_dir / "ledger.csv", [asdict(e)
                                        for e in result.account.ledger])

    _write_csv(run_dir / "fills.csv", [asdict(fill)
                                      for fill in result.fills])

    order_rows = []
    for o in result.orders.values():
        row = asdict(o)
        row["side"] = o.side.value
        row["order_type"] = o.order_type.value
        row["status"] = o.status.value
        order_rows.append(row)
    _write_csv(run_dir / "orders.csv", order_rows)

    open_rows = [r for r in order_rows
                 if r["status"] in (OrderStatus.NEW.value,
                                    OrderStatus.PARTIALLY_FILLED.value)]
    _write_csv(run_dir / "open_orders.csv", open_rows)

    replay_cash, replay_pos = replay_ledger(result.account.ledger)
    positions = dict(result.account.positions)
    reconciliation = {
        "cash_engine": round(result.account.cash, 6),
        "cash_replayed": round(replay_cash, 6),
        "cash_match": abs(replay_cash - result.account.cash) < 1e-6,
        "positions_engine": positions,
        "positions_replayed": replay_pos,
        "positions_match": replay_pos == {k: v for k, v in positions.items()
                                          if v != 0} or replay_pos == positions,
        "n_fills": len(result.fills),
        "n_orders": len(result.orders),
        "n_open_orders": len(open_rows),
    }
    with (run_dir / "reconciliation.json").open("w",
                                                encoding="utf-8") as f:
        json.dump(reconciliation, f, ensure_ascii=False, indent=2)
    return run_dir


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
