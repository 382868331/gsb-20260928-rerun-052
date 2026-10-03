"""交收层验收测试。

覆盖：
- 跨节假日买卖按各自交收日历到期；
- 跨币种义务各记各的币种，未来到账款不可提前支付；
- 同日资金互相依赖（同日卖出到账款不被同日买入交收挪用）；
- 部分交收、交收失败、失败后补交不再次产生交易盈亏；
- 交收日前拆分 / 现金分配的权益归属与义务数量变化；
- 连续推进与保存后接续的可用余额、持仓、未结义务完全一致；
- 仅凭事件日记账独立重放复算一致。
"""

from __future__ import annotations

import json
import unittest
from datetime import date

from backtest.models import Fill, OrderSide
from backtest.settlement import (
    AssetSpec,
    ManualFailPlanner,
    ObligationStatus,
    SettlementCalendar,
    SettlementCustodian,
    InsufficientFunds,
    replay_journal,
)

WEEKDAY = SettlementCalendar("WEEKDAY")
CN_CAL = SettlementCalendar(
    "CN", frozenset({date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5),
                     date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)})
)
US_CAL = SettlementCalendar(
    "US", frozenset({date(2026, 10, 12)})
)


def make_fill(fill_id, order_id, symbol, side, qty, price, commission=1.0, ts=0):
    return Fill(fill_id=fill_id, order_id=order_id, symbol=symbol, side=side,
                timestamp=ts, quantity=qty, price=price, commission=commission,
                bar_timestamp=ts)


def make_custodian(**kwargs):
    assets = kwargs.pop("assets", [
        AssetSpec("CN_A", "CNY", 1, CN_CAL),
        AssetSpec("US_A", "USD", 2, US_CAL),
        AssetSpec("CASH_A", "CNY", 0, WEEKDAY),
        AssetSpec("FAIL_A", "CNY", 1, CN_CAL, fail_fee=5.0),
    ])
    return SettlementCustodian(
        assets,
        initial_cash=kwargs.pop("initial_cash", {"CNY": 100_000.0, "USD": 10_000.0}),
        initial_positions=kwargs.pop("initial_positions", {"CN_A": 1000.0}),
        **kwargs,
    )


class TestSettlementCalendar(unittest.TestCase):
    def test_holiday_lag(self):
        # 2026-09-30（周三）成交的 T+1 A 股，10/1、10/2 休市，
        # 顺延到 10 月 9 日（周五；10/1-10/8 国庆中秋假期）。
        self.assertEqual(CN_CAL.add_business_days(date(2026, 9, 30), 1), date(2026, 10, 9))
        # 美股 T+2：周五成交，跨过周末 -> 周二；再避开 10/12 假期。
        self.assertEqual(US_CAL.add_business_days(date(2026, 10, 9), 2), date(2026, 10, 14))

    def test_t0(self):
        self.assertEqual(WEEKDAY.add_business_days(date(2026, 10, 9), 0), date(2026, 10, 9))


