"""第一版演示入口：人工行情 + 均线策略，账目写入 runs/。"""

from __future__ import annotations

from pathlib import Path

from backtest.data import deterministic_series
from backtest.engine import BacktestEngine
from backtest.examples import MovingAverageCross
from backtest.fees import FixedRateFee
from backtest.report import save_run


def main() -> None:
    bars = deterministic_series(n=30)
    fee = FixedRateFee(rate=0.0003, min_fee=1.0)
    strategy = MovingAverageCross(symbol="DEMO", qty=100)
    engine = BacktestEngine(bars, strategy, initial_cash=1_000_000.0,
                            fee_fn=fee)
    result = engine.run()
    run_dir = save_run(result, Path("runs") / "demo", params={
        "engine_version": "0.1.0",
        "initial_cash": 1_000_000.0,
        "fee": fee.params(),
        "strategy": {
            "class": type(strategy).__name__,
            "short": strategy.short, "long": strategy.long,
            "qty": strategy.qty,
        },
        "data": {"source": "synthetic", "n_bars": len(bars)},
    })
    print(f"成交 {len(result.fills)} 笔，" 
          f"结束现金 {result.account.cash:.2f}，" 
          f"持仓 {dict(result.account.positions)}，" 
          f"未成交委托 {len(result.open_orders)} 个")
    print(f"账目与参数已写入: {run_dir}")


if __name__ == "__main__":
    main()
