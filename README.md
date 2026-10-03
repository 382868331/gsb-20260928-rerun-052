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
| `ledger.csv` | 逐笔资金/持仓账目，含每笔成交后的现金、持仓余额 |
| `equity_curve.csv` | 每根 bar 结算后的现金、持仓市值、总权益 |
| `event_trace.csv` | 事件推进全轨迹（时刻、优先级、序号、明细） |
| `open_orders.csv` | **结束时仍未完全成交的订单（含剩余数量），单独保留** |
| `reconciliation.json` | 仅凭 `初始现金 + fills` 独立复算与引擎账目的核对结论 |

现金与持仓的复算完全独立于引擎内部状态：

```python
from backtest.account import recompute_from_fills
rec = recompute_from_fills(result.fills, initial_cash=result.initial_cash)
assert rec.cash == result.final_cash
assert rec.positions == result.final_positions
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

---

# 第二版：成交确认与实际交收分离（结算托管层）

第一版账户在收到 Fill 的同一刻就改写现金与持仓。真实账户里**成交确认（T 日）
与实际交收（T+n）是两件事**：买入成交后证券尚未到账、卖出成交后款项尚未可用。
`backtest/settlement.py` 新增与撮合层解耦的托管账户 `SettlementCustodian`，
对每一笔 Fill 建立逐笔对应的待交收义务。

## 资金与持仓的三种状态

每个币种的资金分为（`CashBalance`）：

| 字段 | 含义 |
| --- | --- |
| `available` | 可支付资金 |
| `frozen` | 买入成交后已冻结、待交收日实际付出的本金与费用 |
| `receivable` | 卖出应收款 / 应收红利，**在实际到账前不是可支付资金** |

每个证券的持仓分为（`PositionBalance`）：`settled`（已交收净持仓）、
`frozen`（已卖出待交收冻结）；可卖数量 = `settled - frozen`，
在途买入证券未交收前不可卖。

- 买入确认：`available → frozen`（本金与费用逐笔冻结），义务到期交收时
  `frozen` 实际付出、证券进入 `settled`。
- 卖出确认：持仓进入 `frozen`，净应收（金额−费用）记 `receivable`，
  费用在成交日即从 `available` 扣除；到期交收时证券付出、
  `receivable → available`。
- 资金不足 / 持仓不足在成交确认时直接拒绝（`InsufficientFunds` /
  `InsufficientPosition`）。

## 按资产配置交收日历与币种

```python
cn = SettlementCalendar("CN", frozenset({date(2026, 10, 1), ...}))
us = SettlementCalendar("US", frozenset({date(2026, 10, 12)}))
assets = [
    AssetSpec("600000", "CNY", settlement_days=1, calendar=cn),   # A股 T+1
    AssetSpec("AAPL",  "USD", settlement_days=2, calendar=us),   # 美股 T+2
]
cust = SettlementCustodian(assets, initial_cash={"CNY": 200000, "USD": 20000},
                           initial_positions={"600000": 2000})
cust.confirm_fill(fill, trade_date)     # 成交确认：登记义务 + 冻结
cust.advance(date(2026, 10, 9))         # 逐日推进：到期义务交收、红利到账
```

交收日 = 成交日按**该资产自己的日历**推 n 个交收工作日（遇各自节假日顺延）；
跨币种义务全部按各自币种记账，系统不做任何自动换汇。义务到期顺序固定按
`obligation_id`；**同日卖出交收到账的资金在当批次买入义务之后才成为
available**，同一批次内不会被提前挪用。

## 部分交收、失败与补交

- 到期交收数量由 `fail_planner(obligation, ctx)` 决定；内置
  `ManualFailPlanner({obligation_id: {第几次尝试: 数量}})` 可人工指定
  全额失败（0）、部分交收或全额交收，默认策略 `always_settle`。
- 部分交收时现金、费用、证券均按 `本次数量 / 未结数量` **同比例分摊**，
  剩余义务顺延到资产日历的下一工作日。
- 失败不移动任何现金/证券，义务保留为 `FAILED`（曾部分交收则为
  `PARTIALLY_SETTLED`），交收日顺延；可配置失败罚金（`fail_fee`，
  非交易费用）。补交只是现金/证券的实际交割，**不再次产生交易盈亏**——
  交易盈亏只在成交确认时按成交价与成本基础确认一次。
- 每笔义务保留全部 `legs`（成功与失败的每次尝试、数量、金额、原因），
  冻结、费用、持仓数量都可逐笔回溯。

## 交收日前的拆分与现金分配

权益归属统一采用**成交日经济所有权（trade-date entitlement）**：

- 拆分（`apply_split`，如 1 拆 2）：已交收持仓、卖出冻结、买入应收义务、
  卖出应付义务全部乘 `ratio`；义务单价与每股成本基础除以 `ratio`，
  **现金应收/应付总额不变，拆分不产生盈亏**。
- 现金分配（`apply_cash_dividend`）：登记日按
  `settled + 买入在途应收 − 卖出在途冻结` 计算红利权益，即交收途上的
  买入方享有红利；红利在支付日前记 `receivable`，支付日
  `advance` 时才进入 `available`。

## 存档接续与独立复算

```python
data = cust.save_dict()                 # JSON 存档：资产、日历、全部状态、事件日记账
cust2 = SettlementCustodian.restore(data, fail_planner=...)   # 保存后接续
replayed = replay_journal(data)         # 仅凭事件日记账独立重放，不读取余额快照
```

`replay_journal` 不信任存档余额，只用初始资金/持仓与确认、交收/失败、
拆分、红利事件重建状态；失败数量已随交收事件留痕，因此复算无需 fail_planner。

## 人工过程演练与测试

```bash
python -m examples.settlement_walkthrough   # 跨节假日买卖 + 同日资金依赖 + 一笔失败
python -m unittest tests.test_settlement -v
```

演练对同一场景跑两条时间线（连续推进 vs 每个交易日存档、次日接续），
并在每个比较点核对**可用余额、持仓、应收应付与全部未结义务**逐字段一致，
最后再与日记账重放三方对账。场景含：9/30 成交的 A 股跨国庆假期 T+1
（顺延到 10/9）、10/9 成交的美股跨周末与 10/12 假期 T+2（10/14 交收）、
T+0 ETF 同日买卖的资金隔离、以及 A 股买入在 10/9 全额失败后 10/12 补交。
