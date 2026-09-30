"""系统规则测试：设备排他、消毒阻断、版本校验、退款幂等、馆区权限，
另含逾期扫描、充电完成、故障槽、跨馆调拨、维修权限。

运行：python3 -m unittest discover -s tests -v
"""
import tempfile
import threading
import unittest
from pathlib import Path

from app import config, db, service
from app.seed import seed

CLERK_S1 = {"role": "clerk", "site_id": "S1"}
CLERK_S2 = {"role": "clerk", "site_id": "S2"}
TECH_S1 = {"role": "technician", "site_id": "S1"}
TECH_S2 = {"role": "technician", "site_id": "S2"}
REPAIR_S1 = {"role": "repair", "site_id": "S1"}
SUP_S1 = {"role": "supervisor", "site_id": "S1"}
SUP_S2 = {"role": "supervisor", "site_id": "S2"}

GOOD = {"earphone": True, "strap": True, "shell_ok": True,
        "button_ok": True, "lang_ok": True, "battery": 60}


class TestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        config.DATA_DIR = self.tmp
        config.DB_PATH = self.tmp / "museum.db"
        config.PHOTO_DIR = self.tmp / "photos"
        config.LANG_DIR = self.tmp / "langpacks"
        db._conn = None
        seed()

    def tearDown(self):
        db.get_conn().close()
        db._conn = None

    def assertError(self, exc, code=None, contains=None):
        if code is not None:
            self.assertEqual(exc.code, code)
        if contains is not None:
            self.assertIn(contains, str(exc))


class TestVersionCheckout(TestBase):
    def test_old_version_blocked(self):
        _, problems = service.checkout_precheck("AG-004")
        self.assertTrue(any("版本" in p for p in problems))

    def test_broken_langpack_blocked(self):
        _, problems = service.checkout_precheck("AG-005")
        self.assertTrue(any("语言包损坏" in p for p in problems))

    def test_low_battery_blocked(self):
        _, problems = service.checkout_precheck("AG-003")
        self.assertTrue(any("电量不足" in p for p in problems))

    def test_missing_earphone_blocked(self):
        _, problems = service.checkout_precheck("AG-006")
        self.assertTrue(any("耳机" in p for p in problems))

    def test_good_device_passes(self):
        _, problems = service.checkout_precheck("AG-001")
        self.assertEqual(problems, [])


class TestDeviceExclusivity(TestBase):
    def test_second_order_rejected(self):
        service.checkout(CLERK_S1, "AG-001", "V-100001")
        with self.assertRaises(service.ServiceError) as c:
            service.checkout(CLERK_S1, "AG-001", "V-100002")
        self.assertError(c.exception, 409, "锁定")

    def test_concurrent_checkout_single_winner(self):
        barrier = threading.Barrier(2)
        wins, errs = [], []

        def worker(visitor):
            barrier.wait()
            try:
                wins.append(service.checkout(CLERK_S1, "AG-002", visitor))
            except service.ServiceError:
                errs.append(1)

        threads = [threading.Thread(target=worker, args=(v,))
                   for v in ("V-100001", "V-100002")]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(wins), 1)
        self.assertEqual(len(errs), 1)
        active = db.get_conn().execute(
            "SELECT COUNT(*) n FROM orders WHERE device_id='AG-002' AND status='active'").fetchone()["n"]
        self.assertEqual(active, 1)

    def test_duplicate_return_no_double_refund(self):
        r = service.return_device(CLERK_S1, "AG-007", "S1", GOOD)
        self.assertEqual(r["deposit_status"], "refunded")
        self.assertEqual(r["refunded"], 200)
        with self.assertRaises(service.ServiceError) as c:
            service.return_device(CLERK_S1, "AG-007", "S1", GOOD)
        self.assertError(c.exception, 409, "重复归还")
        order = db.get_conn().execute(
            "SELECT * FROM orders WHERE id='O-SEED-007'").fetchone()
        self.assertEqual(order["refunded_amount"], 200)


