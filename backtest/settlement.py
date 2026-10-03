"""成交确认与实际交收分离的结算账户（托管层）。

核心约定：
- 成交确认（Fill / confirm_fill）只产生"待交收义务"，不立即把证券计入可
  用持仓，也不把卖出应收款计入可用资金。
- 买入：成交日冻结资金（含费用），形成"应收证券 + 应付资金"；证券在交
  收日实际到账前不可卖。
- 卖出：成交日冻结持仓，形成"应付证券 + 应收资金"；应收资金在交收日实
  际到账前不是可支付资金（receivable 与 available 严格分开）。
- 不同资产可有不同的交收周期（T+0/T+1/T+2…）与交收日历（节假日集合
  可各自不同）；跨币种义务按各自币种记账，本模块不做任何自动换汇。
- 支持部分交收、交收失败与失败后补交：补交只移动现金/证券，不再次产生
  交易盈亏；交易盈亏只在成交确认时按成交价确认一次。
- 交收日前发生拆分 / 现金分配时，按"成交日经济所有权"（trade-date
  entitlement）处理权益归属，未完成义务的数量随之调整（详见 README）。
- 全部状态可 to_dict/from_dict 做 JSON 存档接续；另有纯事件日记账重放
  函数 replay_journal，用于独立复算核对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional

from .models import Fill, OrderSide


class ObligationStatus(str, Enum):
    PENDING = "PENDING"                    # 已确认，尚未到首次交收日
    PARTIALLY_SETTLED = "PARTIALLY_SETTLED"
    FAILED = "FAILED"                      # 到日交收失败（可补交），仍是未结义务
    SETTLED = "SETTLED"


class FailDecision:
    """失败策略对某笔到期义务给出的本次交收数量。"""

    __slots__ = ("quantity", "note")

    def __init__(self, quantity: float, note: str = ""):
        self.quantity = quantity
        self.note = note


FailPlanner = Callable[["Obligation", "SettlementContext"], FailDecision]


def always_settle(obligation: "Obligation", ctx: "SettlementContext") -> FailDecision:
    """默认失败策略：到期全部交收。"""
    return FailDecision(obligation.quantity_open)


class ManualFailPlanner:
    """人工失败脚本：按 obligation_id 指定第几次尝试交收多少（0=全额失败）。

    schedule: {obligation_id: [(attempt_no, quantity), ...]}，
    attempt_no 从 1 开始；未指定的尝试默认全额交收。
    """

    def __init__(self, schedule: Optional[Dict[int, List[tuple]]] = None):
        self.schedule: Dict[int, Dict[int, float]] = {}
        for oid, items in (schedule or {}).items():
            self.schedule[oid] = dict(items)

    def __call__(self, obligation: "Obligation", ctx: "SettlementContext") -> FailDecision:
        planned = self.schedule.get(obligation.obligation_id, {}).get(obligation.attempt_no)
        if planned is None:
            return FailDecision(obligation.quantity_open, "auto")
        if planned <= 0:
            return FailDecision(0.0, "forced_fail")
        return FailDecision(min(planned, obligation.quantity_open), "forced_partial")


@dataclass(frozen=True)
class SettlementCalendar:
    """交收日历：周末 + 各自节假日集合（不同资产可以不同）。"""

    name: str = "DEFAULT"
    holidays: frozenset = field(default_factory=frozenset)

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def add_business_days(self, d: date, n: int) -> date:
        """从成交日 d 起推 n 个交收工作日；n=0 表示 T+0（当日交收）。"""
        if n < 0:
            raise ValueError("交收周期不能为负")
        cur, remaining = d, n
        while remaining > 0:
            cur += timedelta(days=1)
            if self.is_business_day(cur):
                remaining -= 1
        if n == 0 and not self.is_business_day(cur):
            while not self.is_business_day(cur):
                cur += timedelta(days=1)
        return cur

    def next_business_day(self, d: date) -> date:
        cur = d + timedelta(days=1)
        while not self.is_business_day(cur):
            cur += timedelta(days=1)
        return cur


@dataclass(frozen=True)
class AssetSpec:
    symbol: str
    currency: str
    settlement_days: int
    calendar: SettlementCalendar
    fail_fee: float = 0.0          # 每次交收失败的罚金（非交易费用，与盈亏无关）
    fee_currency: Optional[str] = None

    def __post_init__(self):
        if self.settlement_days < 0:
            raise ValueError("settlement_days 不能为负")


@dataclass
class SettlementLeg:
    """一次交收尝试的记录（失败也留痕）。"""

    date: str
    quantity: float
    cash_amount: float
    commission_amount: float
    success: bool
    note: str = ""

    def to_dict(self) -> dict:
        return {"date": self.date, "quantity": self.quantity, "cash_amount": self.cash_amount,
                "commission_amount": self.commission_amount, "success": self.success, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "SettlementLeg":
        return cls(d["date"], d["quantity"], d["cash_amount"], d["commission_amount"],
                   d["success"], d.get("note", ""))


@dataclass
class Obligation:
    """一笔成交对应的待交收义务（逐笔对应冻结/费用/持仓）。"""

    obligation_id: int
    fill_id: int
    order_id: int
    symbol: str
    currency: str
    side: str                         # "BUY" / "SELL"
    trade_date: str
    due_date: str                     # 当前应交收日（失败补交后顺延）
    price: float
    quantity_original: float          # 确认时的义务数量基准
    quantity_open: float              # 尚未交收数量
    commission_original: float
    commission_open: float
    quantity_settled: float = 0.0
    status: ObligationStatus = ObligationStatus.PENDING
    legs: List[SettlementLeg] = field(default_factory=list)
    fail_count: int = 0
    attempt_no: int = 0

    @property
    def gross_open(self) -> float:
        return self.price * self.quantity_open

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["side"] = self.side
        d["status"] = self.status.value
        d["legs"] = [leg.to_dict() for leg in self.legs]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Obligation":
        return cls(
            obligation_id=d["obligation_id"], fill_id=d["fill_id"], order_id=d["order_id"],
            symbol=d["symbol"], currency=d["currency"], side=d["side"],
            trade_date=d["trade_date"], due_date=d["due_date"], price=d["price"],
            quantity_original=d["quantity_original"], quantity_open=d["quantity_open"],
            commission_original=d["commission_original"], commission_open=d["commission_open"],
            quantity_settled=d["quantity_settled"], status=ObligationStatus(d["status"]),
            legs=[SettlementLeg.from_dict(x) for x in d["legs"]],
            fail_count=d["fail_count"], attempt_no=d["attempt_no"],
        )


@dataclass
class DividendReceivable:
    """现金分配形成的待收红利：在支付日前不是可用资金。"""

    dividend_id: int
    symbol: str
    currency: str
    amount: float
    record_date: str
    payable_date: str
    status: str = "PENDING"          # PENDING / PAID

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "DividendReceivable":
        return cls(**d)


@dataclass
class CashBalance:
    available: float = 0.0
    frozen: float = 0.0              # 买入待付资金（含对应费用）
    receivable: float = 0.0          # 卖出/红利待收资金，不可支付

    @property
    def total(self) -> float:
        return self.available + self.frozen + self.receivable


@dataclass
class PositionBalance:
    settled: float = 0.0             # 已交收到账的净持仓
    frozen: float = 0.0              # 已卖出待交收而冻结的数量

    @property
    def available_to_sell(self) -> float:
        return self.settled - self.frozen


class SettlementContext:
    """失败策略可见的只读上下文（当前日期与账户状态引用）。"""

    def __init__(self, custodian: "SettlementCustodian", as_of: date):
        self.as_of = as_of
        self.cash = custodian.cash
        self.positions = custodian.positions


class InsufficientFunds(Exception):
    pass


class InsufficientPosition(Exception):
    pass


class UnknownAsset(Exception):
    pass


@dataclass
class FillConfirmation:
    """成交确认结果：义务编号、冻结的资金/证券与确认时交易盈亏。"""

    obligation: Obligation
    frozen_cash: float
    frozen_position: float
    realized_pnl: float
    cost_basis_after: float


def _iso(d: date) -> str:
    return d.isoformat()


def _parse(s: str) -> date:
    return date.fromisoformat(s)


class SettlementCustodian:
    """托管结算账户：确认成交、推进交收、处理公司行为、快照存档。"""

    def __init__(
        self,
        assets: Iterable[AssetSpec],
        initial_cash: Optional[Dict[str, float]] = None,
        fail_planner: Optional[FailPlanner] = None,
        initial_positions: Optional[Dict[str, float]] = None,
    ):
        assets = list(assets)
        self.assets: Dict[str, AssetSpec] = {a.symbol: a for a in assets}
        if len(self.assets) != len(assets):
            raise ValueError("资产规格 symbol 重复")
        initial_cash = dict(initial_cash or {})
        self.initial_cash = initial_cash
        initial_positions = dict(initial_positions or {})
        self.initial_positions = initial_positions
        self.cash: Dict[str, CashBalance] = {}
        for ccy, amount in initial_cash.items():
            if amount < 0:
                raise ValueError("初始资金不能为负")
            self.cash[ccy] = CashBalance(available=amount)
        self.positions: Dict[str, PositionBalance] = {}
        for symbol, qty in initial_positions.items():
            self.positions[symbol] = PositionBalance(settled=qty)
        self.obligations: Dict[int, Obligation] = {}
        self.dividends: List[DividendReceivable] = []
        self.cost_basis: Dict[str, float] = {}
        self.realized_pnl_total = 0.0
        self.fail_fees_total: Dict[str, float] = {}
        self.fail_planner: FailPlanner = fail_planner or always_settle
        self._next_obligation_id = 1
        self._next_dividend_id = 1
        self.journal: List[dict] = []

    def _asset(self, symbol: str) -> AssetSpec:
        try:
            return self.assets[symbol]
        except KeyError:
            raise UnknownAsset(symbol)

    def cash_balance(self, currency: str) -> CashBalance:
        return self.cash.get(currency, CashBalance())

    def position(self, symbol: str) -> PositionBalance:
        return self.positions.get(symbol, PositionBalance())

    def open_obligations(self, as_of: Optional[date] = None) -> List[Obligation]:
        obs = [o for o in self.obligations.values() if o.status is not ObligationStatus.SETTLED]
        if as_of is not None:
            key = _iso(as_of)
            obs = [o for o in obs if o.trade_date <= key]
        return sorted(obs, key=lambda o: o.obligation_id)

    def pending_dividends(self) -> List[DividendReceivable]:
        return [d for d in self.dividends if d.status == "PENDING"]

    def snapshot(self) -> dict:
        """全部状态快照（与交收推进方式无关，可直接 JSON 存档）。"""
        return {
            "cash": {c: b.__dict__.copy() for c, b in self.cash.items()},
            "positions": {s: p.__dict__.copy() for s, p in self.positions.items()},
            "obligations": [o.to_dict() for o in self.obligations.values()],
            "dividends": [d.to_dict() for d in self.dividends],
            "cost_basis": dict(self.cost_basis),
            "realized_pnl_total": self.realized_pnl_total,
            "fail_fees_total": dict(self.fail_fees_total),
            "next_obligation_id": self._next_obligation_id,
            "next_dividend_id": self._next_dividend_id,
            "journal": list(self.journal),
        }

    # ----- 成交确认（T 日） -----

    def confirm_fill(self, fill: Fill, trade_date: date) -> FillConfirmation:
        """成交确认：登记待交收义务并冻结资金/证券。不产生任何可用余额。"""
        spec = self._asset(fill.symbol)
        due = spec.calendar.add_business_days(trade_date, spec.settlement_days)
        ccy = spec.currency
        fee_ccy = spec.fee_currency or ccy
        bal = self.cash.setdefault(ccy, CashBalance())
        fee_bal = self.cash.setdefault(fee_ccy, CashBalance()) if fee_ccy != ccy else bal
        pos = self.positions.setdefault(fill.symbol, PositionBalance())

        gross = fill.gross_amount
        oid = self._next_obligation_id
        self._next_obligation_id += 1
        realized = 0.0

        if fill.side is OrderSide.BUY:
            if bal.available < gross or fee_bal.available < fill.commission:
                raise InsufficientFunds(
                    f"买入确认资金不足 {ccy}: 需要本金 {gross} / 费 {fill.commission} {fee_ccy}"
                )
            bal.available -= gross
            bal.frozen += gross
            fee_bal.available -= fill.commission
            fee_bal.frozen += fill.commission
            prev_cost = self.cost_basis.get(fill.symbol, 0.0) * pos.settled
            new_settled = pos.settled + fill.quantity
            self.cost_basis[fill.symbol] = (prev_cost + gross) / new_settled
        else:
            if pos.available_to_sell < fill.quantity:
                raise InsufficientPosition(
                    f"卖出确认持仓不足 {fill.symbol}: 可卖 {pos.available_to_sell} < {fill.quantity}"
                )
            pos.frozen += fill.quantity
            bal.receivable += gross - fill.commission
            # 卖出费用在成交确认当日即从可用资金扣除（应收款已按净额冻结）。
            fee_bal.available -= fill.commission
            realized = (fill.price - self.cost_basis.get(fill.symbol, 0.0)) * fill.quantity
            self.realized_pnl_total += realized

        ob = Obligation(
            obligation_id=oid, fill_id=fill.fill_id, order_id=fill.order_id,
            symbol=fill.symbol, currency=ccy, side=fill.side.value,
            trade_date=_iso(trade_date), due_date=_iso(due), price=fill.price,
            quantity_original=fill.quantity, quantity_open=fill.quantity,
            commission_original=fill.commission, commission_open=fill.commission,
        )
        self.obligations[oid] = ob
        self.journal.append({
            "type": "confirm", "date": _iso(trade_date), "obligation_id": oid,
            "fill": fill.to_row(),
        })
        return FillConfirmation(
            obligation=ob,
            frozen_cash=gross + fill.commission if fill.side is OrderSide.BUY else 0.0,
            frozen_position=0.0 if fill.side is OrderSide.BUY else fill.quantity,
            realized_pnl=realized,
            cost_basis_after=self.cost_basis.get(fill.symbol, 0.0),
        )

    # ----- 交收推进 -----

    def advance(self, as_of: date) -> List[SettlementLeg]:
        """推进到 as_of 日收盘：处理当日到期的全部未结义务与红利。

        顺序固定：先到期义务（按 obligation_id），后红利支付。
        同日卖出交收到账的现金在 advance 返回后即为 available，
        但不会在同一批次内再用于别的买入交收（同日资金互不挪用）。
        """
        legs: List[SettlementLeg] = []
        key = _iso(as_of)
        due_ids = [
            o.obligation_id for o in self.open_obligations()
            if o.due_date <= key
        ]
        for oid in sorted(due_ids):
            legs.append(self._settle_one(self.obligations[oid], as_of))
        for div in list(self.pending_dividends()):
            if div.payable_date <= key:
                self._pay_dividend(div, as_of)
        return legs

    def _settle_one(self, ob: Obligation, as_of: date) -> SettlementLeg:
        spec = self._asset(ob.symbol)
        pos = self.positions.setdefault(ob.symbol, PositionBalance())
        bal = self.cash.setdefault(ob.currency, CashBalance())
        ob.attempt_no += 1
        decision = self.fail_planner(ob, SettlementContext(self, as_of))
        qty = min(max(0.0, decision.quantity), ob.quantity_open)

        if qty <= 0:
            return self._record_fail(ob, spec, as_of, 0.0, 0.0, decision.note)

        frac = qty / ob.quantity_open
        cash_part = ob.gross_open * frac
        fee_part = ob.commission_open * frac

        if ob.side == "BUY":
            # 买入交收：冻结资金实际付出，证券到账（进 settled）。
            if bal.frozen + 1e-9 < cash_part + fee_part:
                return self._record_fail(ob, spec, as_of, cash_part, fee_part,
                                         "frozen_shortfall")
            bal.frozen -= cash_part + fee_part
            pos.settled += qty
        else:
            # 卖出交收：冻结证券付出，应收款实际到账（进 available）。
            if pos.frozen + 1e-9 < qty:
                return self._record_fail(ob, spec, as_of, cash_part, fee_part,
                                         "frozen_position_shortfall")
            pos.frozen -= qty
            pos.settled -= qty
            if bal.receivable + 1e-9 < cash_part - fee_part:
                return self._record_fail(ob, spec, as_of, cash_part, fee_part,
                                         "receivable_shortfall")
            bal.receivable -= cash_part - fee_part
            bal.available += cash_part - fee_part

        ob.quantity_open -= qty
        ob.commission_open -= fee_part
        ob.quantity_settled += qty
        ob.status = (ObligationStatus.SETTLED if ob.quantity_open <= 1e-9
                     else ObligationStatus.PARTIALLY_SETTLED)
        if ob.status is not ObligationStatus.SETTLED:
            ob.due_date = _iso(spec.calendar.next_business_day(as_of))
        leg = SettlementLeg(_iso(as_of), qty, cash_part, fee_part, True, decision.note)
        ob.legs.append(leg)
        self.journal.append({
            "type": "settle", "date": _iso(as_of), "obligation_id": ob.obligation_id,
            "quantity": qty, "note": decision.note,
        })
        return leg

    def _record_fail(self, ob: Obligation, spec: AssetSpec, as_of: date,
                     cash_part: float, fee_part: float, note: str) -> SettlementLeg:
        ob.fail_count += 1
        ob.status = (ObligationStatus.FAILED if ob.quantity_settled <= 1e-9
                     else ObligationStatus.PARTIALLY_SETTLED)
        if ob.quantity_settled > 1e-9:
            # 曾部分交收后剩余失败：仍视为部分交收、未结余额待补
            ob.status = ObligationStatus.PARTIALLY_SETTLED
        ob.due_date = _iso(spec.calendar.next_business_day(as_of))
        fee_ccy = spec.fee_currency or ob.currency
        if spec.fail_fee > 0:
            fee_bal = self.cash.setdefault(fee_ccy, CashBalance())
            fee_bal.available -= spec.fail_fee
            self.fail_fees_total[fee_ccy] = self.fail_fees_total.get(fee_ccy, 0.0) + spec.fail_fee
        leg = SettlementLeg(_iso(as_of), 0.0, cash_part, fee_part, False, note)
        ob.legs.append(leg)
        self.journal.append({
            "type": "fail", "date": _iso(as_of), "obligation_id": ob.obligation_id,
            "note": note,
        })
        return leg

    # ----- 公司行为（交收日前拆分 / 现金分配） -----

    def apply_split(self, event_date: date, symbol: str, ratio: float) -> dict:
        """拆分（如 1 拆 ratio 股）。

        权益归属（成交日经济所有权）：
        - 已交收持仓（settled）与已卖出待交收冻结（frozen）同比例放大；
        - 买入方尚未交收的应收证券义务同比例放大；
        - 卖出方尚未交收的应付证券义务同比例放大（买方、卖方义务同步变化，
          谁持有经济利益谁拿到拆出的股票）；
        - 义务单价按 1/ratio 下调，现金应收/应付总额不变；佣金不变；
        - 成本基础（每股）按 1/ratio 下调，拆分本身不产生盈亏。
        """
        if ratio <= 0:
            raise ValueError("拆分比例必须为正")
        pos = self.positions.setdefault(symbol, PositionBalance())
        pos.settled *= ratio
        pos.frozen *= ratio
        if symbol in self.cost_basis:
            self.cost_basis[symbol] /= ratio
        affected = []
        for ob in self.open_obligations():
            if ob.symbol != symbol:
                continue
            ob.price /= ratio
            ob.quantity_open *= ratio
            ob.quantity_original *= ratio
            ob.quantity_settled *= ratio
            affected.append(ob.obligation_id)
        self.journal.append({
            "type": "split", "date": _iso(event_date), "symbol": symbol, "ratio": ratio,
        })
        return {"symbol": symbol, "ratio": ratio, "affected_obligations": affected}

    def apply_cash_dividend(
        self, record_date: date, symbol: str, per_share: float, payable_date: date
    ) -> DividendReceivable:
        """现金分配登记（trade-date entitlement）。

        权益归属：record_date 当日按经济归属持股计算红利——
        已交收持仓 + 买入未交收应收 - 卖出待交收冻结。即在交收途上的买入方
        享有红利（其买入义务尚未交收），已卖出待交收部分归对手方所有。
        红利在 payable_date 前记为 receivable，不是可支付资金。
        """
        spec = self._asset(symbol)
        pos = self.position(symbol)
        entitlement = pos.settled
        for ob in self.open_obligations():
            if ob.symbol != symbol:
                continue
            if ob.side == "BUY":
                entitlement += ob.quantity_open
            else:
                entitlement -= ob.quantity_open
        amount = entitlement * per_share
        div = DividendReceivable(
            dividend_id=self._next_dividend_id, symbol=symbol, currency=spec.currency,
            amount=amount, record_date=_iso(record_date), payable_date=_iso(payable_date),
        )
        self._next_dividend_id += 1
        self.dividends.append(div)
        bal = self.cash.setdefault(spec.currency, CashBalance())
        bal.receivable += amount
        self.journal.append({
            "type": "dividend", "date": _iso(record_date), "dividend_id": div.dividend_id,
            "symbol": symbol, "per_share": per_share, "payable_date": _iso(payable_date),
            "amount": amount,
        })
        return div

    def _pay_dividend(self, div: DividendReceivable, as_of: date) -> None:
        bal = self.cash.setdefault(div.currency, CashBalance())
        bal.receivable -= div.amount
        bal.available += div.amount
        div.status = "PAID"
        self.journal.append({
            "type": "dividend_pay", "date": _iso(as_of), "dividend_id": div.dividend_id,
        })

    # ----- 存档接续 -----

    def save_dict(self) -> dict:
        """可直接 json.dump 的完整存档（含资产规格与交收日历）。"""
        snap = self.snapshot()
        snap["assets"] = [
            {
                "symbol": a.symbol, "currency": a.currency,
                "settlement_days": a.settlement_days, "fail_fee": a.fail_fee,
                "fee_currency": a.fee_currency,
                "calendar": {"name": a.calendar.name,
                             "holidays": sorted(h.isoformat() for h in a.calendar.holidays)},
            }
            for a in self.assets.values()
        ]
        snap["initial_cash"] = dict(self.initial_cash)
        snap["initial_positions"] = dict(self.initial_positions)
        return snap

    @classmethod
    def restore(cls, data: dict, fail_planner: Optional[FailPlanner] = None) -> "SettlementCustodian":
        """从 save_dict 的存档恢复；恢复后状态与连续推进完全一致。"""
        assets = []
        for a in data["assets"]:
            cal = SettlementCalendar(
                name=a["calendar"]["name"],
                holidays=frozenset(_parse(h) for h in a["calendar"]["holidays"]),
            )
            assets.append(AssetSpec(
                symbol=a["symbol"], currency=a["currency"],
                settlement_days=a["settlement_days"], calendar=cal,
                fail_fee=a.get("fail_fee", 0.0), fee_currency=a.get("fee_currency"),
            ))
        cust = cls(
            assets, fail_planner=fail_planner,
            initial_cash=data.get("initial_cash"),
            initial_positions=data.get("initial_positions"),
        )
        for ccy, b in data["cash"].items():
            cust.cash[ccy] = CashBalance(b["available"], b["frozen"], b["receivable"])
        for sym, p in data["positions"].items():
            cust.positions[sym] = PositionBalance(p["settled"], p["frozen"])
        cust.obligations = {
            int(oid): Obligation.from_dict(od)
            for oid, od in ((o["obligation_id"], o) for o in data["obligations"])
        }
        cust.dividends = [DividendReceivable.from_dict(d) for d in data["dividends"]]
        cust.cost_basis = dict(data["cost_basis"])
        cust.realized_pnl_total = data["realized_pnl_total"]
        cust.fail_fees_total = dict(data["fail_fees_total"])
        cust._next_obligation_id = data["next_obligation_id"]
        cust._next_dividend_id = data["next_dividend_id"]
        cust.journal = list(data.get("journal", []))
        return cust


def replay_journal(data: dict) -> "SettlementCustodian":
    """独立复算：仅凭存档中的资产规格 + 事件日记账重建全部状态。

    不读取存档的现金/持仓/义务余额，只重放确认、交收、失败、公司行为事件，
    失败数量直接取自日记账（策略在失败当日的决定已随事件留痕），
    因此复算不需要 fail_planner，结果可与 restore 的快照逐字段核对。
    """
    assets = []
    for a in data["assets"]:
        cal = SettlementCalendar(
            name=a["calendar"]["name"],
            holidays=frozenset(_parse(h) for h in a["calendar"]["holidays"]),
        )
        assets.append(AssetSpec(
            symbol=a["symbol"], currency=a["currency"],
            settlement_days=a["settlement_days"], calendar=cal,
            fail_fee=a.get("fail_fee", 0.0), fee_currency=a.get("fee_currency"),
        ))
    cust = SettlementCustodian(
        assets,
        initial_cash=data.get("initial_cash"),
        initial_positions=data.get("initial_positions"),
    )
    for ev in data["journal"]:
        d = _parse(ev["date"])
        kind = ev["type"]
        if kind == "confirm":
            fr = ev["fill"]
            fill = Fill(
                fill_id=fr["fill_id"], order_id=fr["order_id"], symbol=fr["symbol"],
                side=OrderSide(fr["side"]), timestamp=fr["timestamp"],
                quantity=fr["quantity"], price=fr["price"], commission=fr["commission"],
                bar_timestamp=fr["bar_timestamp"],
            )
            cust.confirm_fill(fill, d)
        elif kind == "split":
            cust.apply_split(d, ev["symbol"], ev["ratio"])
        elif kind == "dividend":
            div = cust.apply_cash_dividend(
                d, ev["symbol"], ev["per_share"], _parse(ev["payable_date"])
            )
            if abs(div.amount - ev["amount"]) > 1e-9:
                raise AssertionError("日记账重算红利金额不一致")
        elif kind in ("settle", "fail"):
            # 交收事件已记录当时决定交收的数量；直接重放该单笔交收，
            # 不依赖（也不需要）原始的 fail_planner。
            ob = cust.obligations[ev["obligation_id"]]
            qty = ev.get("quantity", 0.0)
            note = ev.get("note", "")
            cust.fail_planner = lambda o, ctx, _q=qty, _n=note: FailDecision(_q, _n)
            cust._settle_one(ob, d)
        elif kind == "dividend_pay":
            div = next(x for x in cust.pending_dividends() if x.dividend_id == ev["dividend_id"])
            cust._pay_dividend(div, d)
    return cust