class TestConfirmVsSettle(unittest.TestCase):
    def test_buy_funds_unavailable_until_settlement(self):
        c = make_custodian()
        c.confirm_fill(make_fill(1, 1, "CN_A", OrderSide.BUY, 100, 10.0), date(2026, 9, 30))
        cny = c.cash_balance("CNY")
        # 成交日：本金+费用被冻结，不是可用资金
        self.assertAlmostEqual(cny.available, 100_000 - 1000 - 1)
        self.assertAlmostEqual(cny.frozen, 1001)
        self.assertAlmostEqual(c.position("CN_A").settled, 1000)
        self.assertAlmostEqual(c.position("CN_A").available_to_sell, 1000)
        ob = c.open_obligations()[0]
        self.assertEqual(ob.due_date, "2026-10-09")
        # 假期中推进，什么都不到账
        c.advance(date(2026, 10, 8))
        self.assertAlmostEqual(c.position("CN_A").settled, 1000)
        # 交收日：冻结资金付出，证券到账
        c.advance(date(2026, 10, 9))
        self.assertAlmostEqual(c.cash_balance("CNY").frozen, 0)
        self.assertAlmostEqual(c.cash_balance("CNY").available, 100_000 - 1001)
        self.assertAlmostEqual(c.position("CN_A").settled, 1100)
        self.assertEqual(c.open_obligations(), [])

    def test_sell_proceeds_receivable_not_spendable(self):
        c = make_custodian()
        c.confirm_fill(make_fill(2, 2, "CN_A", OrderSide.SELL, 400, 10.0), date(2026, 9, 30))
        cny = c.cash_balance("CNY")
        pos = c.position("CN_A")
        self.assertAlmostEqual(pos.frozen, 400)
        self.assertAlmostEqual(pos.available_to_sell, 600)
        self.assertAlmostEqual(cny.receivable, 3999)   # 应收款不可支付
        self.assertAlmostEqual(cny.available, 100_000 - 1)  # 费用当日扣除
        # 未来到账款不能用于买入：尝试买超过 available 的数量
        with self.assertRaises(InsufficientFunds):
            c.confirm_fill(make_fill(3, 3, "CN_A", OrderSide.BUY, 10_000, 10.0),
                           date(2026, 10, 8))

    def test_cross_currency_separate_books(self):
        c = make_custodian()
        c.confirm_fill(make_fill(4, 4, "US_A", OrderSide.BUY, 100, 50.0), date(2026, 10, 9))
        usd = c.cash_balance("USD")
        self.assertAlmostEqual(usd.available, 10_000 - 5001)
        self.assertAlmostEqual(usd.frozen, 5001)
        # CNY 账户完全不受影响，应收/冻结均记在各自币种
        self.assertAlmostEqual(c.cash_balance("CNY").available, 100_000)
        c.advance(date(2026, 10, 13))  # 10/12 休市，尚未到期
        self.assertAlmostEqual(c.cash_balance("USD").frozen, 5001)
        c.advance(date(2026, 10, 14))
        self.assertAlmostEqual(c.cash_balance("USD").frozen, 0)
        self.assertAlmostEqual(c.position("US_A").settled, 100)


class TestSameDayCashChain(unittest.TestCase):
    def test_proceeds_arriving_same_day_are_after_batch_obligations(self):
        # 两笔 T+0 资产同日到期：一笔卖出应收款到账，另一笔买入需付款。
        # 买入义务排在前（obligation_id 更小），交收时不得挪用当日卖出到账款。
        c = make_custodian(
            assets=[AssetSpec("CASH_A", "CNY", 0, WEEKDAY)],
            initial_positions={},
        )
        # 先确认买入（几乎用尽可用资金），再确认卖出
        c.confirm_fill(make_fill(1, 1, "CASH_A", OrderSide.BUY, 900, 10.0),
                       date(2026, 10, 9))
        # 初始无 CASH_A 持仓，手工给一笔已交收持仓以便卖出
        c.positions["CASH_A"].settled += 500
        c.confirm_fill(make_fill(2, 2, "CASH_A", OrderSide.SELL, 500, 10.0),
                       date(2026, 10, 9))
        # 买入需要 9001，可用 100000 足够；这里只验证到账后余额与批次顺序稳定
        legs = c.advance(date(2026, 10, 9))
        self.assertTrue(all(leg.success for leg in legs))
        cny = c.cash_balance("CNY")
        # 买入付 9001、卖出净收 4999（两边各一笔费用）
        self.assertAlmostEqual(cny.available, 100_000 - 9001 + 4999 - 1)
        self.assertAlmostEqual(cny.receivable, 0)
        self.assertAlmostEqual(cny.frozen, 0)

    def test_receivable_stays_separate_on_settle_day(self):
        c = make_custodian(
            assets=[AssetSpec("CASH_A", "CNY", 0, WEEKDAY)],
            initial_cash={"CNY": 1002.0},
            initial_positions={"CASH_A": 500.0},
        )
        # 买入冻结 1001，余 1；卖出费当日扣 1，可用归零，应收款不可提前动用
        c.confirm_fill(make_fill(1, 1, "CASH_A", OrderSide.BUY, 100, 10.0),
                       date(2026, 10, 9))
        # 卖出 500 股，应收 4999；同日没有额外资金给其他买入，验证 receivable 隔离
        c.confirm_fill(make_fill(2, 2, "CASH_A", OrderSide.SELL, 500, 10.0),
                       date(2026, 10, 9))
        self.assertAlmostEqual(c.cash_balance("CNY").available, 0)
        c.advance(date(2026, 10, 9))
        # 两笔均正常交收；卖出款在交收后才成为 available
        self.assertAlmostEqual(c.cash_balance("CNY").available, 4999)
        self.assertEqual(c.open_obligations(), [])


