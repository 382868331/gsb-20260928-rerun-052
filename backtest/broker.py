"""模拟券商：委托管理与逐 bar 撮合。

关键约定：
- 挂单不等于成交：订单提交后为 OPEN，只有 match() 产生 Fill 才算成交。
- 新单不参与提交当根 bar 的撮合（next_bar_active 机制），避免策略使用当根未来信息。
- 部分成交：每根 bar 可供成交量受 bar.volume 与 max_participation 限制，
  多个订单按挂单时间先后（order_id 顺序）分配流动性。
- 限价成交价取订单限价；市价单按当前 bar 收盘价成交（简化的冲击假设）。
- 手续费 = max(成交金额 * commission_rate, min_commission)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .models import Bar, CorporateAction, CorporateActionType, Fill, Order, OrderSide, OrderStatus, OrderType


@dataclass
class CommissionModel:
    rate: float = 0.0003          # 按成交金额的比例（双边收取）
    min_commission: float = 1.0   # 每笔最低手续费

    def commission(self, amount: float) -> float:
        return max(self.min_commission, amount * self.rate)


class Broker:
    """订单簿与撮合器。不直接修改账户，成交以 Fill 形式返回给引擎记账。"""

    def __init__(
        self,
        commission: Optional[CommissionModel] = None,
        max_participation: float = 0.1,
    ):
        if not 0.0 < max_participation <= 1.0:
            raise ValueError("max_participation 必须在 (0, 1] 之间")
        self.commission = commission or CommissionModel()
        self.max_participation = max_participation
        self._orders: Dict[int, Order] = {}
        # 当前 bar 新提交的订单：本根 bar 不撮合，下一根 bar 起生效
        self._next_bar_active: set[int] = set()
        self._next_fill_id = 1
        self._halted: set[str] = set()

    # ----- 委托管理 -----

    def register(self, order: Order) -> Optional[str]:
        """登记新委托。返回拒绝原因；None 表示受理成功（挂单，不代表成交）。"""
        if order.order_id in self._orders:
            return f"order_id {order.order_id} 重复"
        if order.quantity <= 0:
            return "委托数量必须为正"
        if order.order_type is OrderType.LIMIT and (order.limit_price is None or order.limit_price <= 0):
            return "限价单必须给出正的限价"
        order.status = OrderStatus.OPEN
        self._orders[order.order_id] = order
        self._next_bar_active.add(order.order_id)
        if order.symbol in self._halted:
            order.halted = True
        return None

    def reject(self, order: Order, reason: str, timestamp: Optional[int] = None) -> None:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self._next_bar_active.discard(order.order_id)

    def cancel(self, order_id: int, timestamp: int) -> tuple[bool, Optional[str]]:
        """撤单。成功返回 (True, None)，失败返回 (False, 原因)。

        已终结订单（全部成交/已撤/已拒）不可撤；
        当根 bar 才提交、尚未进入撮合阶段的订单允许直接撤销。
        """
        order = self._orders.get(order_id)
        if order is None:
            return False, f"订单 {order_id} 不存在"
        if not order.is_active:
            return False, f"订单 {order_id} 状态为 {order.status.value}，不可撤"
        # 最终状态统一记为 CANCELLED；filled_quantity > 0 表示撤单前曾部分成交
        order.cancelled_at = timestamp
        order.status = OrderStatus.CANCELLED
        self._next_bar_active.discard(order_id)
        return True, None

    def get_order(self, order_id: int) -> Optional[Order]:
        return self._orders.get(order_id)

    def active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        orders = [o for o in self._orders.values() if o.is_active]
        if symbol is not None:
            orders = [o for o in orders if o.symbol == symbol]
        return sorted(orders, key=lambda o: (o.submitted_at, o.order_id))

    def open_orders_snapshot(self) -> List[Order]:
        """结束时仍未完全成交的订单（含部分成交后撤单的剩余量为 0，不在此列）。"""
        return [
            o
            for o in self._orders.values()
            if o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING)
        ]

    def all_orders(self) -> List[Order]:
        """全部委托（按委托顺序），供账目落盘。"""
        return [self._orders[oid] for oid in sorted(self._orders)]

    # ----- 撮合 -----

    def on_bar_start(self, bar: Bar) -> None:
        """新一根 bar 开始：上一根 bar 提交的订单从本根起参与撮合。"""
        self._next_bar_active = {
            oid for oid in self._next_bar_active
            if (order := self._orders.get(oid)) is not None
            and (order.symbol != bar.symbol or order.symbol in self._halted)
        }

    def match(self, bar: Bar, timestamp: int) -> List[Fill]:
        """对当前 bar 撮合所有可成交的存量订单，返回本根 bar 的成交列表。"""
        fills: List[Fill] = []
        remaining_liquidity = bar.volume * self.max_participation
        for order in self.active_orders(bar.symbol):
            if order.halted:
                continue
            if order.order_id in self._next_bar_active:
                continue
            if remaining_liquidity <= 0:
                break
            fill = self._match_one(order, bar, timestamp, remaining_liquidity)
            if fill is not None:
                remaining_liquidity -= fill.quantity
                fills.append(fill)
        return fills

    def _match_one(
        self, order: Order, bar: Bar, timestamp: int, available_liquidity: float
    ) -> Optional[Fill]:
        if order.order_type is OrderType.LIMIT:
            if order.side is OrderSide.BUY and bar.low > order.limit_price:
                return None
            if order.side is OrderSide.SELL and bar.high < order.limit_price:
                return None
            fill_price = order.limit_price
        else:
            fill_price = bar.close

        fill_quantity = min(order.remaining_quantity, available_liquidity)
        if fill_quantity <= 0:
            return None

        order.filled_quantity += fill_quantity
        amount = fill_price * fill_quantity
        commission = self.commission.commission(amount)
        if order.remaining_quantity <= 0:
            order.status = OrderStatus.FILLED
            order.filled_at = timestamp
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
        return Fill(
            fill_id=self._next_fill_id,
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            timestamp=timestamp,
            quantity=fill_quantity,
            price=fill_price,
            commission=commission,
            bar_timestamp=bar.timestamp,
            currency=getattr(order, "currency", getattr(bar, "currency", "USD")),
        )

    def apply_corporate_action(self, action: CorporateAction) -> None:
        if action.action_type is CorporateActionType.SPLIT:
            if action.ratio <= 0:
                raise ValueError("拆股比例必须为正")
            for order in self.active_orders(action.symbol):
                order.quantity *= action.ratio
                order.filled_quantity *= action.ratio
                if order.limit_price is not None:
                    order.limit_price /= action.ratio
        elif action.action_type is CorporateActionType.HALT:
            self._halted.add(action.symbol)
            for order in self.active_orders(action.symbol):
                order.halted = True
                order.halted_at = action.effective_at
        elif action.action_type is CorporateActionType.RESUME:
            self._halted.discard(action.symbol)
            self._next_bar_active = {
                oid for oid in self._next_bar_active
                if (order := self._orders.get(oid)) is None or order.symbol != action.symbol
            }
            for order in self._orders.values():
                if order.symbol == action.symbol and order.is_active and order.halted:
                    order.halted = False
                    order.resumed_at = action.effective_at

    def params(self) -> dict:
        return {
            "commission_rate": self.commission.rate,
            "min_commission": self.commission.min_commission,
            "max_participation": self.max_participation,
        }
