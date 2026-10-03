"""人工过程演练：跨节假日买卖 + 同日资金互相依赖 + 一笔交收失败。

做两条完全相同的时间线：
- 连续推进：从成交日一路 advance 到最后；
- 保存接续：每个交易日收盘后把全部状态 JSON 存档，次日恢复后再推进。

两条时间线在每一比较点的可用余额、持仓（settled/frozen）、应收应付与
全部未结义务必须逐字段一致；最后再用事件日记账独立重放核对一次。

运行：python -m examples.settlement_walkthrough
"""

from __future__ import annotations

import copy
import json
from datetime import date

from backtest.models import Fill, OrderSide
from backtest.settlement import (
    AssetSpec,
    ManualFailPlanner,
    ObligationStatus,
    SettlementCalendar,
    SettlementCustodian,
    replay_journal,
)

# 2026 年国庆中秋假期：A 股 10/1-10/8 休市；美股 10/12（哥伦布日）休市。
CN = SettlementCalendar("CN", frozenset(
    {date(2026, 10, d) for d in (1, 2, 5, 6, 7, 8)}))
US = SettlementCalendar("US", frozenset({date(2026, 10, 12)}))

ASSETS = [
    AssetSpec("600000", "CNY", 1, CN, fail_fee=2.0),  # A 股 T+1，失败罚金 2 元
    AssetSpec("AAPL", "USD", 2, US),                  # 美股 T+2
    AssetSpec("510300", "CNY", 0, CN),                # ETF T+0
]

INITIAL_CASH = {"CNY": 200_000.0, "USD": 20_000.0}
INITIAL_POS = {"600000": 2_000.0, "510300": 1_000.0}

FILLS = [
    # (fill_id, symbol, side, qty, price, commission, 成交日)
    (1, "600000", OrderSide.BUY, 500, 10.0, 1.0, date(2026, 9, 30)),
    (2, "600000", OrderSide.SELL, 800, 11.0, 1.0, date(2026, 9, 30)),
    (3, "AAPL", OrderSide.BUY, 100, 150.0, 1.0, date(2026, 10, 9)),
    # T+0 ETF 同日买卖：卖出应收款同日才到账，买入交收排在前不能挪用
    (4, "510300", OrderSide.BUY, 1_000, 4.0, 1.0, date(2026, 10, 9)),
    (5, "510300", OrderSide.SELL, 1_000, 4.0, 1.0, date(2026, 10, 9)),
]

# 600000 买入义务（obligation_id=1）在到期日 10/9 全额失败一次，次日补交。
FAIL_SCHEDULE = {1: {1: 0.0}}

STEP_DATES = [
    date(2026, 9, 30),
    date(2026, 10, 8),
    date(2026, 10, 9),
    date(2026, 10, 12),
    date(2026, 10, 14),
]


def make_fill(fid, symbol, side, qty, price, commission):
    return Fill(fill_id=fid, order_id=fid * 10, symbol=symbol, side=side,
                timestamp=0, quantity=qty, price=price, commission=commission,
                bar_timestamp=0)


def build():
    return SettlementCustodian(
        copy.deepcopy(ASSETS),
        initial_cash=dict(INITIAL_CASH),
        initial_positions=dict(INITIAL_POS),
        fail_planner=ManualFailPlanner(copy.deepcopy(FAIL_SCHEDULE)),
    )


def view(cust):
    """比较用视图：可用余额、持仓、全部未结义务、应收红利。"""
    return {
        "cash": {
            c: {"available": round(b.available, 6), "frozen": round(b.frozen, 6),
                "receivable": round(b.receivable, 6)}
            for c, b in sorted(cust.cash.items())
        },
        "positions": {
            s: {"settled": p.settled, "frozen": p.frozen,
                "available_to_sell": p.available_to_sell}
            for s, p in sorted(cust.positions.items())
        },
        "open_obligations": [
            {"id": o.obligation_id, "symbol": o.symbol, "side": o.side,
             "currency": o.currency, "due": o.due_date,
             "qty_open": o.quantity_open, "qty_settled": o.quantity_settled,
             "gross_open": round(o.gross_open, 6),
             "commission_open": round(o.commission_open, 6),
             "status": o.status.value, "fails": o.fail_count}
            for o in cust.open_obligations()
        ],
        "pending_dividends": [
            {"symbol": d.symbol, "amount": d.amount, "payable": d.payable_date}
            for d in cust.pending_dividends()
        ],
    }


def main():
    continuous = build()
    resumed = build()
    archive = None

    fills_by_date = {}
    for fid, sym, side, qty, price, fee, td in FILLS:
        fills_by_date.setdefault(td, []).append(
            (fid, sym, side, qty, price, fee, td))

    for d in STEP_DATES:
        # 接续时间线：每个交易日先从昨日存档恢复，再确认当日成交、推进交收
        if archive is not None:
            resumed = SettlementCustodian.restore(
                archive, fail_planner=ManualFailPlanner(copy.deepcopy(FAIL_SCHEDULE)))
        for fid, sym, side, qty, price, fee, td in fills_by_date.get(d, []):
            continuous.confirm_fill(make_fill(fid, sym, side, qty, price, fee), td)
            resumed.confirm_fill(make_fill(fid, sym, side, qty, price, fee), td)

        legs_c = continuous.advance(d)
        legs_r = resumed.advance(d)
        archive = json.loads(json.dumps(resumed.save_dict()))

        vc, vr = view(continuous), view(resumed)
        assert vc == vr, f"{d} 连续推进与存档接续不一致:\\n{vc}\\n{vr}"
        assert len(legs_c) == len(legs_r)
        print(f"== {d} ==")
        print(json.dumps(vc, ensure_ascii=False, indent=2))
        print(f"当日交收尝试 {len(legs_c)} 笔: "
              + ", ".join(
                  f"{'成功' if l.success else '失败'} {l.quantity:g}" for l in legs_c))
        print()

    # 全部完成后的最终核对：连续、接续、日记账重放三者一致
    final_archive = json.loads(json.dumps(continuous.save_dict()))
    replayed = replay_journal(final_archive)
    assert view(continuous) == view(replayed), "日记账重放与连续推进不一致"
    assert continuous.snapshot()["cash"] == replayed.snapshot()["cash"]
    print("全部比较点一致：连续推进 == 保存后接续 == 日记账重放")
    print(f"累计交易盈亏（仅成交确认产生）: {continuous.realized_pnl_total:.2f}")
    print(f"失败罚金: {continuous.fail_fees_total}")
    assert continuous.open_obligations() == []


if __name__ == "__main__":
    main()
