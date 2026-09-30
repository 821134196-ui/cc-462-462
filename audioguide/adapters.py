"""模拟外部硬件/渠道适配器:充电柜与支付回执。

- ChargingCabinetAdapter:模拟充电柜按周期上报电量;故障槽位抛 SlotFault。
- PaymentAdapter:模拟支付渠道冻结/退款/扣款,回执落盘到 data/receipts/。
"""
import os
from datetime import datetime

from . import config
from .db import one


class SlotFault(Exception):
    """充电槽故障,无法继续充电。"""


class ChargingCabinetAdapter:
    """模拟充电柜硬件边界。真实实现会走串口/网络协议,这里按固定速率上报。"""

    def __init__(self, rate=None):
        self.rate = config.CHARGE_RATE_PER_TICK if rate is None else rate

    def read_level(self, slot, current_battery):
        """上报一次充电后的电量。slot 为 charging_slots 行。"""
        if slot["status"] == "fault":
            raise SlotFault(f"充电槽 {slot['code']} 故障,暂停充电")
        return min(100, current_battery + self.rate)


class PaymentAdapter:
    """模拟支付渠道:每次操作生成唯一回执号并写回执文件。"""

    def __init__(self, receipt_dir=None):
        self.dir = receipt_dir or config.receipt_dir()

    def _receipt(self, conn, order_id, order_code, kind, amount):
        seq = one(conn, "SELECT COUNT(*) AS c FROM payments")["c"] + 1
        no = "RT%s-%04d" % (datetime.now().strftime("%y%m%d"), seq)
        conn.execute(
            "INSERT INTO payments(order_id,kind,amount,receipt,created_at)"
            " VALUES(?,?,?,?,?)",
            (order_id, kind, amount, no, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        os.makedirs(self.dir, exist_ok=True)
        kind_cn = {"hold": "押金冻结", "refund": "押金退款", "collect": "赔付扣款"}[kind]
        with open(os.path.join(self.dir, no + ".txt"), "w", encoding="utf-8") as f:
            f.write(
                "博物馆语音导览 · 支付回执(模拟)\n"
                f"回执编号: {no}\n"
                f"订单编号: {order_code}\n"
                f"业务类型: {kind_cn}\n"
                f"金额: ¥{amount:.2f}\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
        return no

    def hold(self, conn, order_id, order_code, amount):
        return self._receipt(conn, order_id, order_code, "hold", amount)

    def refund(self, conn, order_id, order_code, amount):
        return self._receipt(conn, order_id, order_code, "refund", amount)

    def collect(self, conn, order_id, order_code, amount):
        return self._receipt(conn, order_id, order_code, "collect", amount)
