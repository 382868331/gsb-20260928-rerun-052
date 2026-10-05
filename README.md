# 事件驱动策略回测引擎（第一版）

纯 Python 标准库实现，不接任何券商或外部行情。目标：跑通
**事件推进、策略决策、模拟成交、资金记录** 四件事，行情/策略/账户彼此分离。

## 目录结构

```
backtest/
  models.py    # Bar / Order / Fill 数据结构（挂单与成交严格区分）
  events.py    # 事件类型与同刻优先级
  data.py      # 人工行情源（可复现合成行情 / 手工构造行情）
  broker.py    # 挂单、撤单、部分成交、手续费模拟撮合
  account.py   # 现金、持仓、流水；仅凭 fills 的独立复算函数
  strategy.py  # Strategy 基类与受限 Context（信息隔离）
  engine.py    # 优先级事件队列主循环
  report.py    # 每次运行落账 + 账目核对
  cli.py       # 命令行入口
tests/         # 验收测试（含同刻撤单/成交相撞）
runs/          # 每次运行生成一个独立账目目录
```

## 同刻事件顺序（回测确定性的核心约定）

每个行情时刻严格按优先级处理，序号相同刻同优先级时再按自增序号排：

| 优先级 | 事件 | 说明 |
| --- | --- | --- |
| 10 | market | 行情到达，先更新可见行情 |
| 20 | strategy | 策略 `on_bar` 决策，只可见当前及历史 bar |
| 30 | cancel | 撤单请求，**先于撮合** |
| 40 | submit | 新单入账，**当根 bar 不参与撮合** |
| 50 | match | 对存量订单撮合，产生 0..n 笔部分成交 |
| 60 | fill | 成交回报，更新账户并回调 `on_fill` |

因此「同一时刻撤单与成交相撞」的默认结果是**撤单先于成交**：被撤订单不会在该时刻成交。
该规则可通过 `BacktestEngine(cancel_before_match=False)` 反转为先成交后撤单（撤单将失败），
两种顺序都记入 `params.json` 并由测试固定（见 `tests/test_engine.py`）。

## 撮合与费用规则（第一版，可配置）

- 限价单：买入在 `bar.low <= limit` 时成交，卖出在 `bar.high >= limit` 时成交；成交价取限价。
- 市价单：按当根 `bar.close` 成交。
- 部分成交：每根 bar 可供成交量 = `bar.volume * max_participation`（默认 10%），
  多个订单按挂单时间先后分配；不足部分继续挂单，后续 bar 继续成交。
- 新单在提交当根 bar 不撮合，避免使用当根未来信息。
- 手续费 = `max(成交金额 * commission_rate, min_commission)`，每笔成交单独收取并入账。
- 挂单、撤单本身不改变任何现金与持仓；账户只承认 `Fill`。

## 成交确认与交收分离

- 成交回报只生成逐笔 `SettlementObligation`：买入立即冻结对应币种资金，卖出立即冻结证券并扣收手续费。
- 未到交收日的买入证券不进入可用持仓，卖出回款不进入可支付现金；跨币种成交分别按 `Fill.currency` 记录义务和币种余额。
- `SettlementCalendar(lag, holidays)` 可按资产配置，交收日按营业日跨过节假日；默认 T+2。
- 到期允许部分成功、部分失败；失败部分解除原冻结并继续以 `failed_quantity` 挂账。
- 人工 `retry()` 补交沿用原成交数量、价格和费用，只完成原义务，不产生新的交易盈亏。
- 交收日前拆分：现金分配归属截至权益登记口径的已交收持仓，未完成买方义务按拆分比例收足调整后证券，未完成卖方义务按调整后数量交割。
- 每个 bar 的成交事件全部处理后才执行当日到期交收，因此同日卖出回款只有在该卖出本身同日交收时，才可用于后一笔买入依赖。
- `settlement_events.csv` 是成交确认、成功、失败和补交冻结/交收的完整人工过程；`settlement_obligations.csv` 保留全部未结数量。
- 连续推进与保存后接续都应以同一组事件核对 `available_cash`、`frozen_cash`、`positions`、`frozen_positions` 和未结义务。

## 运行

```bash
python -m backtest.cli --bars 60 --seed 42 --tag demo
python -m backtest.cli --match-before-cancel --tag reverse_order   # 反转相撞顺序
python -m unittest discover -s tests -v
```

## 每次运行留下的账目（`runs/<时间戳>_<tag>/`）

| 文件 | 内容 |
| --- | --- |
| `params.json` | 完整运行参数（数据源、种子、策略、券商、初始资金、相撞顺序） |
| `market_data.csv` | 本次使用的全部人工行情 |
| `orders.csv` | 全部委托及最终状态（含拒单、撤单、部分成交） |
| `fills.csv` | 全部成交流水（数量、价格、金额、手续费） |
| `settlement_events.csv` | 成交确认、交收、失败、补交冻结的逐笔过程 |
| `settlement_obligations.csv` | 每笔成交义务的原始、已交收、失败和未结数量 |
| `settlement_state.json` | 保存后接续所需的可用/冻结现金、证券和未结义务快照 |
| `ledger.csv` | 兼容保留的交收事件流水 |
| `equity_curve.csv` | 每根 bar 结算后的现金、持仓市值、总权益 |
| `event_trace.csv` | 事件推进全轨迹（时刻、优先级、序号、明细） |
| `open_orders.csv` | **结束时仍未完全成交的订单（含剩余数量），单独保留** |
| `reconciliation.json` | 仅凭 `初始现金 + fills` 独立复算与引擎账目的核对结论 |

交收后的现金、冻结和持仓可由成交确认与交收事件独立复算：

```python
from backtest.report import reconcile
assert reconcile(result)["passed"]
```

## 添加新策略

继承 `backtest.strategy.Strategy`，实现 `on_bar(ctx, bar)` 即可，账户、撮合、落账均无需改动：

```python
class MyStrategy(Strategy):
    name = "MyStrategy"
    def on_bar(self, ctx, bar):
        ctx.bars(symbol)           # 只含已到达的 bar
        ctx.position_view(symbol)  # 只读持仓
        ctx.submit_limit(...)      # 挂单（不等于成交）
        ctx.cancel_order(order_id) # 撤单请求
    def on_fill(self, ctx, fill):  # 可选：成交回报
        ...
```

策略无法直接触碰账户现金或订单簿，所有委托都走事件队列，因此同刻顺序对所有策略一致且可复现。
