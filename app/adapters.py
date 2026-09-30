"""模拟外部适配器：付款网关与充电柜。

真实部署时替换为 HTTP 调用；这里返回确定性回执，方便测试幂等。
"""
import itertools
import time

_pay_seq = itertools.count(1)
_cab_seq = itertools.count(1)


def payment_refund(order_id: str, amount: int) -> dict:
    """押金退款回执。同一订单重复退款由 service 层幂等拦截。"""
    return {
        "receipt": f"REF-{order_id}-{next(_pay_seq):04d}",
        "amount": amount,
        "channel": "模拟支付网关",
        "ts": time.time(),
    }


def payment_hold(order_id: str, amount: int) -> dict:
    return {
        "receipt": f"HOLD-{order_id}-{next(_pay_seq):04d}",
        "amount": amount,
        "channel": "模拟支付网关",
        "ts": time.time(),
    }


def cabinet_dock(slot_id: str, device_id: str) -> dict:
    """充电柜入槽回执。"""
    return {"dock_id": f"DOCK-{next(_cab_seq):05d}", "slot": slot_id, "device": device_id, "ok": True}


def cabinet_undock(slot_id: str) -> dict:
    return {"slot": slot_id, "ok": True}
