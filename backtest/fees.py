from __future__ import annotations


class FixedRateFee:
    """按成交额比例收费，带最低手续费。"""

    def __init__(self, rate: float = 0.0003, min_fee: float = 1.0) -> None:
        if rate < 0 or min_fee < 0:
            raise ValueError("手续费参数不能为负")
        self.rate = rate
        self.min_fee = min_fee

    def __call__(self, qty: int, price: float) -> float:
        return round(max(qty * price * self.rate, self.min_fee), 6)

    def params(self) -> dict:
        return {"model": "fixed_rate", "rate": self.rate,
                "min_fee": self.min_fee}
