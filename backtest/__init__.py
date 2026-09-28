"""事件驱动策略回测引擎（第一版）。

模块划分：
- models:   行情、订单、成交等核心数据结构
- events:   事件类型与同刻优先级定义
- data:     人工行情源（合成数据 / 手工构造数据）
- broker:   挂单、撤单、部分成交、手续费的模拟撮合
- account:  现金、持仓、成交账目与独立复算
- strategy: 策略基类与受限信息上下文
- engine:   事件推进主循环
- report:   每次运行的完整账目落盘
"""

from .models import Bar, Order, OrderSide, OrderStatus, OrderType, Fill

__all__ = [
    "Bar",
    "Order",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Fill",
]
