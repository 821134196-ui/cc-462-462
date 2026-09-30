"""业务流测试:设备排他、消毒阻断、版本校验、退款幂等、馆区权限等。

运行: python3 -m unittest discover -s tests -v
每个用例使用独立临时数据目录,互不影响。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audioguide import config, tasks
from audioguide import services as svc
from audioguide.db import all_, connect, init_db, one
from audioguide.seed import seed_all


class FlowTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ag-test-")
        config.set_data_dir(self.dir)
        config.ensure_dirs()
        self.conn = connect()
        init_db(self.conn)
        seed_all(self.conn, self.dir)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def staff(self, name):
        return one(self.conn, "SELECT * FROM staff WHERE name=?", (name,))

    def device(self, code):
        return one(self.conn, "SELECT * FROM devices WHERE code=?", (code,))

    def open_order(self, dev_code):
        return one(self.conn,
                   "SELECT o.* FROM orders o JOIN devices d ON d.id=o.device_id"
                   " WHERE d.code=? AND o.status IN ('active','overdue')", (dev_code,))


class TestDeviceExclusivity(FlowTestBase):
    """设备排他:同一设备不能同时被两个订单锁定。"""

    def test_same_device_cannot_be_locked_twice(self):
        wang = self.staff("王柜员")
        dev = self.device("AG-104")
        o1 = svc.create_order(self.conn, wang, 1, "测试甲", "", "zh", device_id=dev["id"])
        self.assertEqual(o1["device_id"], dev["id"])
        with self.assertRaises((svc.StateError, svc.ValidationError)):
            svc.create_order(self.conn, wang, 1, "测试乙", "", "zh", device_id=dev["id"])
        # 自动分配也不会再选中这台在借设备
        o2 = svc.create_order(self.conn, wang, 1, "测试丙", "", "zh")
        self.assertNotEqual(o2["device_id"], dev["id"])
        active = all_(self.conn,
                      "SELECT * FROM orders WHERE device_id=? AND status IN ('active','overdue')",
                      (dev["id"],))
        self.assertEqual(len(active), 1)

    def test_unique_index_backstop(self):
        """即使设备状态被异常改回可借,部分唯一索引也拒绝第二个在借订单。"""
        import sqlite3
        wang = self.staff("王柜员")
        dev = self.device("AG-104")
        svc.create_order(self.conn, wang, 1, "测试甲", "", "zh", device_id=dev["id"])
        self.conn.execute("UPDATE devices SET status='available' WHERE id=?", (dev["id"],))
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            svc.create_order(self.conn, wang, 1, "测试乙", "", "zh", device_id=dev["id"])


class TestDisinfectionBlocking(FlowTestBase):
    """消毒阻断:消毒批次放行前设备不可借;电量达标+放行后才可重新入柜。"""

    def test_blocked_until_batch_released_and_charged(self):
        wang = self.staff("王柜员")
        order = self.open_order("AG-101")  # 电量 78,低于入柜阈值 80
        svc.complete_return(self.conn, wang, order["code"])
        dev = self.device("AG-101")
        self.assertEqual(dev["status"], "pending_disinfection")
        self.assertEqual(dev["disinfected"], 0)
        # 待消毒状态不可借
        with self.assertRaises(svc.ValidationError):
            svc.create_order(self.conn, wang, 1, "测试", "", "zh", device_id=dev["id"])
        # 放行批次 → 电量不足,转待充电并入槽
        batch = one(self.conn,
                    "SELECT b.* FROM disinfection_batches b JOIN disinfection_items i"
                    " ON i.batch_id=b.id WHERE i.device_id=? AND b.status='open'", (dev["id"],))
        svc.release_batch(self.conn, wang, batch["id"])
        dev = self.device("AG-101")
        self.assertEqual(dev["status"], "pending_charging")
        self.assertIsNotNone(dev["slot_id"])
        # 仍不可借;充电至阈值后自动出槽转可借
        with self.assertRaises(svc.ValidationError):
            svc.create_order(self.conn, wang, 1, "测试", "", "zh", device_id=dev["id"])
        tasks.charging_tick_once()
        dev = self.device("AG-101")
        self.assertGreaterEqual(dev["battery"], config.SHELF_MIN_BATTERY)
        self.assertEqual(dev["status"], "available")
        self.assertIsNone(dev["slot_id"])

    def test_double_release_rejected(self):
        wang = self.staff("王柜员")
        batch = one(self.conn, "SELECT * FROM disinfection_batches WHERE status='open'")
        svc.release_batch(self.conn, wang, batch["id"])
        with self.assertRaises(svc.StateError):
            svc.release_batch(self.conn, wang, batch["id"])


class TestLanguagePackValidation(FlowTestBase):
    """版本校验:语言包过期/损坏的设备借出时被拦截。"""

    def test_outdated_pack_blocked(self):
        wang = self.staff("王柜员")
        dev = self.device("AG-109")  # 英文包 v2.7,要求 v2.8
        with self.assertRaises(svc.ValidationError) as ctx:
            svc.create_order(self.conn, wang, 1, "测试", "", "en", device_id=dev["id"])
        self.assertIn("pack_version", [p[0] for p in ctx.exception.problems])
        # 同设备的合规语种不受影响
        order = svc.create_order(self.conn, wang, 1, "测试", "", "zh", device_id=dev["id"])
        self.assertEqual(order["lang"], "zh")

    def test_corrupted_pack_blocked(self):
        wang = self.staff("王柜员")
        dev = self.device("AG-108")  # 语言包损坏
        with self.assertRaises(svc.ValidationError) as ctx:
            svc.create_order(self.conn, wang, 1, "测试", "", "zh", device_id=dev["id"])
        self.assertIn("pack_corrupt", [p[0] for p in ctx.exception.problems])


class TestRefundIdempotency(FlowTestBase):
    """退款幂等:重复归还不重复退押金。"""

    def test_duplicate_return_refunds_once(self):
        wang = self.staff("王柜员")
        order = self.open_order("AG-101")
        s1 = svc.complete_return(self.conn, wang, order["code"])
        self.assertFalse(s1["already"])
        s2 = svc.complete_return(self.conn, wang, order["code"])
        self.assertTrue(s2["already"])
        refunds = all_(self.conn,
                       "SELECT * FROM payments WHERE order_id=? AND kind='refund'",
                       (order["id"],))
        self.assertEqual(len(refunds), 1)
        dep = one(self.conn, "SELECT * FROM deposits WHERE order_id=?", (order["id"],))
        self.assertEqual(dep["status"], "refunded")
        checks = all_(self.conn, "SELECT * FROM return_checks WHERE order_id=?", (order["id"],))
        self.assertEqual(len(checks), 1)


class TestZonePermissions(FlowTestBase):
    """馆区权限:跨馆借出/归还需授权;讲解员可报故障但不能批准赔付。"""

    def test_cross_museum_lending_denied(self):
        li = self.staff("李柜员")  # 东馆
        with self.assertRaises(svc.PermissionDenied):
            svc.create_order(self.conn, li, 1, "测试", "", "zh")  # 主馆设备
        # 本馆借出正常
        order = svc.create_order(self.conn, li, 2, "测试", "", "zh")
        self.assertEqual(order["museum_id"], 2)

    def test_cross_museum_return_requires_authorized_staff(self):
        wang = self.staff("王柜员")   # 主馆,无跨馆权限
        zhou = self.staff("周管理员")  # 跨馆授权
        order = self.open_order("AG-201")  # 东馆借出
        with self.assertRaises(svc.PermissionDenied):
            svc.complete_return(self.conn, wang, order["code"])
        s = svc.complete_return(self.conn, zhou, order["code"])
        self.assertFalse(s["already"])
        dev = self.device("AG-201")
        self.assertEqual(dev["museum_id"], zhou["museum_id"])  # 设备留在归还馆

    def test_docent_can_report_fault_but_not_approve_payout(self):
        zhao = self.staff("赵讲解员")
        rid = svc.report_fault(self.conn, zhao, "AG-104", "屏幕闪烁")
        self.assertIsNotNone(rid)
        self.assertEqual(self.device("AG-104")["status"], "repair")
        dep = one(self.conn, "SELECT * FROM deposits WHERE status='disputed'")
        with self.assertRaises(svc.PermissionDenied):
            svc.review_deposit(self.conn, zhao, dep["id"], "refund_full")
        # 复核员可以处理
        sun = self.staff("孙复核")
        svc.review_deposit(self.conn, sun, dep["id"], "refund_full")
        self.assertEqual(one(self.conn, "SELECT * FROM deposits WHERE id=?", (dep["id"],))["status"],
                         "refunded")


class TestOverdueAndSlotFault(FlowTestBase):
    """逾期任务、逾期费与充电槽故障流。"""

    def test_overdue_scan_and_late_fee(self):
        wang = self.staff("王柜员")
        order = self.open_order("AG-102")  # 应还时间已过
        self.assertEqual(order["status"], "active")
        n = tasks.overdue_scan_once()
        self.assertGreaterEqual(n, 1)
        order = one(self.conn, "SELECT * FROM orders WHERE id=?", (order["id"],))
        self.assertEqual(order["status"], "overdue")
        s = svc.complete_return(self.conn, wang, order["code"])
        self.assertGreater(s["order"]["late_fee"], 0)
        self.assertEqual(s["deposit"]["status"], "partial")
        self.assertEqual(s["deposit"]["deduction"], s["order"]["late_fee"])

    def test_slot_fault_pauses_charging(self):
        qian = self.staff("钱维修")
        slot = one(self.conn, "SELECT * FROM charging_slots WHERE code='S1-03'")
        before = self.device("AG-103")["battery"]
        svc.set_slot_fault(self.conn, qian, slot["id"], True)
        tasks.charging_tick_once()
        self.assertEqual(self.device("AG-103")["battery"], before)  # 故障槽不充电
        svc.set_slot_fault(self.conn, qian, slot["id"], False)
        tasks.charging_tick_once()
        self.assertGreater(self.device("AG-103")["battery"], before)


class TestDepositDisputeReview(FlowTestBase):
    """押金争议:归还时转复核,复核后只结算一次。"""

    def test_dispute_then_review(self):
        wang = self.staff("王柜员")
        sun = self.staff("孙复核")
        order = self.open_order("AG-101")
        s = svc.complete_return(self.conn, wang, order["code"],
                                earphone_back=False, deduction_mode="dispute")
        self.assertEqual(s["deposit"]["status"], "disputed")
        refunds = all_(self.conn,
                       "SELECT * FROM payments WHERE order_id=? AND kind='refund'",
                       (order["id"],))
        self.assertEqual(len(refunds), 0)  # 争议期间不退款
        dep = one(self.conn, "SELECT * FROM deposits WHERE order_id=?", (order["id"],))
        svc.review_deposit(self.conn, sun, dep["id"], "deduct", 30)
        dep = one(self.conn, "SELECT * FROM deposits WHERE id=?", (dep["id"],))
        self.assertEqual(dep["status"], "partial")
        self.assertEqual(dep["deduction"], 30)
        # 复核只能做一次
        with self.assertRaises(svc.StateError):
            svc.review_deposit(self.conn, sun, dep["id"], "refund_full")


if __name__ == "__main__":
    unittest.main()