class TestSitePermission(TestBase):
    def test_checkout_cross_site_denied(self):
        with self.assertRaises(service.ServiceError) as c:
            service.checkout(CLERK_S2, "AG-001", "V-100002")
        self.assertError(c.exception, 403, "跨馆")

    def test_supervisor_cross_site_allowed(self):
        r = service.checkout(SUP_S2, "AG-001", "V-100002")
        self.assertTrue(r["order_id"])

    def test_batch_cross_site_denied(self):
        service.return_device(CLERK_S1, "AG-007", "S1", GOOD)
        with self.assertRaises(service.ServiceError) as c:
            service.create_batch(TECH_S2, ["AG-007"])
        self.assertError(c.exception, 403, "跨馆")

    def test_clerk_cannot_approve_payout(self):
        # 制造一笔待复核押金
        service.return_device(CLERK_S1, "AG-008", "S1",
                              {**GOOD, "earphone": False}, dispute=True)
        with self.assertRaises(service.ServiceError) as c:
            service.settle_deposit(CLERK_S1, "O-SEED-008", "approve")
        self.assertError(c.exception, 403, "无权")

    def test_clerk_can_report_but_not_resolve_repair(self):
        tid = service.report_fault(CLERK_S1, "AG-001", "按键失灵", "button")["ticket_id"]
        with self.assertRaises(service.ServiceError) as c:
            service.resolve_repair(CLERK_S1, tid, "修复")
        self.assertError(c.exception, 403)
        service.resolve_repair(REPAIR_S1, tid, "修复")
        dev = db.get_conn().execute("SELECT * FROM devices WHERE id='AG-001'").fetchone()
        self.assertEqual(dev["status"], "pending_disinfection")  # 修复后必须重新消毒


class TestDisinfectionBlock(TestBase):
    def _batch(self):
        # AG-009 种子已在一个未灭菌批次中；先归还 AG-007 造新设备
        service.return_device(CLERK_S1, "AG-007", "S1", GOOD)
        return service.create_batch(TECH_S1, ["AG-007"])["batch_id"]

    def test_release_before_sterilization_blocked(self):
        bid = self._batch()
        with self.assertRaises(service.ServiceError) as c:
            service.release_batch(TECH_S1, bid)
        self.assertError(c.exception, 409, "消毒阻断")

    def test_double_release_blocked(self):
        bid = self._batch()
        service.sterilize_batch(TECH_S1, bid)
        service.release_batch(TECH_S1, bid)
        with self.assertRaises(service.ServiceError) as c:
            service.release_batch(TECH_S1, bid)
        self.assertError(c.exception, 409, "重复放行")

    def test_release_routes_by_battery(self):
        # AG-009 电量 58 < 80：灭菌放行后进待充电
        bid = db.get_conn().execute(
            "SELECT batch_id FROM disinfect_items WHERE device_id='AG-009'").fetchone()["batch_id"]
        service.sterilize_batch(TECH_S1, bid)
        routed = dict(service.release_batch(TECH_S1, bid)["routed"])
        self.assertEqual(routed["AG-009"], "pending_charge")

    def test_fixed_device_must_redisinfect(self):
        tid = db.get_conn().execute(
            "SELECT id FROM repair_tickets WHERE device_id='AG-011'").fetchone()["id"]
        service.resolve_repair(REPAIR_S1, tid, "换壳")
        with self.assertRaises(service.ServiceError):
            service.checkout(CLERK_S1, "AG-011", "V-100001")  # 仍在待消毒，不可借


