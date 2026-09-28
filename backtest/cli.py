"""命令行入口：跑一次可复现的人工数据回测并留下完整账目。

示例：
    python -m backtest.cli
    python -m backtest.cli --bars 120 --seed 7 --tag seed7
"""

from __future__ import annotations

import argparse
import datetime
import os

from .data import SyntheticDataFeed
from .engine import BacktestEngine
from .report import save_run
from .strategy import MACrossStrategy


def run_once(
    bars: int = 60,
    seed: int = 42,
    tag: str = "demo",
    out_root: str = "runs",
    cancel_before_match: bool = True,
    initial_cash: float = 1_000_000.0,
) -> str:
    feed = SyntheticDataFeed(symbol="TEST", n_bars=bars, seed=seed)
    market_bars = feed.all_bars()
    strategy = MACrossStrategy(symbol="TEST", fast=5, slow=20, quantity=100.0)
    engine = BacktestEngine(
        data_feed=feed,
        strategy=strategy,
        initial_cash=initial_cash,
        cancel_before_match=cancel_before_match,
        run_tag=tag,
    )
    result = engine.run()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_dir = os.path.join(out_root, f"{stamp}_{tag}")
    save_run(
        result,
        output_dir,
        market_bars=market_bars,
        ledger=result.ledger,
        open_orders=engine.broker.open_orders_snapshot(),
    )
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="事件驱动策略回测（人工数据）")
    parser.add_argument("--bars", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--tag", type=str, default="demo")
    parser.add_argument("--out", type=str, default="runs")
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument(
        "--match-before-cancel",
        action="store_true",
        help="同一时刻先撮合后撤单（默认先撤单后撮合）",
    )
    args = parser.parse_args()
    path = run_once(
        bars=args.bars,
        seed=args.seed,
        tag=args.tag,
        out_root=args.out,
        cancel_before_match=not args.match_before_cancel,
        initial_cash=args.initial_cash,
    )
    print(f"回测完成，账目目录: {path}")


if __name__ == "__main__":
    main()
