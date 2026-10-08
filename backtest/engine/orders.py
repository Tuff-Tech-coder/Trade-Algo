from dataclasses import dataclass
from typing import Optional


@dataclass
class Order:
    """An order produced by the strategy adapter for the simulated broker.

    side          : "long" | "short" | "flat"
    qty           : contracts
    stop          : absolute stop-loss price (0 = none)
    target        : absolute take-profit price (0 = none)
    reason        : free-text, logged with the fill
    order_type    : "market" | "stop"
    stop_trigger  : entry trigger price for stop-entry orders
    """
    side: str
    qty: int
    stop: float
    target: float
    reason: str
    order_type: str = "market"
    stop_trigger: Optional[float] = None
