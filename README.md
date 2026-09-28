# 策略回测引擎（第一版）

纯 Python 标准库实现的事件驱动回测框架。行情人工生成，不接券商；
行情、策略、撮合、账户互相解耦——新增策略只需继承 `Strategy`，
不用重写账户与账目。

## 目录

- `backtest/models.py`：Bar / Order / Fill / LedgerEntry 数据模型
- `backtest/data.py`：人工合成行情
- `backtest/strategy.py`：策略基类与上下文（下单/撤单的唯一入口）
- `backtest/engine.py`：事件推进与撮合
- `backtest/account.py`：现金、持仓与不可变流水（可独立复算）
- `backtest/fees.py`：手续费模型（按比例 + 最低收费）
- `backtest/report.py`：每次运行落盘完整账目与参数
- `run_demo.py`：演示入口
- `tests/`：关键行为测试

## 时间与可见性约定

1. 行情按 bar 逐根到达，`on_bar` 只能看到当前这一根 bar，无法看到未来。
2. **挂单不等于成交**：当根 bar 挂出的订单最早下一根 bar 才参与撮合。
3. 每根 bar 先统一处理撤单，再撮合。同一时刻撤单与成交相撞时
   **撤单优先**（见 `test_cancel_wins_over_fill_at_same_moment`）。
4. 撮合顺序：市价单优先，其余按 FIFO；bar 成交量在订单间依次分配，
   量不够即部分成交，剩余留到后续 bar；可随时再撤剩余部分。
5. 买单受现金约束（按手续费精算最多可买股数），卖单受持仓约束，
   不允许透支或裸卖空。
6. 结束时仍未成交的委托不丢弃，单独写入 `open_orders.csv`。

限价成交判定用整根 bar 的 high/low 判断是否触价，成交价取
开盘价与限价中更优者（保守的第一版近似，非逐笔盘口）。

## 运行

```bash
python run_demo.py          # 写出 runs/demo/ 下的完整账目
pytest                      # 运行测试
```

每次运行输出：`params.json`（参数）、`bars.csv`（行情）、
`ledger.csv`（资金/持仓流水）、`fills.csv`（成交+手续费）、
`orders.csv`（全部委托）、`open_orders.csv`（结束时未成交单）、
`reconciliation.json`（只依据流水独立复算现金与持仓的对账结果）。

## 新策略

继承 `backtest.strategy.Strategy`，实现 `on_bar(ctx, bar)`，通过
`ctx.buy / ctx.sell / ctx.cancel` 操作即可，账户与账目由引擎统一维护。
