import json

from backtest.report import save_run
from tests.test_engine import ScriptedStrategy, make_bars3, run


def test_save_run_artifacts_and_open_orders(tmp_path):
    result, strat = run({
        2: [("buy_limit", "a", 100, 50.0)],   # 永不成交，结束时仍挂着
    })
    run_dir = save_run(result, tmp_path, params={"tag": "t"})

    for name in ("params.json", "bars.csv", "ledger.csv", "fills.csv",
                 "orders.csv", "open_orders.csv",
                 "reconciliation.json"):
        assert (run_dir / name).exists()

    open_csv = (run_dir / "open_orders.csv").read_text(encoding="utf-8")
    assert str(strat.ids["a"]) in open_csv

    rec = json.loads((run_dir / "reconciliation.json").read_text())
    assert rec["cash_match"] and rec["positions_match"]
    assert rec["n_open_orders"] == 1

    params = json.loads((run_dir / "params.json").read_text())
    assert params["params"] == {"tag": "t"}
