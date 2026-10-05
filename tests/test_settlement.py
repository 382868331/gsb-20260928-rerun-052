from __future__ import annotations

import tempfile
import unittest

from backtest.broker import SettlementCalendar
from backtest.models import Fill, OrderSide
from backtest.report import load_settlement_state, save_run
from backtest.settlement import CorporateAction, SettlementAccount


def make_fill(fill_id=1, quantity=100.0, price=10.0, side=OrderSide.BUY, currency="USD", timestamp=1, due=3):
    return Fill(
        fill_id=fill_id,
        order_id=fill_id,
        symbol="AAA",
        side=side,
        timestamp=timestamp,
        quantity=quantity,
        price=price,
        commission=1.0,
        bar_timestamp=timestamp,
        currency=currency,
        settlement_date=due,
    )

class TestSettlementAccount(unittest.TestCase):
    def test_holiday_calendar_separates_confirmation_and_settlement(self):
        calendar = SettlementCalendar(lag=2, holidays={2})
        self.assertEqual(calendar.settlement_date(1), 4)

    def test_buy_funds_are_frozen_until_due_date(self):
        account = SettlementAccount(initial_cash=1000.0, currency="USD")
        account.apply_fill(make_fill(quantity=10.0, price=10.0, due=3))
        self.assertAlmostEqual(account.available_cash["USD"], 899.0)
        self.assertAlmostEqual(account.frozen_cash["USD"], 101.0)
        self.assertEqual(account.positions, {})
        account.process_due(2)
        self.assertAlmostEqual(account.available_cash["USD"], 899.0)
        account.process_due(3)
        self.assertAlmostEqual(account.available_cash["USD"], 899.0)
        self.assertEqual(account.frozen_cash.get("USD"), 0.0)
        self.assertEqual(account.positions, {"AAA": 10.0})

    def test_cross_currency_obligations_do_not_net_future_receipts(self):
        account = SettlementAccount(initial_cash=1000.0, currency="USD")
        account.available_cash["EUR"] = 1000.0
        account.positions["AAA"] = 10.0
        account.apply_fill(make_fill(fill_id=1, side=OrderSide.SELL, quantity=10.0, price=10.0, currency="EUR", due=3))
        self.assertNotIn("EUR", account.frozen_cash)
        self.assertAlmostEqual(account.available_cash["EUR"], 999.0)
        self.assertAlmostEqual(account.frozen_positions["AAA"], 10.0)
        account.process_due(3)
        self.assertAlmostEqual(account.available_cash["EUR"], 1099.0)

    def test_partial_failure_retry_uses_original_terms(self):
        account = SettlementAccount(initial_cash=1000.0)
        obligation = account.apply_fill(make_fill(quantity=10.0, price=10.0, due=3))
        account.process_due(3, failures={obligation.obligation_id: 4.0})
        self.assertEqual(obligation.open_quantity, 0.0)
        self.assertEqual(obligation.failed_quantity, 4.0)
        self.assertEqual(account.positions, {"AAA": 6.0})
        self.assertAlmostEqual(account.available_cash["USD"], 939.4)
        account.retry(obligation.obligation_id, 4, 4.0)
        self.assertEqual(account.positions, {"AAA": 10.0})
        self.assertAlmostEqual(account.available_cash["USD"], 899.0)
        self.assertEqual(account.open_obligations(), [])
        actions = [event.action for event in account.events]
        self.assertEqual(actions, ["CONFIRM", "SETTLE", "FAIL", "RETRY_FREEZE", "SETTLE"])

    def test_split_and_cash_distribution_before_settlement(self):
        account = SettlementAccount(initial_cash=1000.0)
        account.positions["AAA"] = 5.0
        obligation = account.apply_fill(make_fill(quantity=10.0, price=10.0, due=4))
        account.apply_corporate_action(CorporateAction(timestamp=2, symbol="AAA", split_ratio=2.0, cash_per_share=1.0))
        self.assertEqual(account.positions, {"AAA": 10.0})
        self.assertAlmostEqual(account.available_cash["USD"], 904.0)
        account.process_due(4)
        self.assertEqual(account.positions, {"AAA": 30.0})
        self.assertEqual(obligation.settled_quantity, 10.0)
        self.assertEqual(obligation.split_ratio, 2.0)

    def test_continuous_and_resumed_manual_process_have_identical_balances(self):
        def snapshot(account):
            return (
                dict(account.available_cash),
                dict(account.frozen_cash),
                dict(account.positions),
                dict(account.frozen_positions),
                [(o.obligation_id, o.open_quantity, o.failed_quantity, o.status.value) for o in account.open_obligations()],
            )

        continuous = SettlementAccount(initial_cash=1000.0)
        resumed = SettlementAccount(initial_cash=1000.0)
        for account in (continuous, resumed):
            buy = account.apply_fill(make_fill(fill_id=1, quantity=10.0, due=3))
            account.process_due(3, failures={buy.obligation_id: 10.0})
        continuous.retry(1, 4, 10.0)
        with tempfile.TemporaryDirectory() as tmp:
            class DummyResult:
                params = {}
                orders = []
                equity_curve = []
                event_trace = []
                initial_cash = 1000.0
                final_cash = sum(resumed.available_cash.values())
                final_positions = dict(resumed.positions)
                available_cash = dict(resumed.available_cash)
                frozen_cash = dict(resumed.frozen_cash)
                frozen_positions = dict(resumed.frozen_positions)
                fills = [make_fill(fill_id=1, quantity=10.0, due=3)]
                settlement_events = list(resumed.events)
                settlement_obligations = list(resumed.obligations)
            save_run(DummyResult(), tmp)
            loaded = load_settlement_state(tmp)
        loaded.retry(1, 4, 10.0)
        self.assertEqual(snapshot(continuous), snapshot(loaded))


if __name__ == "__main__":
    unittest.main()