class TestDepositIdempotency(TestBase):
    def test_approve_then_block_second_refund(self):
        service.return_device(CLERK_S1, "AG-008", "S1",
                              {**GOOD, "earphone": False}, dispute=True)
        r1 = service.settle_deposit(SUP_S1, "O-SEED-008", "approve")
        # 逾期 20 + 耳机 50，退 130
        self.assertEqual(r1["refund"], 130)
        with self.assertRaises(service.ServiceError) as c:
            service.settle_deposit(SUP_S1, "O-SEED-008", "approve")
        self.assertError(c.exception, 409, "幂等")
        with self.assertRaises(service.ServiceError) as c:
            service.settle_deposit(SUP_S1, "O-SEED-008", "waive")
        self.assertEqual(c.exception.code, 409)

    def test_waive_refunds_full(self):
        service.return_device(CLERK_S1, "AG-008", "S1", GOOD)  # 仅逾期 20
        service.settle_deposit(SUP_S1, "O-SEED-008", "waive")
        order = db.get_conn().execute(
            "SELECT * FROM orders WHERE id='O-SEED-008'").fetchone()
        self.assertEqual(order["refunded_amount"], 200)
        self.assertEqual(order["earnings"], 0)


class TestChargingAndSlots(TestBase):
    def test_faulty_slot_rejects_dock(self):
        service.set_slot_fault(TECH_S1, "S1-C01", False, "接触不良")
        with self.assertRaises(service.ServiceError) as c:
            service.dock(TECH_S1, "AG-010", "S1-C01")
        self.assertError(c.exception, 409, "故障")

    def test_charge_until_threshold_then_available(self):
        service.dock(TECH_S1, "AG-010", "S1-C01")
        for _ in range(6):
            service.tick_charging()
        dev = db.get_conn().execute("SELECT * FROM devices WHERE id='AG-010'").fetchone()
        self.assertEqual(dev["status"], "available")
        self.assertGreaterEqual(dev["battery"], config.READY_BATTERY)

    def test_undock_below_threshold_blocked(self):
        service.dock(TECH_S1, "AG-010", "S1-C01")
        service.tick_charging()  # 30 -> 44，仍不足
        with self.assertRaises(service.ServiceError) as c:
            service.undock(TECH_S1, "AG-010")
        self.assertError(c.exception, 409, "阈值")

    def test_faulting_occupied_charging_slot_requeues(self):
        service.dock(TECH_S1, "AG-010", "S1-C01")
        service.set_slot_fault(TECH_S1, "S1-C01", False, "突发故障")
        dev = db.get_conn().execute("SELECT * FROM devices WHERE id='AG-010'").fetchone()
        slot = db.get_conn().execute("SELECT * FROM charging_slots WHERE id='S1-C01'").fetchone()
        self.assertEqual(dev["status"], "pending_charge")
        self.assertIsNone(slot["device_id"])


class TestOverdue(TestBase):
    def test_overdue_scan_flips_once(self):
        conn = db.get_conn()
        r = service.checkout(CLERK_S1, "AG-001", "V-100001")
        conn.execute("UPDATE orders SET due_at=? WHERE id=?", (0, r["order_id"]))
        conn.commit()
        flipped = service.scan_overdue()
        self.assertIn(r["order_id"], flipped)
        # 罚金只计一次
        service.scan_overdue()
        o = conn.execute("SELECT * FROM orders WHERE id=?", (r["order_id"],)).fetchone()
        self.assertEqual(o["status"], "overdue")
        self.assertEqual(o["fee_overdue"], config.OVERDUE_FEE)


class TestCrossSiteReturn(TestBase):
    def test_full_transfer_flow(self):
        r = service.return_device(CLERK_S1, "AG-103", "S1", GOOD)
        self.assertTrue(r["cross_site"])
        bid = service.create_batch(TECH_S1, ["AG-103"])["batch_id"]
        service.sterilize_batch(TECH_S1, bid)
        routed = dict(service.release_batch(TECH_S1, bid)["routed"])
        self.assertEqual(routed["AG-103"], "in_transit")
        with self.assertRaises(service.ServiceError) as c:
            service.receive_transfer(TECH_S2, "AG-103")
        self.assertEqual(c.exception.code, 403)
        out = service.receive_transfer(SUP_S2, "AG-103")
        self.assertEqual(out["target"], "pending_charge")  # 电量 60 < 80
        dev = db.get_conn().execute("SELECT * FROM devices WHERE id='AG-103'").fetchone()
        self.assertEqual(dev["current_site_id"], "S2")
        self.assertEqual(dev["pending_transfer"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