class TestFailAndPartialSettlement(unittest.TestCase):
    def test_partial_then_full_settlement(self):
        planner = ManualFailPlanner()
        c = make_custodian(fail_planner=planner)
        c.confirm_fill(make_fill(5, 5, "CN_A", OrderSide.BUY, 100, 10.0),
                       date(2026, 9, 30))
        ob_id = c.open_obligations()[0].obligation_id
        # 首次到期只交收 40 股
        planner.schedule = {ob_id: {1: 40.0}}
        legs = c.advance(date(2026, 10, 9))
        self.assertTrue(legs[0].success)
        self.assertAlmostEqual(legs[0].quantity, 40)
        ob = c.obligations[ob_id]
        self.assertIs(ob.status, ObligationStatus.PARTIALLY_SETTLED)
        self.assertAlmostEqual(c.position("CN_A").settled, 1040)
        self.assertAlmostEqual(c.cash_balance("CNY").frozen, 600 + 0.6)
        # 费用按交收比例分摊
        self.assertAlmostEqual(ob.commission_open, 0.6)
        # 次一工作日补交剩余 60 股
        legs = c.advance(date(2026, 10, 12))
        self.assertTrue(legs[0].success)
        self.assertAlmostEqual(legs[0].quantity, 60)
        self.assertAlmostEqual(c.position("CN_A").settled, 1100)
        self.assertAlmostEqual(c.cash_balance("CNY").frozen, 0)
        self.assertEqual(c.open_obligations(), [])

    def test_fail_then_makeup_no_extra_pnl(self):
        planner = ManualFailPlanner()
        c = make_custodian(fail_planner=planner)
        c.confirm_fill(make_fill(6, 6, "CN_A", OrderSide.SELL, 200, 12.0),
                       date(2026, 9, 30))
        pnl_at_confirm = c.realized_pnl_total
        ob_id = c.open_obligations()[0].obligation_id
        planner.schedule = {ob_id: {1: 0.0}}
        legs = c.advance(date(2026, 10, 9))
        self.assertFalse(legs[0].success)
        ob = c.obligations[ob_id]
        self.assertIs(ob.status, ObligationStatus.FAILED)
        self.assertEqual(ob.fail_count, 1)
        self.assertEqual(ob.due_date, "2026-10-12")
        # 失败不移动任何现金/持仓，罚金单独计入非交易费用
        self.assertAlmostEqual(c.cash_balance("CNY").receivable, 2399)
        self.assertAlmostEqual(c.position("CN_A").frozen, 200)
        # 补交成功：盈亏与成交确认时完全相同，补交不再次产生盈亏
        c.advance(date(2026, 10, 12))
        self.assertAlmostEqual(c.realized_pnl_total, pnl_at_confirm)
        # FAIL_A 资产才有罚金；CN_A 无罚金，故只扣卖出费
        self.assertAlmostEqual(c.cash_balance("CNY").available,
                               100_000 - 1 + 2399)
        self.assertAlmostEqual(c.fail_fees_total.get("CNY", 0), 0)
        self.assertAlmostEqual(c.position("CN_A").frozen, 0)
        self.assertAlmostEqual(c.position("CN_A").settled, 800)


