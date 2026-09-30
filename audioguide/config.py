"""全局配置:路径、阈值、费用、语言与状态字典。

数据目录默认在 <项目>/data,可用环境变量 AG_DATA_DIR 覆盖(测试用)。
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_data_dir = os.environ.get("AG_DATA_DIR") or os.path.join(BASE_DIR, "data")


def data_dir():
    return _data_dir


def set_data_dir(path):
    global _data_dir
    _data_dir = path


def db_path():
    return os.path.join(_data_dir, "museum.db")


def photo_dir():
    return os.path.join(_data_dir, "photos")


def langpack_dir():
    return os.path.join(_data_dir, "langpacks")


def receipt_dir():
    return os.path.join(_data_dir, "receipts")


def ensure_dirs():
    for d in (_data_dir, photo_dir(), langpack_dir(), receipt_dir()):
        os.makedirs(d, exist_ok=True)


# ---- 业务参数 ----
DEPOSIT_AMOUNT = 200.0        # 押金(元)
LOAN_HOURS = 4                # 标准租借时长
LATE_FEE_PER_HOUR = 5.0       # 逾期费/小时
LATE_FEE_CAP = 50.0           # 逾期费封顶

LEND_MIN_BATTERY = 40         # 借出最低电量
SHELF_MIN_BATTERY = 80        # 重新入柜(可借)电量阈值
CHARGE_RATE_PER_TICK = 7      # 充电柜每个周期上报的电量增量(%)
CHARGE_TICK_SECONDS = 5       # 充电周期(秒)
OVERDUE_SCAN_SECONDS = 30     # 逾期扫描周期(秒)

# 损坏/缺失赔付标准(元)
FEE_EARPHONE = 30.0
FEE_LANYARD = 10.0
FEE_SHELL = 50.0
FEE_BUTTONS = 40.0

LANGS = {"zh": "中文", "en": "English", "ja": "日本語", "fr": "Français"}
LANG_SHORT = {"zh": "中", "en": "EN", "ja": "日", "fr": "法"}

DEVICE_STATUS = {
    "available": "可借",
    "on_loan": "在借",
    "pending_disinfection": "待消毒",
    "pending_charging": "待充电",
    "repair": "维修中",
    "scrapped": "已报废",
}
ORDER_STATUS = {"active": "在借", "overdue": "逾期未还", "returned": "已归还"}
DEPOSIT_STATUS = {
    "held": "冻结中",
    "refunded": "已全额退还",
    "partial": "部分退还",
    "forfeited": "全额扣除",
    "disputed": "争议待复核",
}
ROLES = {
    "counter": "柜台员",
    "docent": "讲解员",
    "technician": "维修员",
    "reviewer": "复核员",
    "admin": "管理员",
}