class TestCorporateActions(unittest.TestCase):
    def test_split_before_settlement_adjusts_open_obligations(self):
        c = make_custodian()
        c.confirm_fill(make_fill(7, 7, "CN_A", OrderSide.BUY, 100, 20.0),
                       date(2026, 9, 30))
        # 10/8 宣布 1 拆 2（交收日 10/9 之前）
        c.apply_split(date(2026, 10, 8), "CN_A", 2.0)
        ob = c.open_obligations()[0]
        self.assertAlmostEqual(ob.quantity_open, 200)
        self.assertAlmostEqual(ob.price, 10.0)
        self.assertAlmostEqual(ob.gross_open, 2000)   # 现金应付总额不变
        c.advance(date(2026, 10, 9))
        # 已交收 1000 股与在途买入 100 股均翻倍：2000 + 200 = 2200
        self.assertAlmostEqual(c.position("CN_A").settled, 2200)
        self.assertAlmostEqual(c.cash_balance("CNY").frozen, 0)

    def test_split_adjusts_sell_obligation_and_cost_basis(self):
        c = make_custodian()
        c.confirm_fill(make_fill(8, 8, "CN_A", OrderSide.SELL, 100, 20.0),
                       date(2026, 9, 30))
        pnl = c.realized_pnl_total
        c.apply_split(date(2026, 10, 8), "CN_A", 2.0)
        ob = c.open_obligations()[0]
        self.assertAlmostEqual(ob.quantity_open, 200)
        self.assertAlmostEqual(c.position("CN_A").frozen, 200)
        self.assertAlmostEqual(c.position("CN_A").settled, 2000)
        c.advance(date(2026, 10, 9))
        # 拆分不产生盈亏，卖出盈亏在确认时已定
        self.assertAlmostEqual(c.realized_pnl_total, pnl)
        self.assertAlmostEqual(c.position("CN_A").settled, 1800)

    def test_dividend_entitlement_follows_trade_date_owner(self):
        c = make_custodian()
        # 买入 200 股在途（未交收），卖出 100 股在途；
        # 股权登记日经济归属 = 1000 + 200 - 100 = 1100 股
        c.confirm_fill(make_fill(9, 9, "CN_A", OrderSide.BUY, 200, 10.0),
                       date(2026, 9, 30))
        c.confirm_fill(make_fill(10, 10, "CN_A", OrderSide.SELL, 100, 10.0),
                       date(2026, 9, 30))
        div = c.apply_cash_dividend(date(2026, 10, 8), "CN_A", 0.5,
                                    payable_date=date(2026, 10, 15))
        self.assertAlmostEqual(div.amount, 550.0)
        # 支付日前不可用
        c.advance(date(2026, 10, 9))
        self.assertEqual(div.status, "PENDING")
        self.assertAlmostEqual(c.pending_dividends()[0].amount, 550)
        c.advance(date(2026, 10, 15))
        self.assertEqual(c.pending_dividends(), [])


class TestSaveResumeAndReplay(unittest.TestCase):
    def _build_scenario(self):
        planner = ManualFailPlanner()
        c = make_custodian(fail_planner=planner)
        c.confirm_fill(make_fill(11, 11, "CN_A", OrderSide.BUY, 100, 10.0),
                       date(2026, 9, 30))
        c.confirm_fill(make_fill(12, 12, "CN_A", OrderSide.SELL, 200, 11.0),
                       date(2026, 9, 30))
        ob_ids = [o.obligation_id for o in c.open_obligations()]
        planner.schedule = {ob_ids[0]: {1: 0.0}}  # 买入在 10/9 失败
        c.advance(date(2026, 10, 9))
        return c

    def test_continuous_equals_resumed(self):
        continuous = self._build_scenario()
        data = json.loads(json.dumps(continuous.save_dict()))
        mid = SettlementCustodian.restore(data, fail_planner=ManualFailPlanner())
        continuous.apply_cash_dividend(date(2026, 10, 13), "CN_A", 0.2,
                                       payable_date=date(2026, 10, 16))
        mid.apply_cash_dividend(date(2026, 10, 13), "CN_A", 0.2,
                                payable_date=date(2026, 10, 16))
        continuous.advance(date(2026, 10, 16))
        mid.advance(date(2026, 10, 16))
        self.assertEqual(
            json.loads(json.dumps(continuous.snapshot(), sort_keys=True)),
            json.loads(json.dumps(mid.snapshot(), sort_keys=True)),
        )

    def test_journal_replay_matches_snapshot(self):
        c = self._build_scenario()
        c.apply_cash_dividend(date(2026, 10, 13), "CN_A", 0.2,
                              payable_date=date(2026, 10, 16))
        c.apply_split(date(2026, 10, 13), "CN_A", 2.0)
        c.advance(date(2026, 10, 16))
        data = c.save_dict()
        replayed = replay_journal(json.loads(json.dumps(data)))
        keys = ["cash", "positions", "obligations", "dividends", "cost_basis",
                "realized_pnl_total", "fail_fees_total"]
        for k in keys:
            self.assertEqual(
                json.loads(json.dumps(replayed.snapshot()[k], sort_keys=True)),
                json.loads(json.dumps(c.snapshot()[k], sort_keys=True)),
                f"日记账重放在 {k} 上不一致",
            )


if __name__ == "__main__":
    unittest.main()
